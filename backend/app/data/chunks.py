"""不可变分块清单、版本引用与固定版本读取视图。

设计要点（C1 冻结约束）：

- 分块身份 = 源 + 类别 + 分区算法 + 分区键 + 分区内稳定排序成员的物理摘要；
- 分区只由逻辑键计算，不读取当前行数，不随导入批次大小/顺序漂移；
- 版本只保存分块引用，不复制全量成员；
- 旧版本（``manifest_version=1``）的 ID 与摘要永不改写，成员可从旧表或转换后的
  分块读取；
- 对外成员顺序与布局无关：``(category, logical_key)``；
- 固定版本读取只使用该版本成员的 ``record_id``，不回退到当前最新修订。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .digests import canonical_json_bytes, hash_bytes, sha256_of_object
from .errors import DataError
from .records import logical_key as make_logical_key, spec_for
from .timeutil import utc_now_iso

__all__ = [
    "CHUNK_PARTITION_VERSION",
    "MANIFEST_ALGORITHM_VERSION",
    "MAX_CHUNK_ROWS",
    "VersionView",
    "partition_key_for",
    "build_chunk",
    "current_partition_members",
    "publish_chunked_version",
    "version_refs",
    "version_members",
    "iter_version_members",
    "version_scope_codes",
    "compute_content_digest",
    "legacy_manifest_digest",
    "convert_legacy_version",
]

CHUNK_PARTITION_VERSION = "part-1"
MANIFEST_ALGORITHM_VERSION = 2
MAX_CHUNK_ROWS = 20_000
_VERSION_COLUMNS = (
    "version_id",
    "source_code",
    "scope",
    "categories",
    "quality_summary",
    "manifest_sha256",
    "rule_version",
    "status",
    "created_at",
    "published_at",
    "manifest_version",
    "member_storage",
    "member_count",
    "chunk_count",
    "content_digest",
    "retention_state",
    "window_mode",
    "window_start",
    "archive_id",
)


def _parts(logical_key: str) -> list[str]:
    return logical_key.split("|")


def partition_key_for(category: str, logical_key: str) -> str:
    """由逻辑键计算确定性分区键（part-1）。"""
    parts = _parts(logical_key)
    keys = parts[2:]
    if category == "calendar":
        # keys = exchange, cal_date
        return f"{keys[1][:4]}|{keys[0]}"
    if category == "security":
        return keys[0][:3]
    if category == "corporate_action":
        # keys = ts_code, end_date, div_proc
        return f"{keys[1][:4]}|{keys[0][:2]}"
    # daily / adj_factor / suspension: ts_code, trade_date[, event_type]
    return f"{keys[1]}|{keys[0][:2]}"


def _key_date(category: str, logical_key: str) -> str | None:
    parts = _parts(logical_key)
    keys = parts[2:]
    if category in {"daily", "adj_factor", "suspension"}:
        return keys[1]
    if category == "calendar":
        return keys[1]
    if category == "corporate_action":
        return keys[1]
    return None


def _warmup_start(window_start: str, warmup_days: int) -> str:
    """按自然日给出预热起点（20 交易日约 30 自然日，取保守放大）。"""
    from datetime import datetime, timedelta

    start = datetime.strptime(window_start, "%Y%m%d").date()
    return (start - timedelta(days=max(0, warmup_days) * 2)).strftime("%Y%m%d")


def _partition_date(category: str, partition_key: str) -> str:
    if category == "security":
        return "99999999"
    if category == "calendar":
        return partition_key.split("|", 1)[0] + "0101"
    if category == "corporate_action":
        return partition_key.split("|", 1)[0] + "0101"
    return partition_key.split("|", 1)[0]


def _partition_in_window(
    category: str,
    partition_key: str,
    *,
    window_start: str,
    warmup_start: str,
) -> bool:
    if category == "security":
        return True
    value = _partition_date(category, partition_key)
    if category == "calendar":
        return value[:4] >= str(int(window_start[:4]) - 1)
    if category == "corporate_action":
        return value[:4] >= str(int(window_start[:4]) - 1)
    return value >= warmup_start


def _member_in_window(
    category: str,
    logical_key: str,
    *,
    window_start: str,
    warmup_start: str,
) -> bool:
    if category == "security":
        return True
    value = _key_date(category, logical_key) or ""
    if category in {"calendar", "corporate_action"}:
        return value[:4] >= str(int(window_start[:4]) - 1)
    return value >= warmup_start


def _anchor_partitions(
    conn: sqlite3.Connection,
    source_code: str,
    category: str,
    window_start: str,
) -> list[str]:
    """每证券窗口前最后一条日线/因子锚点所在分区（有界：证券数）。"""
    if category not in {"daily", "adj_factor"}:
        return []
    from .records import spec_for

    spec = spec_for(category)
    rows = conn.execute(
        f"SELECT ts_code, MAX(trade_date) FROM {spec.table} WHERE source_code=?"
        " AND trade_date < ? GROUP BY ts_code",
        (source_code, window_start),
    ).fetchall()
    partitions: list[str] = []
    for ts_code, trade_date in rows:
        if ts_code and trade_date:
            partitions.append(f"{trade_date}|{str(ts_code)[:2]}")
    return sorted(set(partitions))


def _member_columns(category: str, logical_key: str) -> tuple[str | None, str | None]:
    parts = _parts(logical_key)
    keys = parts[2:]
    if category == "calendar":
        return None, keys[1]
    if category == "security":
        return keys[0], None
    if category == "corporate_action":
        return keys[0], keys[1]
    return keys[0], keys[1]


def _chunk_physical_digest(
    source_code: str, category: str, partition_key: str, entries: Sequence[tuple[str, int, str]]
) -> str:
    return sha256_of_object(
        [
            [source_code, category, CHUNK_PARTITION_VERSION, partition_key, key, record_id, digest]
            for key, record_id, digest in entries
        ]
    )


def _chunk_logical_digest(entries: Sequence[tuple[str, int, str]]) -> str:
    return sha256_of_object([[key, digest] for key, _record_id, digest in entries])


def build_chunk(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    category: str,
    members: Sequence[tuple[int, str, str]],
    first_version_id: str | None = None,
) -> str:
    """构建或复用分区不可变分块；返回 chunk_id。

    ``members`` 为 ``(record_id, logical_key, content_hash)``。
    """
    if not members:
        raise DataError("chunk_empty", "不能构建空分块")
    if len(members) > MAX_CHUNK_ROWS:
        raise DataError(
            "chunk_partition_overflow",
            f"分区成员 {len(members)} 行超过上限 {MAX_CHUNK_ROWS}；需要显式升级分区算法",
        )
    entries = sorted(
        ((str(key), int(record_id), str(digest)) for record_id, key, digest in members),
        key=lambda item: item[0],
    )
    partitions = {partition_key_for(category, key) for key, _rid, _digest in entries}
    if len(partitions) != 1:
        raise DataError("chunk_partition_conflict", "同一分块包含多个分区键")
    partition = partitions.pop()
    physical = _chunk_physical_digest(source_code, category, partition, entries)
    logical = _chunk_logical_digest(entries)
    chunk_id = "ch_" + physical[:16]
    row = conn.execute(
        "SELECT source_code, category, partition_algorithm, partition_key, physical_sha256,"
        " logical_sha256, row_count FROM data_chunk WHERE chunk_id=?",
        (chunk_id,),
    ).fetchone()
    if row is not None:
        if (
            str(row[0]) != source_code
            or str(row[1]) != category
            or str(row[2]) != CHUNK_PARTITION_VERSION
            or str(row[3]) != partition
            or str(row[4]) != physical
        ):
            raise DataError("chunk_id_collision", f"分块短 ID 冲突: {chunk_id}")
        return chunk_id

    byte_size = sum(len(key) + len(digest) + 48 for key, _rid, digest in entries)
    conn.execute(
        "INSERT INTO data_chunk(chunk_id, source_code, category, partition_algorithm,"
        " partition_key, logical_sha256, physical_sha256, row_count, byte_size, created_at,"
        " first_seen_version_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            source_code,
            category,
            CHUNK_PARTITION_VERSION,
            partition,
            logical,
            physical,
            len(entries),
            byte_size,
            utc_now_iso(),
            first_version_id,
        ),
    )
    rows = []
    for seq, (key, record_id, digest) in enumerate(entries):
        ts_code, biz_date = _member_columns(category, key)
        rows.append((chunk_id, seq, key, record_id, digest, ts_code, biz_date))
    conn.executemany(
        "INSERT INTO data_chunk_member(chunk_id, seq, logical_key, record_id, content_hash,"
        " ts_code, biz_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return chunk_id


def _partition_filter(category: str, partition_key: str) -> tuple[str, list[Any]]:
    if category == "calendar":
        year, exchange = partition_key.split("|", 1)
        return "(cal_date LIKE ? AND exchange=?)", [year + "%", exchange]
    if category == "security":
        return "ts_code LIKE ?", [partition_key + "%"]
    if category == "corporate_action":
        year, prefix = partition_key.split("|", 1)
        return "(end_date LIKE ? AND ts_code LIKE ?)", [year + "%", prefix + "%"]
    date, prefix = partition_key.split("|", 1)
    return "(trade_date=? AND ts_code LIKE ?)", [date, prefix + "%"]


def current_partition_members(
    conn: sqlite3.Connection,
    category: str,
    source_code: str,
    partition_key: str,
) -> list[tuple[int, str, str]]:
    """读取某分区当前最新修订的成员（有界：单分区）。"""
    spec = spec_for(category)
    where, params = _partition_filter(category, partition_key)
    key_join = " AND ".join(
        f"y.{field}=x.{field}" for field in spec.key_fields
    )
    select_keys = ", ".join(f"x.{field}" for field in spec.key_fields)
    query = (
        f"SELECT x.id, {select_keys}, x.content_hash FROM {spec.table} x"
        f" WHERE x.source_code=? AND {where}"
        f" AND x.revision_no = (SELECT MAX(y.revision_no) FROM {spec.table} y"
        f" WHERE y.source_code=x.source_code AND {key_join})"
    )
    rows = conn.execute(query, [source_code, *params]).fetchall()
    members: list[tuple[int, str, str]] = []
    for row in rows:
        keys = row[1 : 1 + len(spec.key_fields)]
        record = dict(zip(spec.key_fields, keys))
        members.append(
            (int(row[0]), make_logical_key(category, record, source_code), str(row[-1]))
        )
    return members


@dataclass(frozen=True)
class _VersionMeta:
    version_id: str
    source_code: str
    categories: str
    manifest_version: int
    member_storage: str
    retention_state: str
    window_mode: str
    window_start: str | None

    @property
    def category_list(self) -> list[str]:
        import json

        try:
            return list(json.loads(self.categories))
        except (TypeError, ValueError):
            return []


def _load_meta(conn: sqlite3.Connection, version_id: str) -> _VersionMeta:
    row = conn.execute(
        "SELECT version_id, source_code, categories, manifest_version, member_storage,"
        " retention_state, window_mode, window_start FROM data_version WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise DataError("version_not_found", f"数据版本 {version_id} 不存在")
    return _VersionMeta(
        version_id=str(row[0]),
        source_code=str(row[1]),
        categories=str(row[2]),
        manifest_version=int(row[3]),
        member_storage=str(row[4]),
        retention_state=str(row[5]),
        window_mode=str(row[6]),
        window_start=row[7],
    )


def version_refs(conn: sqlite3.Connection, version_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT vc.category, vc.chunk_id, vc.seq, c.partition_key, c.logical_sha256"
        " FROM data_version_chunk vc JOIN data_chunk c ON c.chunk_id=vc.chunk_id"
        " WHERE vc.version_id=? ORDER BY vc.category, c.partition_key, c.chunk_id",
        (version_id,),
    ).fetchall()
    return [
        {
            "category": str(row[0]),
            "chunk_id": str(row[1]),
            "seq": int(row[2]),
            "partition_key": str(row[3]),
            "logical_sha256": str(row[4]),
        }
        for row in rows
    ]


def iter_version_members(
    conn: sqlite3.Connection, version_id: str, *, batch_size: int = 5000
) -> Iterator[tuple[str, int, str, str]]:
    """流式产出 ``(category, record_id, content_hash, logical_key)``。"""
    meta = _load_meta(conn, version_id)
    if meta.retention_state == "archived":
        raise DataError(
            "version_archived", f"版本 {version_id} 已归档离线；请使用 restore --version-id 恢复"
        )
    if meta.member_storage == "legacy":
        cursor = conn.execute(
            "SELECT category, record_id, content_hash, logical_key FROM data_version_member"
            " WHERE version_id=? ORDER BY category, logical_key",
            (version_id,),
        )
    else:
        cursor = conn.execute(
            "SELECT vc.category, m.record_id, m.content_hash, m.logical_key"
            " FROM data_version_chunk vc JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
            " WHERE vc.version_id=? ORDER BY vc.category, m.logical_key",
            (version_id,),
        )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            yield (str(row[0]), int(row[1]), str(row[2]), str(row[3]))


def version_members(conn: sqlite3.Connection, version_id: str) -> list[tuple[str, int, str, str]]:
    return list(iter_version_members(conn, version_id))


def version_scope_codes(conn: sqlite3.Connection, version_id: str) -> list[str]:
    meta = _load_meta(conn, version_id)
    if meta.member_storage == "legacy":
        codes: set[str] = set()
        for category, _record_id, _digest, key in iter_version_members(conn, version_id):
            if category in {"daily", "adj_factor", "suspension", "corporate_action", "security"}:
                parts = key.split("|")
                if len(parts) >= 3:
                    codes.add(parts[2])
        return sorted(codes)
    rows = conn.execute(
        "SELECT DISTINCT m.ts_code FROM data_version_chunk vc"
        " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
        " WHERE vc.version_id=? AND m.ts_code IS NOT NULL"
        " AND vc.category IN ('daily','adj_factor','suspension','corporate_action','security')"
        " ORDER BY m.ts_code",
        (version_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def compute_content_digest(conn: sqlite3.Connection, version_id: str) -> str:
    """布局无关的逻辑内容摘要：全局 (category, logical_key, content_hash) 排序。

    流式重算时保留 canonical JSON 数组的括号与分隔符，与 ``sha256_of_object`` 一致。
    """
    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    for category, _record_id, content_hash, key in iter_version_members(conn, version_id):
        if not first:
            digest.update(b",")
        first = False
        digest.update(canonical_json_bytes([category, key, content_hash]))
    digest.update(b"]")
    return digest.hexdigest()


def legacy_manifest_digest(
    conn: sqlite3.Connection, version_id: str
) -> str:
    """按旧版顺序重算 v1 清单摘要，用于迁移核对（不改写存储值）。"""
    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    cursor = conn.execute(
        "SELECT category, record_id, content_hash, logical_key FROM data_version_member"
        " WHERE version_id=? ORDER BY category, logical_key",
        (version_id,),
    )
    for category, record_id, content_hash, key in cursor:
        if not first:
            digest.update(b",")
        first = False
        digest.update(
            canonical_json_bytes(
                {
                    "category": str(category),
                    "record_id": int(record_id),
                    "logical_key": str(key),
                    "content_hash": str(content_hash),
                }
            )
        )
    digest.update(b"]")
    return digest.hexdigest()


def _manifest_header(
    *,
    source_code: str,
    categories: Sequence[str],
    scope: Mapping[str, Any],
    window_mode: str,
    window_start: str | None,
    rule_version: str,
) -> dict[str, Any]:
    return {
        "manifest_algorithm": MANIFEST_ALGORITHM_VERSION,
        "source_code": source_code,
        "categories": sorted(categories),
        "scope": dict(scope),
        "window_mode": window_mode,
        "window_start": window_start,
        "rule_version": rule_version,
    }


def _write_refs(
    conn: sqlite3.Connection,
    version_id: str,
    refs: Iterable[tuple[str, str]],
) -> None:
    conn.executemany(
        "INSERT INTO data_version_chunk(version_id, category, chunk_id, seq) VALUES (?, ?, ?, 0)",
        [(version_id, category, chunk_id) for category, chunk_id in refs],
    )


def _recompute_ref_seq(conn: sqlite3.Connection, version_id: str) -> None:
    rows = conn.execute(
        "SELECT vc.rowid, vc.category, c.partition_key FROM data_version_chunk vc"
        " JOIN data_chunk c ON c.chunk_id=vc.chunk_id WHERE vc.version_id=?"
        " ORDER BY vc.category, c.partition_key, vc.chunk_id",
        (version_id,),
    ).fetchall()
    current_category = None
    seq = -1
    updates = []
    for rowid, category, _partition in rows:
        if category != current_category:
            current_category = category
            seq = 0
        else:
            seq += 1
        updates.append((seq, rowid))
    conn.executemany("UPDATE data_version_chunk SET seq=? WHERE rowid=?", updates)


def publish_chunked_version(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    scope: Mapping[str, Any],
    categories: Sequence[str],
    rule_version: str,
    quality_summary: Mapping[str, Any],
    members: Mapping[str, Sequence[tuple[int, str, str]]] | None = None,
    base_version_id: str | None = None,
    affected_partitions: Mapping[str, Sequence[str]] | None = None,
    window_mode: str = "full",
    window_start: str | None = None,
    categories_scope: Sequence[str] | None = None,
) -> dict[str, Any]:
    """发布一个分块版本。

    三种调用方式：
    1. ``members`` 显式给出（迁移/测试）：按分区建块；
    2. ``base_version_id`` + ``affected_partitions``：复制未变分区引用，重建变化分区；
    3. 两者皆无：逐分区扫描当前最新成员构建全量版本。
    """
    now = utc_now_iso()
    categories = sorted(set(categories))

    working = window_mode == "working" and window_start is not None
    warmup_start = _warmup_start(window_start, 20) if working else None

    def _in_window(category: str, key: str) -> bool:
        if not working:
            return True
        return _member_in_window(
            category, key, window_start=str(window_start), warmup_start=str(warmup_start)
        )

    built: dict[tuple[str, str], str] = {}
    if members is not None:
        for category in categories:
            grouped: dict[str, list[tuple[int, str, str]]] = {}
            for record_id, key, digest in members.get(category, []):
                if not _in_window(category, key):
                    continue
                grouped.setdefault(partition_key_for(category, key), []).append(
                    (record_id, key, digest)
                )
            for partition, entries in grouped.items():
                chunk_id = build_chunk(
                    conn,
                    source_code=source_code,
                    category=category,
                    members=entries,
                )
                built[(category, partition)] = chunk_id
        refs: list[tuple[str, str]] = sorted(
            (category, chunk_id) for (category, _partition), chunk_id in built.items()
        )
    else:
        affected = affected_partitions or {}
        refs = []
        if base_version_id is not None:
            # 先复制基线引用（排除变化分区）到本版本。
            exclude_sql = ""
            exclude_params: list[Any] = []
            for category, partitions in affected.items():
                if not partitions:
                    continue
                placeholders = ",".join("?" for _ in partitions)
                exclude_sql += (
                    " AND NOT (c.category=? AND c.partition_key IN (" + placeholders + "))"
                )
                exclude_params.extend([category, *partitions])
            rows = conn.execute(
                "SELECT vc.category, vc.chunk_id, c.partition_key FROM data_version_chunk vc"
                " JOIN data_chunk c ON c.chunk_id=vc.chunk_id"
                " WHERE vc.version_id=?" + exclude_sql,
                (base_version_id, *exclude_params),
            ).fetchall()
            for row in rows:
                category = str(row[0])
                if not working or _partition_in_window(
                    category,
                    str(row[2]),
                    window_start=str(window_start),
                    warmup_start=str(warmup_start),
                ):
                    refs.append((category, str(row[1])))
            for category, partitions in affected.items():
                for partition in partitions:
                    if working and not _partition_in_window(
                        category,
                        partition,
                        window_start=str(window_start),
                        warmup_start=str(warmup_start),
                    ):
                        continue
                    chunk_id = _build_partition_from_current(
                        conn, source_code, category, partition
                    )
                    if chunk_id is not None:
                        refs.append((category, chunk_id))
        else:
            for category in categories:
                for partition in _iter_current_partitions(conn, source_code, category):
                    if working and not _partition_in_window(
                        category,
                        partition,
                        window_start=str(window_start),
                        warmup_start=str(warmup_start),
                    ):
                        continue
                    chunk_id = _build_partition_from_current(
                        conn, source_code, category, partition
                    )
                    if chunk_id is not None:
                        refs.append((category, chunk_id))

    if working:
        # 窗口边界前每证券最后一条日线/因子锚点必须保留；行数 ≤ 证券数。
        for anchor_category in ("daily", "adj_factor"):
            existing_partitions = {
                str(
                    conn.execute(
                        "SELECT partition_key FROM data_chunk WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()[0]
                )
                for category, chunk_id in refs
                if category == anchor_category
            }
            for partition in _anchor_partitions(
                conn, source_code, anchor_category, str(window_start)
            ):
                if partition in existing_partitions:
                    continue
                chunk_id = _build_partition_from_current(
                    conn, source_code, anchor_category, partition
                )
                if chunk_id is not None:
                    refs.append((anchor_category, chunk_id))

    # 去重并检查同一版本内一个分区只有一个分块。
    refs = sorted(set(refs))
    actual_categories = sorted({category for category, _chunk_id in refs}) or categories
    header = _manifest_header(
        source_code=source_code,
        categories=actual_categories,
        scope=scope,
        window_mode=window_mode,
        window_start=window_start,
        rule_version=rule_version,
    )
    seen_partitions: set[tuple[str, str]] = set()
    for category, chunk_id in refs:
        row = conn.execute(
            "SELECT partition_key, logical_sha256 FROM data_chunk WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        if row is None:
            raise DataError("chunk_missing", f"分块 {chunk_id} 不存在")
        key = (category, str(row[0]))
        if key in seen_partitions:
            raise DataError("version_partition_duplicate", f"版本内分区重复: {key}")
        seen_partitions.add(key)

    entries = []
    for category, chunk_id in refs:
        row = conn.execute(
            "SELECT partition_key, logical_sha256 FROM data_chunk WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        entries.append(
            {
                "category": category,
                "partition_key": str(row[0]),
                "chunk_id": chunk_id,
                "logical_sha256": str(row[1]),
            }
        )
    entries.sort(key=lambda item: (item["category"], item["partition_key"], item["chunk_id"]))
    manifest = sha256_of_object({"header": header, "chunks": entries})
    version_id = "dv_" + manifest[:16]

    existing = conn.execute(
        "SELECT manifest_sha256, published_at, member_storage, member_count, retention_state"
        " FROM data_version WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != manifest:
            raise DataError("version_id_collision", f"版本 {version_id} 已存在但摘要不一致")
        # 合法的空版本（member_count=0）本来就没有分块引用；归档离线版本会主动删除
        # 引用。只有“应含成员却无引用且仍在线”的版本才是结构不一致。
        has_refs = conn.execute(
            "SELECT 1 FROM data_version_chunk WHERE version_id=? LIMIT 1", (version_id,)
        ).fetchone()
        if (
            str(existing[2]) == "chunked"
            and int(existing[3] or 0) > 0
            and str(existing[4]) != "archived"
            and not has_refs
        ):
            raise DataError("version_storage_inconsistent", f"版本 {version_id} 缺少分块引用")
        return {
            "version_id": version_id,
            "manifest_sha256": manifest,
            "manifest_version": MANIFEST_ALGORITHM_VERSION,
            "status": "published",
            "reused": True,
            "member_count": _count_members(conn, version_id, "chunked"),
            "chunk_count": len(entries),
            "published_at": existing[1],
        }

    with conn:
        conn.execute(
            "INSERT INTO data_version(version_id, source_code, scope, categories, quality_summary,"
            " manifest_sha256, rule_version, status, created_at, published_at, manifest_version,"
            " member_storage, member_count, chunk_count, content_digest, retention_state,"
            " window_mode, window_start, archive_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, 'chunked', ?, ?, NULL,"
            " 'active', ?, ?, NULL)",
            (
                version_id,
                source_code,
                _json_text(scope),
                _json_text(categories),
                _json_text(dict(quality_summary)),
                manifest,
                rule_version,
                now,
                now,
                MANIFEST_ALGORITHM_VERSION,
                sum(
                    int(
                        conn.execute(
                            "SELECT row_count FROM data_chunk WHERE chunk_id=?", (chunk_id,)
                        ).fetchone()[0]
                    )
                    for _category, chunk_id in refs
                ),
                len(entries),
                window_mode,
                window_start,
            ),
        )
        _write_refs(conn, version_id, refs)
        _recompute_ref_seq(conn, version_id)
        _stamp_first_version(conn, version_id, sorted(set(built.values())))
        _bump_generation(conn)
        _refresh_coverage_stats(conn, source_code, version_id)

    return {
        "version_id": version_id,
        "manifest_sha256": manifest,
        "manifest_version": MANIFEST_ALGORITHM_VERSION,
        "status": "published",
        "reused": False,
        "member_count": _count_members(conn, version_id, "chunked"),
        "chunk_count": len(entries),
        "published_at": now,
        "window_mode": window_mode,
        "window_start": window_start,
    }


def _build_partition_from_current(
    conn: sqlite3.Connection, source_code: str, category: str, partition: str
) -> str | None:
    members = current_partition_members(conn, category, source_code, partition)
    if not members:
        return None
    return build_chunk(conn, source_code=source_code, category=category, members=members)


def _iter_current_partitions(
    conn: sqlite3.Connection, source_code: str, category: str
) -> Iterator[str]:
    spec = spec_for(category)
    seen: set[str] = set()
    cursor = conn.execute(
        f"SELECT {', '.join(spec.key_fields)} FROM {spec.table} WHERE source_code=?"
        f" ORDER BY {', '.join(spec.key_fields)}",
        (source_code,),
    )
    for row in cursor:
        record = dict(zip(spec.key_fields, row))
        key = make_logical_key(category, record, source_code)
        partition = partition_key_for(category, key)
        if partition not in seen:
            seen.add(partition)
            yield partition


def _stamp_first_version(
    conn: sqlite3.Connection,
    version_id: str,
    chunk_ids: Sequence[str],
) -> None:
    if not chunk_ids:
        return
    by_table: dict[str, list[tuple[str, int]]] = {}
    for start in range(0, len(chunk_ids), 400):
        batch = chunk_ids[start : start + 400]
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT c.category, m.record_id FROM data_chunk_member m"
            f" JOIN data_chunk c ON c.chunk_id=m.chunk_id"
            f" WHERE m.chunk_id IN ({placeholders})",
            batch,
        ).fetchall()
        for category, record_id in rows:
            by_table.setdefault(spec_for(str(category)).table, []).append(
                (version_id, int(record_id))
            )
    for table, params in by_table.items():
        conn.executemany(
            f"UPDATE {table} SET first_version_id=? WHERE id=? AND first_version_id IS NULL",
            params,
        )


def _count_members(conn: sqlite3.Connection, version_id: str, storage: str) -> int:
    if storage == "legacy":
        row = conn.execute(
            "SELECT COUNT(*) FROM data_version_member WHERE version_id=?", (version_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM data_version_chunk vc JOIN data_chunk_member m"
            " ON m.chunk_id=vc.chunk_id WHERE vc.version_id=?",
            (version_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _json_text(payload: Any) -> str:
    return canonical_json_bytes(payload).decode("utf-8")


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE maintenance_generation SET generation=generation+1, updated_at=?"
        " WHERE id=1",
        (utc_now_iso(),),
    )


def _refresh_coverage_stats(
    conn: sqlite3.Connection, source_code: str, version_id: str | None
) -> None:
    """发布时只做轻量标记：计数由写入事务增量维护，避免每次发布全表扫描。"""
    conn.execute(
        "UPDATE coverage_stats SET computed_from_version_id=?, computed_at=?, stale=0"
        " WHERE source_code=?",
        (version_id, utc_now_iso(), source_code),
    )


def _date_field(category: str) -> str | None:
    if category == "calendar":
        return "cal_date"
    if category == "corporate_action":
        return "end_date"
    if category == "security":
        return None
    return "trade_date"


def convert_legacy_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    """把旧全量成员转换为分块存储；不改 version_id/manifest_version/manifest_sha256。"""
    row = conn.execute(
        "SELECT version_id, source_code, categories, manifest_sha256, member_storage,"
        " manifest_version FROM data_version WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise DataError("version_not_found", f"数据版本 {version_id} 不存在")
    if str(row[4]) == "chunked":
        return {"version_id": version_id, "converted": False, "reason": "already_chunked"}
    source_code = str(row[1])
    import json

    categories = list(json.loads(row[2]))
    legacy = conn.execute(
        "SELECT category, record_id, content_hash, logical_key FROM data_version_member"
        " WHERE version_id=? ORDER BY category, logical_key",
        (version_id,),
    ).fetchall()

    legacy_digest = legacy_manifest_digest(conn, version_id)
    if legacy_digest != str(row[3]):
        raise DataError(
            "legacy_manifest_mismatch",
            f"版本 {version_id} 旧成员重算摘要与保存值不一致，拒绝转换",
        )

    grouped: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for category, record_id, content_hash, key in legacy:
        partition = partition_key_for(str(category), str(key))
        grouped.setdefault((str(category), partition), []).append(
            (int(record_id), str(key), str(content_hash))
        )
    expected_members = [
        (str(category), int(record_id), str(content_hash), str(key))
        for category, record_id, content_hash, key in legacy
    ]
    with conn:
        # 保存本轮 build_chunk 的返回值；同一分区可能同时存在多个旧版本的
        # 不同修订块，绝不能按分区“任取一个”作为本版本引用。
        built: dict[tuple[str, str], str] = {}
        for (category, partition), entries in grouped.items():
            built[(category, partition)] = build_chunk(
                conn,
                source_code=source_code,
                category=category,
                members=entries,
                first_version_id=version_id,
            )
        refs = sorted({(category, chunk_id) for (category, _p), chunk_id in built.items()})
        _write_refs(conn, version_id, refs)
        _recompute_ref_seq(conn, version_id)
        conn.execute(
            "UPDATE data_version SET member_storage='chunked', member_count=?, chunk_count=?"
            " WHERE version_id=?",
            (len(legacy), len(refs), version_id),
        )
        actual_members = version_members(conn, version_id)
        if actual_members != expected_members:
            raise DataError(
                "legacy_conversion_mismatch",
                f"版本 {version_id} 分块成员与旧成员不一致，拒绝转换",
            )
        conn.execute(
            "UPDATE data_version SET content_digest=? WHERE version_id=?",
            (compute_content_digest(conn, version_id), version_id),
        )
        _bump_generation(conn)
    return {"version_id": version_id, "converted": True, "chunks": len(refs)}


class VersionView:
    """固定版本读取视图：只使用该版本成员的 record_id。

    ``expected_source_code`` 用于把调用方声明的来源与版本的真实来源绑定：
    两者不一致时立即拒绝，绝不以调用方声明覆盖版本来源，也不回退到最新数据。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        *,
        expected_source_code: str | None = None,
    ) -> None:
        self.conn = conn
        self.meta = _load_meta(conn, version_id)
        self.version_id = version_id
        if self.meta.retention_state == "archived":
            raise DataError(
                "version_archived",
                f"版本 {version_id} 已归档离线；请先 restore --version-id",
            )
        if expected_source_code is not None and self.meta.source_code != expected_source_code:
            raise DataError(
                "version_source_mismatch",
                f"数据版本 {version_id} 属于来源 {self.meta.source_code}，"
                f"与请求来源 {expected_source_code} 不一致",
            )

    @property
    def source_code(self) -> str:
        return self.meta.source_code

    @property
    def chunked(self) -> bool:
        return self.meta.member_storage == "chunked"

    def _legacy_filter(self, category: str) -> tuple[str, list[Any]]:
        return (
            "id IN (SELECT record_id FROM data_version_member"
            " WHERE version_id=? AND category=?)",
            [self.version_id, category],
        )

    def _join(self, category: str, table: str, columns: str) -> tuple[str, list[Any]]:
        """分块版本：成员表 → 记录表连接；返回 (FROM/WHERE 片段, 参数)。"""
        return (
            f" JOIN data_version_chunk vc ON vc.version_id=?"
            f" AND vc.category='{category}'"
            f" JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
            f" JOIN {table} d ON d.id=m.record_id",
            [self.version_id],
        )

    def daily_on(self, ts_code: str, trade_date: str):
        if self.chunked:
            return self.conn.execute(
                "SELECT d.* FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN daily_price d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='daily' AND m.ts_code=? AND m.biz_date=?",
                (self.version_id, ts_code, trade_date),
            ).fetchone()
        clause, params = self._legacy_filter("daily")
        return self.conn.execute(
            f"SELECT * FROM daily_price WHERE source_code=? AND ts_code=? AND trade_date=?"
            f" AND {clause}",
            [self.meta.source_code, ts_code, trade_date, *params],
        ).fetchone()

    def latest_daily(self, ts_code: str, *, on_or_before: str | None = None):
        if self.chunked:
            query = (
                "SELECT d.* FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN daily_price d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='daily' AND m.ts_code=?"
            )
            values: list[Any] = [self.version_id, ts_code]
            if on_or_before:
                query += " AND m.biz_date<=?"
                values.append(on_or_before)
            query += " ORDER BY m.biz_date DESC LIMIT 1"
            return self.conn.execute(query, values).fetchone()
        clause, params = self._legacy_filter("daily")
        query = f"SELECT * FROM daily_price WHERE source_code=? AND ts_code=? AND {clause}"
        values = [self.meta.source_code, ts_code, *params]
        if on_or_before:
            query += " AND trade_date<=?"
            values.append(on_or_before)
        query += " ORDER BY trade_date DESC LIMIT 1"
        return self.conn.execute(query, values).fetchone()

    def factor_on(self, ts_code: str, trade_date: str) -> float | None:
        if self.chunked:
            row = self.conn.execute(
                "SELECT d.adj_factor FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN adj_factor d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='adj_factor' AND m.ts_code=?"
                " AND m.biz_date=?",
                (self.version_id, ts_code, trade_date),
            ).fetchone()
        else:
            clause, params = self._legacy_filter("adj_factor")
            row = self.conn.execute(
                f"SELECT adj_factor FROM adj_factor WHERE source_code=? AND ts_code=?"
                f" AND trade_date=? AND {clause}",
                [self.meta.source_code, ts_code, trade_date, *params],
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def factor_series(self, ts_code: str, *, on_or_before: str) -> list[tuple[str, float]]:
        if self.chunked:
            rows = self.conn.execute(
                "SELECT m.biz_date, d.adj_factor FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN adj_factor d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='adj_factor' AND m.ts_code=?"
                " AND m.biz_date<=? ORDER BY m.biz_date",
                (self.version_id, ts_code, on_or_before),
            ).fetchall()
        else:
            clause, params = self._legacy_filter("adj_factor")
            rows = self.conn.execute(
                f"SELECT trade_date, adj_factor FROM adj_factor WHERE source_code=? AND ts_code=?"
                f" AND trade_date<=? AND {clause} ORDER BY trade_date",
                [self.meta.source_code, ts_code, on_or_before, *params],
            ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows if row[1] is not None]

    def is_suspended(self, ts_code: str, trade_date: str) -> bool:
        if self.chunked:
            rows = self.conn.execute(
                "SELECT d.event_type, d.detail FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN suspension_event d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='suspension' AND m.ts_code=?"
                " AND m.biz_date=?",
                (self.version_id, ts_code, trade_date),
            ).fetchall()
        else:
            clause, params = self._legacy_filter("suspension")
            rows = self.conn.execute(
                f"SELECT event_type, detail FROM suspension_event WHERE source_code=? AND ts_code=?"
                f" AND trade_date=? AND {clause}",
                [self.meta.source_code, ts_code, trade_date, *params],
            ).fetchall()
        return any(str(row[0]) == "S" and not row[1] for row in rows)

    def is_trading_day(self, trade_date: str) -> bool | None:
        if self.chunked:
            row = self.conn.execute(
                "SELECT d.is_open FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN trade_calendar d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='calendar' AND m.biz_date=? LIMIT 1",
                (self.version_id, trade_date),
            ).fetchone()
        else:
            clause, params = self._legacy_filter("calendar")
            row = self.conn.execute(
                f"SELECT is_open FROM trade_calendar WHERE source_code=? AND cal_date=?"
                f" AND {clause} LIMIT 1",
                [self.meta.source_code, trade_date, *params],
            ).fetchone()
        if row is None:
            return None
        return int(row[0]) == 1

    def history_length(self, ts_code: str, *, on_or_before: str | None = None) -> int:
        """固定版本内该证券的历史长度。

        ``on_or_before`` 按请求的截止时点截断（含边界日）；不截断会让
        ``as_of`` 之后的成员补足历史长度，造成时点泄漏。
        """
        if self.chunked:
            query = (
                "SELECT COUNT(*) FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " WHERE vc.version_id=? AND vc.category='daily' AND m.ts_code=?"
            )
            values: list[Any] = [self.version_id, ts_code]
            if on_or_before:
                query += " AND m.biz_date<=?"
                values.append(on_or_before)
            row = self.conn.execute(query, values).fetchone()
        else:
            clause, params = self._legacy_filter("daily")
            query = (
                f"SELECT COUNT(*) FROM daily_price WHERE source_code=? AND ts_code=? AND {clause}"
            )
            values = [self.meta.source_code, ts_code, *params]
            if on_or_before:
                query += " AND trade_date<=?"
                values.append(on_or_before)
            row = self.conn.execute(query, values).fetchone()
        return int(row[0]) if row else 0

    def latest_data_date(self, ts_codes: Sequence[str]) -> str | None:
        if not ts_codes:
            return None
        if self.chunked:
            placeholders = ", ".join("?" for _ in ts_codes)
            row = self.conn.execute(
                f"SELECT MAX(m.biz_date) FROM data_version_chunk vc"
                f" JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                f" WHERE vc.version_id=? AND vc.category='daily'"
                f" AND m.ts_code IN ({placeholders})",
                [self.version_id, *ts_codes],
            ).fetchone()
        else:
            clause, params = self._legacy_filter("daily")
            placeholders = ", ".join("?" for _ in ts_codes)
            row = self.conn.execute(
                f"SELECT MAX(trade_date) FROM daily_price WHERE source_code=?"
                f" AND ts_code IN ({placeholders}) AND {clause}",
                [self.meta.source_code, *ts_codes, *params],
            ).fetchone()
        return row[0] if row and row[0] else None

    def daily_series(self, ts_code: str, *, on_or_before: str):
        if self.chunked:
            return self.conn.execute(
                "SELECT d.* FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN daily_price d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='daily' AND m.ts_code=?"
                " AND m.biz_date<=? ORDER BY m.biz_date",
                (self.version_id, ts_code, on_or_before),
            ).fetchall()
        clause, params = self._legacy_filter("daily")
        return self.conn.execute(
            f"SELECT * FROM daily_price WHERE source_code=? AND ts_code=?"
            f" AND trade_date<=? AND {clause} ORDER BY trade_date",
            [self.meta.source_code, ts_code, on_or_before, *params],
        ).fetchall()

    def securities(self) -> list[sqlite3.Row]:
        if self.chunked:
            return self.conn.execute(
                "SELECT d.* FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN security_info d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='security' ORDER BY d.ts_code",
                (self.version_id,),
            ).fetchall()
        clause, params = self._legacy_filter("security")
        return self.conn.execute(
            f"SELECT * FROM security_info WHERE source_code=? AND {clause} ORDER BY ts_code",
            [self.meta.source_code, *params],
        ).fetchall()

    def calendar_days(self, *, on_or_before: str | None = None) -> list[str]:
        if self.chunked:
            query = (
                "SELECT d.cal_date FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN trade_calendar d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='calendar' AND d.is_open=1"
            )
            values: list[Any] = [self.version_id]
            if on_or_before:
                query += " AND m.biz_date<=?"
                values.append(on_or_before)
            query += " ORDER BY d.cal_date"
            return [str(row[0]) for row in self.conn.execute(query, values).fetchall()]
        clause, params = self._legacy_filter("calendar")
        query = f"SELECT cal_date FROM trade_calendar WHERE source_code=? AND is_open=1 AND {clause}"
        values = [self.meta.source_code, *params]
        if on_or_before:
            query += " AND cal_date<=?"
            values.append(on_or_before)
        query += " ORDER BY cal_date"
        return [str(row[0]) for row in self.conn.execute(query, values).fetchall()]

    def suspension_rows(self) -> list[tuple[Any, Any, Any, Any]]:
        if self.chunked:
            rows = self.conn.execute(
                "SELECT d.ts_code, d.trade_date, d.event_type, d.detail"
                " FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN suspension_event d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='suspension'",
                (self.version_id,),
            ).fetchall()
        else:
            clause, params = self._legacy_filter("suspension")
            rows = self.conn.execute(
                f"SELECT ts_code, trade_date, event_type, detail FROM suspension_event"
                f" WHERE source_code=? AND {clause}",
                [self.meta.source_code, *params],
            ).fetchall()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    def corporate_action_count(self) -> int:
        if self.chunked:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " WHERE vc.version_id=? AND vc.category='corporate_action'",
                (self.version_id,),
            ).fetchone()
        else:
            clause, params = self._legacy_filter("corporate_action")
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM corporate_action WHERE source_code=? AND {clause}",
                [self.meta.source_code, *params],
            ).fetchone()
        return int(row[0]) if row else 0

    def suspension_dates(self, ts_code: str) -> set[str]:
        if self.chunked:
            rows = self.conn.execute(
                "SELECT d.trade_date, d.event_type, d.detail FROM data_version_chunk vc"
                " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
                " JOIN suspension_event d ON d.id=m.record_id"
                " WHERE vc.version_id=? AND vc.category='suspension' AND m.ts_code=?",
                (self.version_id, ts_code),
            ).fetchall()
        else:
            clause, params = self._legacy_filter("suspension")
            rows = self.conn.execute(
                f"SELECT trade_date, event_type, detail FROM suspension_event WHERE source_code=?"
                f" AND ts_code=? AND {clause}",
                [self.meta.source_code, ts_code, *params],
            ).fetchall()
        return {str(row[0]) for row in rows if str(row[1]) == "S" and not row[2]}


__all__ += ["hash_bytes"]
