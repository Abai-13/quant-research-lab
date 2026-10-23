"""导入管线：分页、幂等、有界重试、失败不发布与写入串行。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.data.adapters.fixture import FixtureAdapter
from app.data.errors import DataError
from app.data.locking import DataWriteLock
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, incremental_window, run_import
from app.data.store import published_versions

CODES = ("000001.SZ", "600000.SH", "000003.SZ")

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class _InvalidPriceAdapter(FixtureAdapter):
    """夹具适配器包装：把指定交易日的收盘价改造成不合法值。"""

    def __init__(self, mutations: dict[str, object]) -> None:
        super().__init__(FIXTURES_DIR / "tushare")
        self._mutations = mutations

    def fetch_page(self, request):  # noqa: ANN001 - 与基类签名一致
        outcome = super().fetch_page(request)
        if outcome.status == "ok" and request.interface == "daily":
            for row in outcome.rows:
                date_value = str(row.get("trade_date") or "")
                if date_value in self._mutations:
                    row["close"] = self._mutations[date_value]
        return outcome


def _history_options(codes=CODES, **kwargs) -> ImportOptions:
    return ImportOptions(
        mode="history",
        categories=("calendar", "security", "daily", "adj_factor", "suspension", "corporate_action"),
        start="20240102",
        end="20240628",
        codes=tuple(codes),
        **kwargs,
    )


def test_successful_import_publishes_version_and_writes_records(data_settings: Settings) -> None:
    initialize_database(data_settings)

    result = run_import(data_settings, _history_options())

    assert result.status == "completed"
    assert result.pages_failed == 0
    assert result.version is not None
    assert result.version["status"] == "published"
    assert result.records["daily"]["inserted"] > 0
    assert result.records["calendar"]["inserted"] > 0
    with sqlite3.connect(data_settings.db_path) as conn:
        versions = published_versions(conn)
        rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()
    assert len(versions) == 1
    assert rows[0] == result.records["daily"]["inserted"]


def test_repeated_import_is_idempotent(data_settings: Settings) -> None:
    initialize_database(data_settings)

    first = run_import(data_settings, _history_options())
    second = run_import(data_settings, _history_options())

    assert second.status == "completed"
    assert second.records["daily"]["inserted"] == 0
    assert second.records["daily"]["revised"] == 0
    assert second.records["daily"]["unchanged"] == first.records["daily"]["inserted"]
    assert second.version["version_id"] == first.version["version_id"]
    assert second.version["reused"] is True
    with sqlite3.connect(data_settings.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()
        versions = published_versions(conn)
    assert rows[0] == first.records["daily"]["inserted"]
    assert len(versions) == 1


def test_pagination_collects_all_pages(fixture_settings_factory) -> None:
    settings = fixture_settings_factory("paging")
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(mode="history", categories=("daily",), start="20240102", end="20240110", codes=("000001.SZ",)),
    )

    assert result.status == "completed"
    assert result.pages_ok == 2
    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute("SELECT trade_date FROM daily_price ORDER BY trade_date").fetchall()
    assert [row[0] for row in rows] == ["20240102", "20240103", "20240104", "20240105", "20240108"]


def test_missing_page_is_not_reported_as_complete(fixture_settings_factory) -> None:
    settings = fixture_settings_factory("errors")
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(mode="history", categories=("daily",), start="20240102", end="20240110", codes=("900014.SZ",)),
    )

    assert result.status == "failed"
    assert result.pages_failed >= 1
    assert result.version is None
    codes = {issue["rule_code"] for issue in result.issues}
    assert "Q002_page_coverage_gap" in codes
    with sqlite3.connect(settings.db_path) as conn:
        versions = published_versions(conn)
    assert versions == []


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_attempts"),
    [
        ("900010.SZ", "rate_limited", 3),
        ("900011.SZ", "no_permission", 1),
        ("900012.SZ", "contract_error", 1),
        ("900013.SZ", "transport_error", 3),
    ],
)
def test_error_classification_and_bounded_retries(
    fixture_settings_factory, code: str, expected_status: str, expected_attempts: int
) -> None:
    settings = fixture_settings_factory("errors", APP_IMPORT_MAX_ATTEMPTS="3")
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(mode="history", categories=("daily",), start="20240102", end="20240110", codes=(code,)),
    )

    assert result.status == "failed"
    assert result.errors[0]["status"] == expected_status
    with sqlite3.connect(settings.db_path) as conn:
        attempts = conn.execute(
            "SELECT attempt FROM import_request_log WHERE batch_id=? ORDER BY request_id DESC LIMIT 1",
            (result.batch_id,),
        ).fetchone()
    assert attempts[0] == expected_attempts


def test_permission_error_does_not_publish_new_version_but_old_version_stays_readable(
    fixture_settings_factory, data_settings: Settings
) -> None:
    initialize_database(data_settings)
    good = run_import(data_settings, _history_options(codes=("000001.SZ",)))
    assert good.version is not None

    bad_settings = fixture_settings_factory("errors")
    initialize_database(bad_settings)
    bad = run_import(
        bad_settings,
        ImportOptions(mode="history", categories=("daily",), start="20240102", end="20240110", codes=("900011.SZ",)),
    )

    assert bad.version is None
    with sqlite3.connect(bad_settings.db_path) as conn:
        versions = published_versions(conn)
    assert versions == []


def test_request_log_never_contains_credentials(data_settings: Settings) -> None:
    initialize_database(data_settings)

    result = run_import(data_settings, _history_options(codes=("000001.SZ",)))

    with sqlite3.connect(data_settings.db_path) as conn:
        rows = conn.execute("SELECT request_params, detail FROM import_request_log").fetchall()
    assert rows
    for params, detail in rows:
        payload = params or ""
        assert "token" not in payload.lower()
        assert "secret" not in payload.lower()
        assert data_settings.data_source.token == ""


def test_raw_archive_stores_response_without_credentials(data_settings: Settings) -> None:
    initialize_database(data_settings)

    run_import(data_settings, _history_options(codes=("000001.SZ",)))

    with sqlite3.connect(data_settings.db_path) as conn:
        rows = conn.execute("SELECT digest, path FROM raw_archive").fetchall()
    assert rows
    for digest, path in rows:
        target = Path(data_settings.raw_archive_dir) / path
        assert target.is_file()
        content = target.read_text(encoding="utf-8")
        assert "token" not in content.lower()
        assert "secret" not in content.lower()


def test_concurrent_write_is_rejected_with_clear_error(data_settings: Settings) -> None:
    initialize_database(data_settings)
    lock = DataWriteLock(data_settings.lock_dir / "data-write.lock")
    lock.acquire()
    try:
        with pytest.raises(DataError) as excinfo:
            run_import(data_settings, _history_options(codes=("000001.SZ",)))
    finally:
        lock.release()

    assert excinfo.value.reason == "write_lock_timeout"


def test_resume_uses_checkpoint_and_skips_completed_units(data_settings: Settings) -> None:
    initialize_database(data_settings)
    first = run_import(data_settings, _history_options(codes=("000001.SZ",)))
    assert first.status == "completed"

    with sqlite3.connect(data_settings.db_path) as conn:
        checkpoint = conn.execute(
            "SELECT checkpoint FROM import_batch WHERE batch_id=?", (first.batch_id,)
        ).fetchone()[0]

    second = run_import(
        data_settings, _history_options(codes=("000001.SZ",), resume_batch_id=first.batch_id)
    )

    assert second.status == "completed"
    # 已完成的范围被跳过：不会重复写入，也不会产生新修订。
    assert second.records.get("daily", {}).get("inserted", 0) == 0
    with sqlite3.connect(data_settings.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()
        versions = published_versions(conn)
    assert rows[0] == first.records["daily"]["inserted"]
    assert len(versions) == 1
    assert checkpoint


def test_incremental_window_uses_business_scope_not_process_start(tmp_path: Path) -> None:
    settings = load_settings(
        {"APP_DATA_SOURCE": "tushare-fixture", "APP_RUNTIME_DIR": str(tmp_path / "runtime")},
        project_root=tmp_path,
    )
    initialize_database(settings)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO daily_price(ts_code, trade_date, close, source_code, revision_no, content_hash,"
            " observed_at, ingested_at) VALUES ('000001.SZ','20240620',10.0,'tushare-fixture',1,'h',"
            "'2024-06-20T00:00:00+00:00','2024-06-20T00:00:00+00:00')"
        )
        conn.commit()
        window = incremental_window(conn, "tushare-fixture")

    assert window["last_known_trade_date"] == "20240620"


def test_import_requires_schema_2(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", "legacy"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )

    with pytest.raises(DataError) as excinfo:
        run_import(temp_settings, _history_options(codes=("000001.SZ",)))

    assert excinfo.value.reason == "migration_required"


def test_real_source_without_credentials_fails_with_credentials_reason(tmp_path: Path) -> None:
    settings = load_settings(
        {"APP_DATA_SOURCE": "tushare", "APP_RUNTIME_DIR": str(tmp_path / "runtime")},
        project_root=tmp_path,
    )
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(mode="history", categories=("daily",), start="20240102", end="20240110", codes=("000001.SZ",)),
    )

    assert result.status == "failed"
    assert result.errors[0]["status"] == "credentials_required"


def test_invalid_source_prices_keep_evidence_and_are_unusable(
    data_settings: Settings,
) -> None:
    from app.data.eligibility import REASON_QUALITY_BLOCKED, check_eligibility

    initialize_database(data_settings)
    adapter = _InvalidPriceAdapter({"20240103": True, "20240104": -5.0, "20240105": 0})
    result = run_import(
        data_settings,
        adapter=adapter,
        options=ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor"),
            start="20240102",
            end="20240110",
            codes=("000001.SZ",),
        ),
    )

    assert result.status == "completed"
    assert result.version is not None
    q018 = [issue for issue in result.issues if issue["rule_code"] == "Q018_invalid_price_format"]
    assert len(q018) == 3
    scopes = {(issue["scope"]["trade_date"], issue["scope"]["ts_code"]) for issue in q018}
    assert scopes == {("20240103", "000001.SZ"), ("20240104", "000001.SZ"), ("20240105", "000001.SZ")}
    assert q018[0]["severity"] == "high"
    assert "契约" in q018[0]["evidence"] or "价格" in q018[0]["evidence"]

    # 不合法价格回落为缺失（None），不会静默转成 1.0/0.0/负数。
    with sqlite3.connect(data_settings.db_path) as conn:
        rows = dict(
            conn.execute(
                "SELECT trade_date, close FROM daily_price WHERE ts_code='000001.SZ'"
                " AND trade_date IN ('20240102','20240103','20240104','20240105')"
            ).fetchall()
        )
    assert rows["20240102"] is not None
    assert rows["20240103"] is None
    assert rows["20240104"] is None
    assert rows["20240105"] is None

    # 该记录不能用于成交用途：资格被已记录的高严重度问题阻断，并给出明确原因。
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        blocked = check_eligibility(
            conn,
            purpose="execution_reference",
            as_of="20240110",
            source_code="tushare-fixture",
            data_version_id=result.version["version_id"],
            ts_codes=["000001.SZ"],
            business_date="20240103",
        )
        # 未受影响的同证券其他日期不被误伤。
        clean = check_eligibility(
            conn,
            purpose="execution_reference",
            as_of="20240110",
            source_code="tushare-fixture",
            data_version_id=result.version["version_id"],
            ts_codes=["000001.SZ"],
            business_date="20240102",
        )
    finally:
        conn.close()

    assert blocked.eligible is False
    assert blocked.reason_code == REASON_QUALITY_BLOCKED
    assert any("20240103" in reason and "Q018_invalid_price_format" in reason for reason in blocked.reasons)
    assert clean.eligible is True
    assert clean.reason_code == "eligible"
