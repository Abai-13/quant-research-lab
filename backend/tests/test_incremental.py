"""增量与修订：修订窗口内旧值变化产生新版本，旧记录与旧清单不变。"""

from __future__ import annotations

import json
import sqlite3

from app.config import Settings
from app.data.adapters.registry import build_adapter
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.store import published_versions, version_members


def _import(settings: Settings, *, start: str, end: str, codes=("000001.SZ",)) -> object:
    return run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor"),
            start=start,
            end=end,
            codes=tuple(codes),
        ),
    )


def test_incremental_import_appends_new_records(data_settings: Settings) -> None:
    initialize_database(data_settings)
    first = _import(data_settings, start="20230103", end="20240628")
    assert first.status == "completed"
    before = first.records["daily"]["inserted"]

    second = run_import(
        data_settings,
        ImportOptions(
            mode="incremental",
            categories=("daily",),
            codes=("000001.SZ",),
            end="20240628",
        ),
    )

    assert second.status == "completed"
    # 增量窗口内内容一致时不会产生新的修订。
    assert second.records["daily"]["revised"] == 0
    assert second.records["daily"]["inserted"] == 0
    with sqlite3.connect(data_settings.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    assert rows == before


class _RevisingAdapter:
    """模拟供应商修订某个历史交易日的数值。"""

    source_code = "tushare-fixture"

    def __init__(self, inner, *, code: str, trade_date: str, new_close: float) -> None:
        self._inner = inner
        self._code = code
        self._trade_date = trade_date
        self._new_close = new_close

    def min_interval_seconds(self) -> float:
        return 0.0

    def max_rows(self, interface: str) -> int:
        return self._inner.max_rows(interface)

    def fetch_page(self, request):
        outcome = self._inner.fetch_page(request)
        if not outcome.is_success:
            return outcome
        changed = False
        rows = []
        for row in outcome.rows:
            row = dict(row)
            if row.get("ts_code") == self._code and row.get("trade_date") == self._trade_date:
                row["close"] = self._new_close
                changed = True
            rows.append(row)
        outcome.rows = rows
        if changed:
            outcome.raw_bytes = json.dumps(
                {"code": 0, "msg": None, "data": {"fields": outcome.fields, "items": [
                    [row.get(field) for field in outcome.fields] for row in rows
                ]}},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        return outcome


def test_revised_value_creates_new_version_and_keeps_old_rows(data_settings: Settings) -> None:
    initialize_database(data_settings)
    first = _import(data_settings, start="20230103", end="20240628")
    version_id = first.version["version_id"]

    # 模拟供应商修订了某个历史交易日的收盘价。
    adapter = _RevisingAdapter(
        build_adapter(data_settings),
        code="000001.SZ",
        trade_date="20240627",
        new_close=99.99,
    )
    second = run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("daily",),
            start="20240627",
            end="20240628",
            codes=("000001.SZ",),
        ),
        adapter=adapter,
    )
    assert second.status == "completed"
    assert second.records["daily"]["revised"] >= 1
    assert second.version["version_id"] != version_id

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, revision_no, close FROM daily_price WHERE ts_code='000001.SZ'"
            " AND trade_date='20240627' ORDER BY revision_no"
        ).fetchall()
        old_members = version_members(conn, version_id)
        new_members = version_members(conn, second.version["version_id"])
    finally:
        conn.close()

    # 旧修订保留，新修订追加。
    assert len(rows) == 2
    assert rows[0]["revision_no"] == 1
    assert rows[0]["close"] != 99.99
    assert rows[1]["revision_no"] == 2
    assert rows[1]["close"] == 99.99
    # 旧数据版本的成员集合不变：仍指向修订 1，而不是新修订。
    old_daily = [item for item in old_members if item[0] == "daily" and item[3].endswith("000001.SZ|20240627")]
    new_daily = [item for item in new_members if item[0] == "daily" and item[3].endswith("000001.SZ|20240627")]
    assert len(old_daily) == 1
    assert len(new_daily) == 1
    assert old_daily[0][1] != new_daily[0][1]
    assert old_daily[0][1] == rows[0]["id"]
    assert new_daily[0][1] == rows[1]["id"]


def test_data_version_members_are_immutable(data_settings: Settings) -> None:
    initialize_database(data_settings)
    first = _import(data_settings, start="20230103", end="20240628")
    version_id = first.version["version_id"]

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute(
            "SELECT version_id, manifest_sha256, published_at FROM data_version WHERE version_id=?",
            (version_id,),
        ).fetchone()
        members_before = version_members(conn, version_id)
    finally:
        conn.close()

    _import(data_settings, start="20240102", end="20240105")

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        after = conn.execute(
            "SELECT version_id, manifest_sha256, published_at FROM data_version WHERE version_id=?",
            (version_id,),
        ).fetchone()
        members_after = version_members(conn, version_id)
    finally:
        conn.close()

    assert before["manifest_sha256"] == after["manifest_sha256"]
    assert before["published_at"] == after["published_at"]
    assert members_before == members_after


def test_incremental_window_uses_stored_business_scope(data_settings: Settings) -> None:
    initialize_database(data_settings)
    _import(data_settings, start="20240102", end="20240628")

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT checkpoint FROM import_batch ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    checkpoint = json.loads(row["checkpoint"])
    assert checkpoint["last_trade_date"] == "20240628"
    assert checkpoint["completed_units"]
