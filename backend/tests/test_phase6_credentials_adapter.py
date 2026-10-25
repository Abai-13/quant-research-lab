from __future__ import annotations

import httpx

from app.config import load_settings
from app.credentials import CredentialManager
from app.credentials.service import target_fingerprint
from app.data.adapters.base import DataRequest
from app.data.adapters.registry import build_adapter


class FakeProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"sealed-test-only:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"sealed-test-only:")
        return ciphertext[len(b"sealed-test-only:"):][::-1]


def _ok() -> dict:
    return {"code": 0, "data": {"fields": ["ts_code", "trade_date", "close"],
                                      "items": [["000001.SZ", "20240102", 10.0]]}}


def test_tushare_adapter_resolves_each_request_and_clear_blocks_env_fallback(tmp_path, monkeypatch):
    from app.agent_gateway import security

    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_DATA_SOURCE": "tushare", "TUSHARE_TOKEN": "fake-env-token",
                              "APP_RUNTIME_DIR": str(tmp_path / "runtime"), "APP_TUSHARE_API_URL": "https://api.example.invalid"},
                             project_root=tmp_path)
    manager = CredentialManager(settings.runtime_dir, protector=FakeProtector())
    manager.save(slot="market_data:tushare", provider="tushare",
                 target_fingerprint=target_fingerprint(settings.data_source.api_base_url),
                 secret="fake-managed-token", storage_mode="persistent", expected_generation=0,
                 idempotency_key="market-save-operation-1")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok())

    adapter = build_adapter(settings)
    adapter._transport = httpx.MockTransport(handler)
    request = DataRequest(interface="daily", params={"ts_code": "000001.SZ"})
    assert adapter.fetch_page(request).status == "ok"
    assert "fake-managed-token" in seen[-1]
    assert "fake-env-token" not in seen[-1]

    manager.clear(slot="market_data:tushare", expected_generation=1, idempotency_key="market-clear-operation-1")
    failed = adapter.fetch_page(request)
    assert failed.status == "error"
    assert failed.error.reason == "credentials_required"
    assert len(seen) == 1


def test_tushare_external_environment_is_supported_until_cleared(tmp_path, monkeypatch):
    from app.agent_gateway import security

    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_DATA_SOURCE": "tushare", "TUSHARE_TOKEN": "fake-env-token",
                              "APP_RUNTIME_DIR": str(tmp_path / "runtime"), "APP_TUSHARE_API_URL": "https://api.example.invalid"},
                             project_root=tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok())

    adapter = build_adapter(settings)
    adapter._transport = httpx.MockTransport(handler)
    assert adapter.fetch_page(DataRequest(interface="daily")).status == "ok"
    assert "fake-env-token" in seen[0]
