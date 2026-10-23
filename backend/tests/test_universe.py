"""历史股票池快照：历史状态、排除原因、可重现性与限制传播。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.store import published_versions
from app.data.universe import (
    LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE,
    LIMIT_SURVIVORSHIP_BIAS,
    PoolRule,
    generate_pool_snapshot,
    load_pool_snapshot,
)

RULE = PoolRule(
    min_history_trade_days=30,
    liquidity_window_days=10,
    min_avg_amount_cny=1_000_000.0,
)


def _seed(settings: Settings) -> str:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor", "suspension", "corporate_action"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ", "600000.SH", "000003.SZ"),
        ),
    )
    assert result.status == "completed"
    return result.version["version_id"]


def test_pool_excludes_delisted_and_keeps_listed(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    assert "000001.SZ" in snapshot["members"]
    assert "600000.SH" in snapshot["members"]
    reasons = {item["ts_code"]: item["reason_code"] for item in snapshot["exclusions"]}
    assert reasons["000003.SZ"] == "delisted_before_as_of"


def test_pool_uses_historical_state_not_current_list(data_settings: Settings) -> None:
    """退市证券在退市日之前仍可进入当时的历史股票池。"""
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = generate_pool_snapshot(
            conn,
            as_of="20240301",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
        after = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    assert "000003.SZ" in before["members"]
    assert "000003.SZ" not in after["members"]
    assert before["snapshot_id"] != after["snapshot_id"]


def test_pool_reports_exclusion_reasons(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=PoolRule(
                min_history_trade_days=1000,
                liquidity_window_days=10,
                min_avg_amount_cny=1_000_000.0,
            ),
        )
    finally:
        conn.close()

    reasons = {item["reason_code"] for item in snapshot["exclusions"]}
    assert "insufficient_history" in reasons
    for item in snapshot["exclusions"]:
        assert item["detail"]


def test_pool_liquidity_window_uses_only_data_before_as_of(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        strict = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=PoolRule(
                min_history_trade_days=30,
                liquidity_window_days=10,
                min_avg_amount_cny=10_000_000_000.0,
            ),
        )
    finally:
        conn.close()

    assert strict["member_count"] == 0
    reasons = {item["reason_code"] for item in strict["exclusions"]}
    assert "below_liquidity_threshold" in reasons


def test_pool_snapshot_is_reproducible(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        first = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
        second = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["members"] == second["members"]
    assert first["generated_at"] == second["generated_at"]


def test_new_data_does_not_change_existing_snapshot(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        first = generate_pool_snapshot(
            conn,
            as_of="20240301",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    # 追加更多（更晚）行情并发布新版本。
    run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("daily",),
            start="20240301",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        stored = load_pool_snapshot(conn, first["snapshot_id"])
        versions = published_versions(conn)
        regenerated = generate_pool_snapshot(
            conn,
            as_of="20240301",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    assert stored is not None
    assert stored["members"] == first["members"]
    assert stored["manifest_sha256"] == first["manifest_sha256"]
    assert regenerated["members"] == first["members"]
    assert len(versions) >= 1


def test_limit_flags_are_propagated(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    assert LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE in snapshot["limit_flags"]
    assert LIMIT_SURVIVORSHIP_BIAS in snapshot["limit_flags"]
    assert "historical_availability_unproven" in snapshot["limit_flags"]


def test_pool_excludes_securities_with_unknown_listing_status(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
    finally:
        conn.close()

    reasons = {item["ts_code"]: item["reason_code"] for item in snapshot["exclusions"]}
    assert reasons.get("900001.SZ") == "listing_status_unknown"


def test_pool_members_are_persisted(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = generate_pool_snapshot(
            conn,
            as_of="20240628",
            data_version_id=version_id,
            source_code="tushare-fixture",
            rule=RULE,
        )
        stored = load_pool_snapshot(conn, snapshot["snapshot_id"])
    finally:
        conn.close()

    assert stored is not None
    assert stored["members"] == snapshot["members"]
    assert stored["member_count"] == len(snapshot["members"])
    assert stored["as_of"] == "20240628"
    assert stored["data_version_id"] == version_id
