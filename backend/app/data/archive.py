"""原始响应档案：按内容摘要保存在 `.runtime/data/raw/`，数据库只存索引。

- 档案内容为供应商响应体本身，绝不包含请求头、令牌或任何凭据；
- 相同正文重复到来时只保留一份，但记录首次入库与时间；
- 只有成功获得的响应才写入档案；失败的请求没有正文可存档。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import Settings
from .timeutil import utc_now_iso


@dataclass(frozen=True)
class ArchiveRecord:
    digest: str
    path: str
    content_bytes: int
    interface: str
    source_code: str
    fetched_at: str


def content_digest(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def write_raw_response(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    source_code: str,
    interface: str,
    raw_bytes: bytes,
    batch_id: str,
) -> ArchiveRecord:
    """保存响应正文并登记档案索引；重复正文不写盘，只复用记录。"""
    digest = content_digest(raw_bytes)
    existing = conn.execute(
        "SELECT path, content_bytes, interface, source_code, fetched_at, first_batch_id"
        " FROM raw_archive WHERE digest=?",
        (digest,),
    ).fetchone()
    if existing is not None:
        return ArchiveRecord(digest, existing[0], existing[1], existing[2], existing[3], existing[4])

    path = Path(f"{source_code}/{interface}/{digest[:2]}/{digest}.json")
    target = Path(settings.raw_archive_dir) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(raw_bytes)
    fetched_at = utc_now_iso()
    conn.execute(
        "INSERT INTO raw_archive(digest, path, content_bytes, interface, source_code, fetched_at, first_batch_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (digest, str(path), len(raw_bytes), interface, source_code, fetched_at, batch_id),
    )
    return ArchiveRecord(digest, str(path), len(raw_bytes), interface, source_code, fetched_at)


def verify_archive_files(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """核对索引中的文件是否都能读到且摘要一致，返回损坏路径列表（只读检查）。"""
    broken: list[str] = []
    rows = conn.execute("SELECT digest, path, content_bytes FROM raw_archive").fetchall()
    for digest, path, size_bytes in rows:
        target = Path(settings.raw_archive_dir) / path
        if not target.is_file() or target.stat().st_size != size_bytes:
            broken.append(path)
            continue
        if content_digest(target.read_bytes()) != digest:
            broken.append(path)
    return broken


def rows_for_source(conn: sqlite3.Connection, source_code: str) -> Iterable[tuple]:
    yield from conn.execute(
        "SELECT digest, path, content_bytes, interface, fetched_at FROM raw_archive"
        " WHERE source_code=? ORDER BY fetched_at",
        (source_code,),
    )


def strip_secrets(payload: dict) -> dict:
    """移除请求参数中的凭据字段，用于请求日志与配置快照。"""
    import copy

    cleaned = copy.deepcopy(payload)
    for key in list(cleaned.keys()):
        lowered = str(key).lower()
        if "token" in lowered or "secret" in lowered or "password" in lowered or "key" == lowered:
            cleaned[key] = "<removed>"
    return cleaned


__all__ = [
    "ArchiveRecord",
    "content_digest",
    "write_raw_response",
    "verify_archive_files",
    "rows_for_source",
    "strip_secrets",
]
