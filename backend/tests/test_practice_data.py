"""阶段四练习数据域测试：来源声明、版本共享与显式刷新。

原则：
- 全部使用临时目录与临时 SQLite 库，不触碰真实 `.runtime`。
- 真实取数不进 CI：BaoStock 客户端以最小假实现注入，行格式严格复刻
  官方客户端真实小样本（2026-10-24 核验）的返回形态。
- 合成证据与“真实源”证据分开断言。
"""

from __future__ import annotations

import sqlite3
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import load_settings
from app.practice_data import (
    create_practice_data_router,
    reset_practice_connect_provider,
    set_practice_connect_provider,
)
from app.practice_data.fetch import SourceDataError, normalize_daily_row
from app.practice_data.store import ensure_practice_data_schema

# 官方 query_history_k_data_plus 的日线字段契约。
_K_FIELDS = [
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "tradestatus", "pctChg", "isST",
]

# 真实小样本复刻（2026-10-24 经官方客户端 0.9.4 核验）。
_K_RAW = [
    ["2024-07-01", "sh.600000", "8.2200", "8.3000", "8.2000", "8.2800",
     "8.2300", "28732962", "237294502.0600", "3", "0.097900", "1", "0.607500", "0"],
    ["2024-07-02", "sh.600000", "8.2700", "8.5400", "8.2700", "8.5300",
     "8.2800", "57345810", "485897423.6600", "3", "0.195400", "1", "3.019300", "0"],
]


class _RS:
    """最小结果集：协议与官方 rs 对象一致（error_code/next/get_row_data/fields）。"""

    def __init__(self, fields, rows, error_code="0"):
        self.error_code = error_code
        self.error_msg = "success" if error_code == "0" else "error"
        self.fields = fields
        self._rows = list(rows)
        self._pos = 0

    def next(self) -> bool:
        if self._pos < len(self._rows):
            self._pos += 1
            return True
        return False

    def get_row_data(self) -> list[str]:
        return self._rows[self._pos - 1]


def _client(tmp_path) -> TestClient:
    settings = load_settings(
        {"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path
    )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(create_practice_data_router())
    return TestClient(app)


@pytest.fixture
def practice_db(tmp_path):
    """临时练习库与连接工厂（每次调用新开连接，语义与 core 一致）。"""
    # TestClient 在辅助线程执行端点；允许跨线程使用测试连接。
    path = tmp_path / "practice.db"
    bootstrap = sqlite3.connect(path, check_same_thread=False)
    bootstrap.row_factory = sqlite3.Row
    ensure_practice_data_schema(bootstrap)
    bootstrap.commit()
    bootstrap.close()

    def open_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    return open_conn


@pytest.fixture
def provider(practice_db):
    set_practice_connect_provider(lambda settings: practice_db())
    yield practice_db
    reset_practice_connect_provider()


@pytest.fixture
def fake_baostock(monkeypatch):
    """离线假客户端：按官方契约返回真实小样本形态，并记录调用轨迹。"""
    module = types.ModuleType("baostock")
    calls: list[str] = []

    def login():
        calls.append("login")
        return types.SimpleNamespace(error_code="0", error_msg="success")

    def query_history_k_data_plus(
        code, fields, start_date="", end_date="", frequency="d", adjustflag="3"
    ):
        calls.append(f"query:{code}:{frequency}:{adjustflag}")
        return _RS(fields.split(","), _K_RAW)

    def logout():
        calls.append("logout")

    module.login = login
    module.query_history_k_data_plus = query_history_k_data_plus
    module.logout = logout
    monkeypatch.setitem(sys.modules, "baostock", module)
    return calls


def test_sources_declares_limitations(tmp_path):
    """GET /data/sources 只读不联网；许可与覆盖按官方文档如实声明。"""
    with _client(tmp_path) as client:
        payload = client.get("/data/sources").json()
    assert payload["ok"] is True
    sources = {item["source_code"]: item for item in payload["sources"]}
    assert set(sources) == {"synthetic", "baostock"}
    assert sources["baostock"]["permission_status"] == "documented"
    assert sources["baostock"]["needs_credentials"] is False
    assert sources["synthetic"]["permission_status"] == "verified_ok"
    assert any(
        "客户端开源许可不等于行情数据许可" in line
        for line in sources["baostock"]["limitations"]
    )
    limit_text = " ".join(sources["baostock"]["coverage"].values())
    assert "1990-12-19" in limit_text and "isST" in limit_text
    # 可用性字段总是显式给出；不可用时给出受限原因，不冒充可用。
    assert "available" in sources["baostock"]
    if not sources["baostock"]["available"]:
        assert sources["baostock"]["restricted_reason"] == "baostock_not_installed"


def test_versions_empty_before_refresh(tmp_path):
    """合入 core runtime 后，隔离库无数据版本时 GET 返回空列表。"""
    with _client(tmp_path) as client:
        response = client.get("/data/versions")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["versions"] == []
        assert payload["total"] == 0


def test_versions_empty_with_injected_connection(tmp_path, provider):
    with _client(tmp_path) as client:
        payload = client.get("/data/versions").json()
    assert payload["ok"] is True
    assert payload["versions"] == []
    assert payload["total"] == 0


def test_versions_empty_before_data_schema_without_mutation(tmp_path):
    """GET /data/versions can run before the first refresh without creating tables."""
    path = tmp_path / "practice-no-data-schema.db"
    bootstrap = sqlite3.connect(path, check_same_thread=False)
    bootstrap.close()

    def open_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    set_practice_connect_provider(lambda settings: open_conn())
    try:
        with _client(tmp_path) as client:
            response = client.get("/data/versions")
        assert response.status_code == 200
        assert response.json()["versions"] == []
        with open_conn() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert tables == []
    finally:
        reset_practice_connect_provider()


def test_refresh_synthetic_offline_idempotent(tmp_path, provider):
    """合成源完全离线；同键重试复用同版本。"""
    with _client(tmp_path) as client:
        body = {
            "source_code": "synthetic",
            "symbols": ["sh.600000"],
            "start_date": "2024-07-01",
            "end_date": "2024-07-05",
            "idempotency_key": "key-1",
        }
        first = client.post("/data/refresh", json=body).json()
        assert first["ok"] is True
        assert first["evidence"]["offline_synthetic"] is True
        assert first["evidence"]["real_network"] is False
        assert first["version"]["synthetic"] is True
        assert first["version"]["record_count"] == 5

        again = client.post("/data/refresh", json=body).json()
        assert again["reused"] is True
        assert again["version"]["version_id"] == first["version"]["version_id"]

        listing = client.get("/data/versions").json()
        assert listing["total"] == 1
        assert listing["versions"][0]["version_id"] == first["version"]["version_id"]


def test_refresh_conflicting_key_rejected(tmp_path, provider):
    with _client(tmp_path) as client:
        base = {
            "source_code": "synthetic",
            "symbols": ["sh.600000"],
            "start_date": "2024-07-01",
            "end_date": "2024-07-05",
            "idempotency_key": "key-x",
        }
        assert client.post("/data/refresh", json=base).json()["ok"] is True
        conflict = dict(base)
        conflict["start_date"] = "2024-07-02"
        response = client.post("/data/refresh", json=conflict)
        assert response.status_code == 409
        payload = response.json()
        assert payload["ok"] is False
        assert payload["reason"] == "idempotency_conflict"


def test_refresh_validations(tmp_path, provider):
    with _client(tmp_path) as client:
        base = {
            "source_code": "unknown-src",
            "symbols": ["sh.600000"],
            "start_date": "2024-07-01",
            "end_date": "2024-07-05",
            "idempotency_key": "k",
        }
        response = client.post("/data/refresh", json=base)
        assert response.status_code == 404
        assert response.json()["reason"] == "unknown_source"

        bad_date = dict(base, source_code="synthetic", start_date="2024/07/01")
        response = client.post("/data/refresh", json=bad_date)
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_request"

        no_key = dict(base, source_code="synthetic", idempotency_key="")
        response = client.post("/data/refresh", json=no_key)
        assert response.json()["reason"] == "invalid_request"


def test_refresh_baostock_real_contract_rows(tmp_path, provider, fake_baostock):
    """官方契约行（真实小样本形态）解析与入库；调用轨迹完整。"""
    with _client(tmp_path) as client:
        assert client.get("/data/sources").status_code == 200
        assert client.get("/data/versions").json()["total"] == 0
        assert fake_baostock == []  # GET 请求不会登录、取数或刷新。
        payload = client.post(
            "/data/refresh",
            json={
                "source_code": "baostock",
                "symbols": ["sh.600000"],
                "start_date": "2024-07-01",
                "end_date": "2024-07-05",
                "idempotency_key": "real-1",
            },
        ).json()
        assert payload["ok"] is True
        assert payload["evidence"] == {
            "real_network": True,
            "offline_synthetic": False,
            "provider": "baostock",
            "fetched_rows": 2,
        }
        version = payload["version"]
        assert version["synthetic"] is False
        assert version["record_count"] == 2

        with provider() as query_conn:
            rows = query_conn.execute(
                "SELECT trade_date, open, close, preclose, volume, amount, tradestatus,"
                " adjustflag FROM practice_data_daily WHERE version_id = ?"
                " ORDER BY trade_date",
                (version["version_id"],),
            ).fetchall()
        assert [row["trade_date"] for row in rows] == ["2024-07-01", "2024-07-02"]
        assert rows[0]["open"] == pytest.approx(8.22)
        assert rows[0]["preclose"] == pytest.approx(8.23)
        assert rows[0]["volume"] == 28732962
        assert rows[0]["amount"] == pytest.approx(237294502.06)
        assert rows[0]["tradestatus"] == 1
        assert rows[0]["adjustflag"] == "3"
    assert fake_baostock[0] == "login"
    assert "query:sh.600000:d:3" in fake_baostock[1]
    assert fake_baostock[-1] == "logout"


def test_baostock_field_contract_mismatch_is_error(tmp_path, provider, monkeypatch):
    """客户端返回列漂移必须显式报错，不得列错位入库。"""
    module = types.ModuleType("baostock")
    module.login = lambda: types.SimpleNamespace(error_code="0", error_msg="success")

    def query(code, fields, start_date="", end_date="", frequency="d", adjustflag="3"):
        return _RS(["date", "code"], [])

    module.query_history_k_data_plus = query
    module.logout = lambda: None
    monkeypatch.setitem(sys.modules, "baostock", module)
    with _client(tmp_path) as client:
        response = client.post(
            "/data/refresh",
            json={
                "source_code": "baostock",
                "symbols": ["sh.600000"],
                "start_date": "2024-07-01",
                "end_date": "2024-07-02",
                "idempotency_key": "bad-fields",
            },
        )
        assert response.status_code == 400
        assert response.json()["reason"] == "baostock_field_contract_mismatch"
        assert client.get("/data/versions").json()["total"] == 0


def test_baostock_not_installed_restricted(tmp_path, provider, monkeypatch):
    """客户端缺失时显式降级：available=False 且给出原因。"""
    monkeypatch.setitem(sys.modules, "baostock", None)
    with _client(tmp_path) as client:
        payload = client.get("/data/sources").json()
    sources = {item["source_code"]: item for item in payload["sources"]}
    assert sources["baostock"]["available"] is False
    assert sources["baostock"]["restricted_reason"] == "baostock_not_installed"


def test_normalize_rejects_bad_tradestatus():
    bad_row = [
        "2024-07-01", "sh.600000", "8.2200", "8.3000", "8.2000", "8.2800",
        "8.2300", "1000", "12345.0", "3", "0.09", "2", "0.5", "0",
    ]
    with pytest.raises(SourceDataError):
        normalize_daily_row("sh.600000", _K_FIELDS, bad_row)


def test_router_exports_expected_subroutes():
    router = create_practice_data_router()
    paths = {route.path for route in router.routes}
    assert paths == {
        "/data/sources", "/data/versions", "/data/refresh",
        "/data/presets", "/data/prepare", "/data/jobs/{job_id}",
        "/data/jobs/{job_id}/cancel", "/data/jobs/{job_id}/resume", "/data/cache",
    }
