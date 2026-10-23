"""规范化记录的修订写入与数据版本清单。

写入语义：

- 逻辑键 = 数据源 + 类别 + 键字段；同一逻辑键的**内容相同**不新增记录（幂等）；
- 内容不同则追加新的 ``revision_no``，旧修订原样保留；
- 业务内容摘要只覆盖业务字段，请求时间或导入日志变化不产生新修订；
- 数据版本清单固化“本版本包含哪些记录的哪个修订”，新版本只保存分块引用；
- 已发布版本不可变：重复发布同一内容不会改写既有清单与时间戳。

有界性：写入只查询本批逻辑键的最新修订，不扫描来源该类别全部历史。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .chunks import (
    MANIFEST_ALGORITHM_VERSION,
    compute_content_digest,
    convert_legacy_version,
    iter_version_members,
    partition_key_for,
    publish_chunked_version,
    version_members,
)
from .digests import canonical_json_bytes, hash_bytes, sha256_of_object
from .records import (
    ALL_CATEGORIES,
    logical_key,
    record_content_hash,
    record_values,
    revision_metadata,
    spec_for,
)
from .timeutil import utc_now_iso

__all__ = [
    "WriteOutcome",
    "write_records",
    "latest_record_rows",
    "record_counts",
    "coverage_summary",
    "publish_data_version",
    "published_versions",
    "version_members",
    "version_count",
    "update_coverage_stats",
    "bump_generation",
]

MAX_KEYS_PER_QUERY = 400
MAX_SQL_PARAMS = 900


@dataclass
class WriteOutcome:
    """一次写入的统计。"""

    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    skipped: int = 0
    record_ids: list[int] = field(default_factory=list)
    logical_keys: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    affected_partitions: set[tuple[str, str]] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.inserted + self.revised + self.unchanged + self.skipped

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "revised": self.revised,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "total": self.total,
        }


def _chunks(sequence: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(sequence), size):
        yield sequence[start : start + size]


def _select_latest_for_keys(
    conn: sqlite3.Connection, category: str, source_code: str, keys: Sequence[tuple[str, ...]]
) -> dict[str, dict[str, Any]]:
    """只查询本批逻辑键的最新修订。"""
    if not keys:
        return {}
    spec = spec_for(category)
    result: dict[str, dict[str, Any]] = {}
    key_fields = spec.key_fields
    per_query = min(MAX_KEYS_PER_QUERY, MAX_SQL_PARAMS // max(1, len(key_fields)))
    for batch in _chunks(list(keys), per_query):
        if len(key_fields) == 1:
            placeholders = ", ".join("?" for _ in batch)
            query = (
                f"SELECT id, revision_no, content_hash, {key_fields[0]} FROM {spec.table}"
                f" WHERE source_code=? AND {key_fields[0]} IN ({placeholders})"
                f" AND revision_no = (SELECT MAX(revision_no) FROM {spec.table} t2"
                f" WHERE t2.source_code={spec.table}.source_code"
                f" AND t2.{key_fields[0]}={spec.table}.{key_fields[0]})"
            )
            params: list[Any] = [source_code, *[key[0] for key in batch]]
        else:
            row_placeholders = ", ".join(
                "(" + ", ".join("?" for _ in key_fields) + ")" for _ in batch
            )
            key_match = " AND ".join(
                f"t2.{field}={spec.table}.{field}" for field in key_fields
            )
            query = (
                f"SELECT id, revision_no, content_hash, {', '.join(key_fields)}"
                f" FROM {spec.table}"
                f" WHERE source_code=? AND ({', '.join(key_fields)}) IN ({row_placeholders})"
                f" AND revision_no = (SELECT MAX(revision_no) FROM {spec.table} t2"
                f" WHERE t2.source_code={spec.table}.source_code AND {key_match})"
            )
            params = [source_code, *[value for key in batch for value in key]]
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            key = "|".join(str(row[3 + index]) for index in range(len(key_fields)))
            result[key] = {
                "id": int(row[0]),
                "revision_no": int(row[1]),
                "content_hash": str(row[2]),
            }
    return result


def write_records(
    conn: sqlite3.Connection,
    category: str,
    records: Sequence[Mapping[str, Any]],
    *,
    source_code: str,
    batch_id: str | None = None,
) -> WriteOutcome:
    """把规范化记录写入修订表；内容相同的重复记录不新增行。"""
    spec = spec_for(category)
    outcome = WriteOutcome()
    if not records:
        return outcome

    # 批内按逻辑键去重；冲突键保留第一条并记录，不静默覆盖。
    unique: dict[str, Mapping[str, Any]] = {}
    for record in records:
        key_values = tuple(str(record.get(field, "")) for field in spec.key_fields)
        key = "|".join(key_values)
        if key in unique:
            if record_content_hash(category, record) != record_content_hash(category, unique[key]):
                outcome.conflicts.append(logical_key(category, record, source_code))
            outcome.skipped += 1
            continue
        unique[key] = record

    keys = [tuple(str(record.get(field, "")) for field in spec.key_fields) for record in unique.values()]
    existing = _select_latest_for_keys(conn, category, source_code, keys)
    metadata = revision_metadata(source_code, batch_id=batch_id)
    insert_columns = list(spec.key_fields) + list(spec.content_fields)
    placeholders = ", ".join("?" for _ in insert_columns)
    column_sql = ", ".join(
        insert_columns
        + [
            "source_code",
            "revision_no",
            "content_hash",
            "source_published_at",
            "historical_available_at",
            "observed_at",
            "ingested_at",
            "first_version_id",
            "batch_id",
        ]
    )
    values_sql = ", ".join("?" for _ in range(len(insert_columns) + 9))

    with conn:  # 单个事务：要么整批写入，要么整批回滚。
        for record in unique.values():
            key_values = tuple(str(record.get(field, "")) for field in spec.key_fields)
            key = "|".join(key_values)
            content = record_content_hash(category, record)
            previous = existing.get(key)
            if previous is not None and previous["content_hash"] == content:
                outcome.unchanged += 1
                outcome.record_ids.append(int(previous["id"]))
                outcome.logical_keys.append(logical_key(category, record, source_code))
                continue

            values = list(key_values) + list(record_values(category, record))
            if previous is None:
                revision_no = 1
                outcome.inserted += 1
            else:
                revision_no = int(previous["revision_no"]) + 1
                outcome.revised += 1
            values.extend(
                [
                    source_code,
                    revision_no,
                    content,
                    metadata["source_published_at"],
                    metadata["historical_available_at"],
                    metadata["observed_at"],
                    metadata["ingested_at"],
                    None,
                    batch_id,
                ]
            )
            cursor = conn.execute(
                f"INSERT INTO {spec.table}({column_sql}) VALUES ({values_sql})", values
            )
            new_id = int(cursor.lastrowid)
            outcome.record_ids.append(new_id)
            outcome.logical_keys.append(logical_key(category, record, source_code))
            outcome.affected_partitions.add(
                (category, partition_key_for(category, logical_key(category, record, source_code)))
            )
            existing[key] = {
                "id": new_id,
                "revision_no": revision_no,
                "content_hash": content,
            }
        _increment_coverage_stats(conn, category, source_code, outcome, unique.values())
        bump_generation(conn)
    return outcome


def _increment_coverage_stats(
    conn: sqlite3.Connection,
    category: str,
    source_code: str,
    outcome: WriteOutcome,
    records: Iterable[Mapping[str, Any]],
) -> None:
    if outcome.inserted == 0 and outcome.revised == 0:
        return
    date_field = _date_field(category)
    new_dates: list[str] = []
    if date_field:
        for record in records:
            value = str(record.get(date_field) or "")
            if value:
                new_dates.append(value)
    start = min(new_dates) if new_dates else None
    end = max(new_dates) if new_dates else None
    now = utc_now_iso()
    row = conn.execute(
        "SELECT 1 FROM coverage_stats WHERE source_code=? AND category=?",
        (source_code, category),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO coverage_stats(source_code, category, logical_count, revision_rows,"
            " start_date, end_date, computed_from_version_id, computed_at, stale)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1)",
            (
                source_code,
                category,
                int(outcome.inserted),
                int(outcome.inserted + outcome.revised),
                start,
                end,
                now,
            ),
        )
        return
    conn.execute(
        "UPDATE coverage_stats SET logical_count=logical_count+?, revision_rows=revision_rows+?,"
        " start_date=CASE WHEN ? IS NULL THEN start_date"
        "   WHEN start_date IS NULL OR ? < start_date THEN ? ELSE start_date END,"
        " end_date=CASE WHEN ? IS NULL THEN end_date"
        "   WHEN end_date IS NULL OR ? > end_date THEN ? ELSE end_date END,"
        " computed_at=?, stale=0 WHERE source_code=? AND category=?",
        (
            int(outcome.inserted),
            int(outcome.inserted + outcome.revised),
            start,
            start,
            start,
            end,
            end,
            end,
            now,
            source_code,
            category,
        ),
    )


def latest_record_rows(
    conn: sqlite3.Connection,
    category: str,
    source_code: str,
    *,
    ts_codes: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[sqlite3.Row]:
    """读取某类别每个逻辑键的最新修订（可按证券与日期区间过滤）。"""
    spec = spec_for(category)
    date_field = _date_field(category)
    query = (
        f"SELECT * FROM {spec.table} WHERE id IN ("
        f"  SELECT MAX(id) FROM {spec.table} WHERE source_code=?"
    )
    params: list[Any] = [source_code]
    if ts_codes:
        placeholders = ", ".join("?" for _ in ts_codes)
        query += f" AND ts_code IN ({placeholders})"
        params.extend(ts_codes)
    if date_field and start:
        query += f" AND {date_field}>=?"
        params.append(start)
    if date_field and end:
        query += f" AND {date_field}<=?"
        params.append(end)
    query += " GROUP BY " + ", ".join(spec.key_fields) + ")"
    return conn.execute(query, params).fetchall()


def _date_field(category: str) -> str | None:
    if category == "calendar":
        return "cal_date"
    if category == "corporate_action":
        return "end_date"
    if category == "security":
        return None
    return "trade_date"


def record_counts(conn: sqlite3.Connection, source_code: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for category in ALL_CATEGORIES:
        spec = spec_for(category)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {spec.table} WHERE source_code=?", (source_code,)
        ).fetchone()
        counts[category] = int(row[0]) if row else 0
    return counts


def _logical_count(conn: sqlite3.Connection, category: str, source_code: str) -> int:
    spec = spec_for(category)
    group = ", ".join(spec.key_fields)
    row = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {spec.table} WHERE source_code=?"
        f" GROUP BY {group})",
        (source_code,),
    ).fetchone()
    return int(row[0]) if row else 0


def update_coverage_stats(
    conn: sqlite3.Connection, source_code: str, *, version_id: str | None = None
) -> None:
    """重算并持久化逻辑/修订计数（显式维护操作或发布事务内）。"""
    now = utc_now_iso()
    for category in ALL_CATEGORIES:
        spec = spec_for(category)
        date_field = _date_field(category)
        logical = _logical_count(conn, category, source_code)
        revision_rows = conn.execute(
            f"SELECT COUNT(*) FROM {spec.table} WHERE source_code=?", (source_code,)
        ).fetchone()[0]
        if date_field:
            latest_where = " AND ".join(
                f"t2.{field}={spec.table}.{field}" for field in spec.key_fields
            )
            row = conn.execute(
                f"SELECT MIN({date_field}), MAX({date_field}) FROM {spec.table}"
                f" WHERE source_code=? AND revision_no=("
                f" SELECT MAX(revision_no) FROM {spec.table} t2 WHERE t2.source_code=?"
                f" AND {latest_where})",
                (source_code, source_code),
            ).fetchone()
            start, end = row[0], row[1]
        else:
            start = end = None
        conn.execute(
            "INSERT INTO coverage_stats(source_code, category, logical_count, revision_rows,"
            " start_date, end_date, computed_from_version_id, computed_at, stale)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)"
            " ON CONFLICT(source_code, category) DO UPDATE SET logical_count=excluded.logical_count,"
            " revision_rows=excluded.revision_rows, start_date=excluded.start_date,"
            " end_date=excluded.end_date, computed_from_version_id=excluded.computed_from_version_id,"
            " computed_at=excluded.computed_at, stale=0",
            (source_code, category, int(logical), int(revision_rows), start, end, version_id, now),
        )


def coverage_summary(conn: sqlite3.Connection, source_code: str) -> dict[str, dict[str, Any]]:
    """各类别的覆盖区间与计数。

    优先读取持久摘要（``coverage_stats``）；摘要缺失时回退为一次只读统计，
    并明确返回 ``stale`` 标记。``count`` 与 ``logical_count`` 是逻辑记录数，
    ``revision_rows`` 是物理修订行数，二者不混用。
    """
    summary: dict[str, dict[str, Any]] = {}
    stats_rows = conn.execute(
        "SELECT category, logical_count, revision_rows, start_date, end_date, stale,"
        " computed_at FROM coverage_stats WHERE source_code=?",
        (source_code,),
    ).fetchall()
    stats = {
        str(row[0]): {
            "count": int(row[1]),
            "logical_count": int(row[1]),
            "revision_rows": int(row[2]),
            "start": row[3],
            "end": row[4],
            "stale": bool(row[5]),
            "computed_at": row[6],
            "source": "coverage_stats",
        }
        for row in stats_rows
    }
    for category in ALL_CATEGORIES:
        if category in stats:
            summary[category] = stats[category]
            continue
        # 摘要缺失时明确返回 unknown/stale，由显式 `coverage --refresh` 维护，
        # 不在状态请求里现场扫描全历史（P2-F004 / C1 有界约定）。
        summary[category] = {
            "count": None,
            "logical_count": None,
            "revision_rows": None,
            "start": None,
            "end": None,
            "stale": True,
            "computed_at": None,
            "source": "unknown",
        }
    return summary


def bump_generation(conn: sqlite3.Connection) -> None:
    """推进维护代际：所有相关写事务都调用，供清理 preview 失效校验。"""
    conn.execute(
        "UPDATE maintenance_generation SET generation=generation+1, updated_at=? WHERE id=1",
        (utc_now_iso(),),
    )


def publish_data_version(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    scope: Mapping[str, Any],
    categories: Sequence[str],
    rule_version: str,
    quality_summary: Mapping[str, Any],
    members: Mapping[str, Sequence[tuple[int, str, str]]],
    window_mode: str = "full",
    window_start: str | None = None,
) -> dict[str, Any]:
    """固化一个数据版本（保留旧入参：显式成员按分区写入分块）。"""
    return publish_chunked_version(
        conn,
        source_code=source_code,
        scope=scope,
        categories=categories,
        rule_version=rule_version,
        quality_summary=quality_summary,
        members=members,
        window_mode=window_mode,
        window_start=window_start,
    )


def published_versions(
    conn: sqlite3.Connection, *, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    query = (
        "SELECT version_id, source_code, scope, categories, quality_summary, manifest_sha256,"
        " rule_version, status, created_at, published_at, manifest_version, member_storage,"
        " member_count, chunk_count, retention_state, window_mode, window_start, archive_id"
        " FROM data_version ORDER BY rowid"
    )
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = conn.execute(query, params).fetchall()
    versions: list[dict[str, Any]] = []
    for row in rows:
        versions.append(
            {
                "version_id": row[0],
                "source_code": row[1],
                "scope": row[2],
                "categories": row[3],
                "quality_summary": row[4],
                "manifest_sha256": row[5],
                "rule_version": row[6],
                "status": row[7],
                "created_at": row[8],
                "published_at": row[9],
                "manifest_version": row[10],
                "member_storage": row[11],
                "member_count": row[12],
                "chunk_count": row[13],
                "retention_state": row[14],
                "window_mode": row[15],
                "window_start": row[16],
                "archive_id": row[17],
            }
        )
    return versions


def latest_active_version_id(conn: sqlite3.Connection, source_code: str) -> str | None:
    row = conn.execute(
        "SELECT version_id FROM data_version WHERE source_code=? AND retention_state='active'"
        " ORDER BY rowid DESC LIMIT 1",
        (source_code,),
    ).fetchone()
    return str(row[0]) if row else None


def latest_versions(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """最近 N 个版本（有界），按时间升序返回便于状态页展示。"""
    rows = conn.execute(
        "SELECT version_id, source_code, scope, categories, quality_summary, manifest_sha256,"
        " rule_version, status, created_at, published_at, manifest_version, member_storage,"
        " member_count, chunk_count, retention_state, window_mode, window_start, archive_id"
        " FROM data_version ORDER BY rowid DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    versions = []
    for row in reversed(rows):
        versions.append(
            {
                "version_id": row[0],
                "source_code": row[1],
                "scope": row[2],
                "categories": row[3],
                "quality_summary": row[4],
                "manifest_sha256": row[5],
                "rule_version": row[6],
                "status": row[7],
                "created_at": row[8],
                "published_at": row[9],
                "manifest_version": row[10],
                "member_storage": row[11],
                "member_count": row[12],
                "chunk_count": row[13],
                "retention_state": row[14],
                "window_mode": row[15],
                "window_start": row[16],
                "archive_id": row[17],
            }
        )
    return versions


def version_count(conn: sqlite3.Connection, *, include_retired: bool = True) -> int:
    query = "SELECT COUNT(*) FROM data_version"
    if not include_retired:
        query += " WHERE retention_state='active'"
    return int(conn.execute(query).fetchone()[0])


__all__ += ["convert_legacy_version", "iter_version_members", "compute_content_digest"]
