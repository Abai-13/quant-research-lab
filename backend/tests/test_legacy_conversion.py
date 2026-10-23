"""schema 2 旧版本成员转换：同分区多修订块不得串版本，回收后旧 ID 仍可重现。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.chunks import version_members
from app.data.digests import sha256_of_object
from app.data.migration import (
    convert_legacy_members,
    initialize_database,
    migrate_database,
    reclaim_legacy_members,
)
from app.data.schema import create_schema_2


def _manifest(entries: list[dict]) -> str:
    ordered = sorted(entries, key=lambda item: (item["category"], item["logical_key"]))
    return sha256_of_object(ordered)


def _build_schema2_with_two_versions(settings: Settings) -> tuple[str, str]:
    path = settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "2"),
                ("instance_id", "legacy-two"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )
        create_schema_2(conn)
        conn.execute(
            "INSERT INTO daily_price(id, ts_code, trade_date, close, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (1, '000001.SZ', '20240102', 10.0,"
            " 'tushare', 1, 'h1', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO daily_price(id, ts_code, trade_date, close, open, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (2, '000001.SZ', '20240102', 11.0,"
            " 11.0, 'tushare', 2, 'h2', 'x', 'x')"
        )
        key = "tushare|daily|000001.SZ|20240102"
        entries1 = [
            {
                "category": "daily",
                "record_id": 1,
                "logical_key": key,
                "content_hash": "h1",
            }
        ]
        entries2 = [
            {
                "category": "daily",
                "record_id": 2,
                "logical_key": key,
                "content_hash": "h2",
            }
        ]
        for version_id, entries, created in (
            ("dv_legacy_v1", entries1, "2024-01-02T00:00:00+00:00"),
            ("dv_legacy_v2", entries2, "2024-01-03T00:00:00+00:00"),
        ):
            manifest = _manifest(entries)
            conn.execute(
                "INSERT INTO data_version(version_id, source_code, scope, categories,"
                " quality_summary, manifest_sha256, rule_version, status, created_at, published_at)"
                " VALUES (?, 'tushare', '{}', '[\"daily\"]', '{}', ?, 'qr-test', 'published', ?, ?)",
                (version_id, manifest, created, created),
            )
            conn.execute(
                "INSERT INTO data_version_member(version_id, category, record_id, content_hash,"
                " logical_key) VALUES (?, 'daily', ?, ?, ?)",
                (version_id, entries[0]["record_id"], entries[0]["content_hash"], key),
            )
    return "dv_legacy_v1", "dv_legacy_v2"


def test_same_partition_revisions_do_not_cross_versions_after_migration(
    temp_settings: Settings,
) -> None:
    v1, v2 = _build_schema2_with_two_versions(temp_settings)
    initialize_database(temp_settings)  # schema 2 不被隐式升级
    migrate_database(temp_settings, convert_legacy=True, reclaim_legacy=True)

    conn = sqlite3.connect(temp_settings.db_path)
    try:
        assert version_members(conn, v1)[0][1] == 1
        assert version_members(conn, v2)[0][1] == 2
        rows = conn.execute(
            "SELECT version_id, member_storage, manifest_version, manifest_sha256 FROM data_version"
            " ORDER BY version_id"
        ).fetchall()
        assert {row[0] for row in rows} == {v1, v2}
        assert all(row[1] == "chunked" and row[2] == 1 for row in rows)
        # 旧全量成员行已回收，但旧 ID 仍可读。
        assert conn.execute("SELECT COUNT(*) FROM data_version_member").fetchone()[0] == 0
        # 同一分区存在两个不同修订块，且引用不同。
        chunks = conn.execute(
            "SELECT version_id, chunk_id FROM data_version_chunk ORDER BY version_id"
        ).fetchall()
        assert len(chunks) == 2
        assert chunks[0][1] != chunks[1][1]
    finally:
        conn.close()


def test_conversion_is_idempotent_and_reclaim_requires_consistency(temp_settings: Settings) -> None:
    v1, v2 = _build_schema2_with_two_versions(temp_settings)
    initialize_database(temp_settings)
    migrate_database(temp_settings, convert_legacy=False)
    assert convert_legacy_members(temp_settings)
    # 已全部转换时再次调用是无操作。
    assert convert_legacy_members(temp_settings) == []
    deleted = reclaim_legacy_members(temp_settings)
    assert deleted == 2
    assert reclaim_legacy_members(temp_settings) == 0
