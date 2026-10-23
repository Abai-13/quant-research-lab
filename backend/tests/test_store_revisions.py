"""修订幂等、修订追加与数据版本清单。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.migration import initialize_database
from app.data.records import CATEGORY_DAILY, logical_key
from app.data.store import (
    publish_data_version,
    version_members,
    write_records,
)


def _daily(ts_code: str, trade_date: str, close: float) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
        "currency": "CNY",
        "source_volume_raw": 10.0,
        "source_amount_raw": close,
    }


def test_same_content_does_not_create_new_revision(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)

    first = write_records(conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare")
    second = write_records(conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare")

    assert first.inserted == 1
    assert first.revised == 0
    assert second.inserted == 0
    assert second.revised == 0
    assert second.unchanged == 1
    with sqlite3.connect(temp_settings.db_path) as check:
        rows = check.execute("SELECT COUNT(*) FROM daily_price").fetchone()
    assert rows[0] == 1
    conn.close()


def test_changed_content_appends_revision_and_keeps_old_row(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)

    write_records(conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare")
    outcome = write_records(
        conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 11.0)], source_code="tushare"
    )

    assert outcome.revised == 1
    assert outcome.inserted == 0
    with sqlite3.connect(temp_settings.db_path) as check:
        rows = check.execute(
            "SELECT revision_no, close FROM daily_price ORDER BY revision_no"
        ).fetchall()
    assert rows == [(1, 10.0), (2, 11.0)]
    conn.close()


def test_different_sources_do_not_mix(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)

    write_records(conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare")
    outcome = write_records(
        conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 99.0)], source_code="other-source"
    )

    assert outcome.inserted == 1
    with sqlite3.connect(temp_settings.db_path) as check:
        rows = check.execute(
            "SELECT source_code, revision_no, close FROM daily_price ORDER BY id"
        ).fetchall()
    assert rows == [("tushare", 1, 10.0), ("other-source", 1, 99.0)]
    conn.close()


def test_published_version_manifest_is_stable_and_members_fixed(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)
    outcome = write_records(
        conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare"
    )
    members = {
        CATEGORY_DAILY: [
            (outcome.record_ids[0], logical_key(CATEGORY_DAILY, _daily("000001.SZ", "20240102", 10.0), "tushare"), "hash")
        ]
    }
    with conn:
        conn.execute("UPDATE daily_price SET content_hash=? WHERE id=?", ("hash", outcome.record_ids[0]))

    first = publish_data_version(
        conn,
        source_code="tushare",
        scope={"start": "20240102", "end": "20240102"},
        categories=[CATEGORY_DAILY],
        rule_version="qr-test",
        quality_summary={"total": 0},
        members=members,
    )
    second = publish_data_version(
        conn,
        source_code="tushare",
        scope={"start": "20240102", "end": "20240102"},
        categories=[CATEGORY_DAILY],
        rule_version="qr-test",
        quality_summary={"total": 0},
        members=members,
    )

    assert first["version_id"] == second["version_id"]
    assert second["reused"] is True
    assert first["manifest_sha256"] == second["manifest_sha256"]
    stored = version_members(conn, first["version_id"])
    assert len(stored) == 1
    with sqlite3.connect(temp_settings.db_path) as check:
        version_rows = check.execute(
            "SELECT COUNT(*) FROM data_version WHERE version_id=?", (first["version_id"],)
        ).fetchone()
        lineage = check.execute(
            "SELECT first_version_id FROM daily_price WHERE id=?", (outcome.record_ids[0],)
        ).fetchone()
    assert version_rows[0] == 1
    assert lineage[0] == first["version_id"]
    conn.close()


def test_new_revision_creates_new_version_without_touching_old(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)
    first_outcome = write_records(
        conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 10.0)], source_code="tushare"
    )
    members_v1 = {
        CATEGORY_DAILY: [(first_outcome.record_ids[0], "tushare|daily|000001.SZ|20240102", "h1")]
    }
    with conn:
        conn.execute("UPDATE daily_price SET content_hash='h1' WHERE id=?", (first_outcome.record_ids[0],))
    version1 = publish_data_version(
        conn,
        source_code="tushare",
        scope={"start": "20240102", "end": "20240102"},
        categories=[CATEGORY_DAILY],
        rule_version="qr-test",
        quality_summary={},
        members=members_v1,
    )

    second_outcome = write_records(
        conn, CATEGORY_DAILY, [_daily("000001.SZ", "20240102", 12.0)], source_code="tushare"
    )
    members_v2 = {
        CATEGORY_DAILY: [(second_outcome.record_ids[0], "tushare|daily|000001.SZ|20240102", "h2")]
    }
    with conn:
        conn.execute("UPDATE daily_price SET content_hash='h2' WHERE id=?", (second_outcome.record_ids[0],))
    version2 = publish_data_version(
        conn,
        source_code="tushare",
        scope={"start": "20240102", "end": "20240102"},
        categories=[CATEGORY_DAILY],
        rule_version="qr-test",
        quality_summary={},
        members=members_v2,
    )

    assert version2["version_id"] != version1["version_id"]
    # 旧版本的成员集合不变。
    assert len(version_members(conn, version1["version_id"])) == 1
    assert version_members(conn, version1["version_id"])[0][1] == first_outcome.record_ids[0]
    with sqlite3.connect(temp_settings.db_path) as check:
        rows = check.execute("SELECT revision_no, close FROM daily_price ORDER BY id").fetchall()
    assert rows == [(1, 10.0), (2, 12.0)]
    conn.close()
