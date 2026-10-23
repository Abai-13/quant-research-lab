"""R1：指定数据版本的来源必须与请求来源一致，且缺失版本不得回退到最新数据。"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.data.chunks import VersionView
from app.data.eligibility import REASON_SOURCE_VERSION_MISMATCH, check_eligibility
from app.data.errors import DataError
from app.data.prices import price_for_purpose
from app.data.quality import run_dataset_quality_check
from app.data.universe import generate_pool_snapshot
from test_cleanup import _seed


def _connection(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_eligibility_rejects_cross_source_version_without_reading_prices(
    data_settings: Settings,
) -> None:
    v1, _v2 = _seed(data_settings)
    conn = _connection(data_settings)
    try:
        result = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare",
            data_version_id=v1,
            ts_codes=["000001.SZ"],
        )
    finally:
        conn.close()

    assert result.eligible is False
    assert result.reason_code == REASON_SOURCE_VERSION_MISMATCH
    assert result.details == []
    assert result.latest_data_date is None
    assert result.affected_scope["source_code"] == "tushare"
    assert result.affected_scope["data_version_source_code"] == "tushare-fixture"


def test_same_source_old_version_is_still_readable(data_settings: Settings) -> None:
    v1, v2 = _seed(data_settings)
    conn = _connection(data_settings)
    try:
        old = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare-fixture",
            data_version_id=v1,
            ts_codes=["000001.SZ"],
        )
        new = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare-fixture",
            data_version_id=v2,
            ts_codes=["000001.SZ"],
        )
    finally:
        conn.close()

    assert old.eligible is True
    assert old.details[0]["price"] is not None
    assert new.eligible is True
    assert new.details[0]["price"] is not None


def test_missing_version_does_not_fall_back_to_latest(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = _connection(data_settings)
    try:
        result = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare-fixture",
            data_version_id="dv_does_not_exist",
            ts_codes=["000001.SZ"],
        )
    finally:
        conn.close()
    assert result.eligible is False
    assert result.reason_code == "version_not_found"
    assert result.details == []


def test_pool_snapshot_rejects_cross_source_version(data_settings: Settings) -> None:
    v1, _v2 = _seed(data_settings)
    conn = _connection(data_settings)
    try:
        with pytest.raises(DataError) as excinfo:
            generate_pool_snapshot(
                conn,
                as_of="20240103",
                data_version_id=v1,
                source_code="tushare",
            )
    finally:
        conn.close()
    assert excinfo.value.reason == "version_source_mismatch"


def test_quality_check_rejects_cross_source_version(data_settings: Settings) -> None:
    v1, _v2 = _seed(data_settings)
    conn = _connection(data_settings)
    try:
        with pytest.raises(DataError) as excinfo:
            run_dataset_quality_check(
                conn, "tushare", as_of="20240103", data_version_id=v1
            )
    finally:
        conn.close()
    assert excinfo.value.reason == "version_source_mismatch"


def test_price_helpers_reject_cross_source_view(data_settings: Settings) -> None:
    v1, _v2 = _seed(data_settings)
    conn = _connection(data_settings)
    try:
        view = VersionView(conn, v1)
        with pytest.raises(DataError) as excinfo:
            price_for_purpose(
                conn,
                purpose="display",
                source_code="tushare",
                ts_code="000001.SZ",
                as_of="20240103",
                view=view,
            )
    finally:
        conn.close()
    assert excinfo.value.reason == "version_source_mismatch"
