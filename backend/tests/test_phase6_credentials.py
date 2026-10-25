from __future__ import annotations

import base64
import json
import sys

import pytest

from app.credentials import CredentialError, CredentialManager, close_process_credentials
from app.credentials.service import target_fingerprint
from app.agent_gateway.core import GatewayService
from app.config import load_settings
from app.agent_gateway.security import WindowsDpapiProtector
from app.api.credentials import create_credentials_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeProtector:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def protect(self, plaintext: bytes) -> bytes:
        if self.fail:
            raise RuntimeError("test protection failure")
        mask = b"phase6-test-only-mask"
        return b"FAKE" + bytes(value ^ mask[index % len(mask)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext: bytes) -> bytes:
        if self.fail or not ciphertext.startswith(b"FAKE"):
            raise RuntimeError("test protected value unavailable")
        mask = b"phase6-test-only-mask"
        body = ciphertext[4:]
        return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(body))


def _save(manager: CredentialManager, *, slot: str = "model", secret: str = "fake-key-123456789",
          mode: str = "persistent", generation: int = 0, target: str = "https://model.example/v1",
          idem: str = "save-operation-0001") -> dict:
    return manager.save(slot=slot, provider="test-provider", target_fingerprint=target_fingerprint(target),
                        secret=secret, storage_mode=mode, expected_generation=generation, idempotency_key=idem)


def test_persistent_secret_is_protected_and_survives_manager_recreation(tmp_path):
    manager = CredentialManager(tmp_path, protector=FakeProtector())
    saved = _save(manager)
    secret_file = next((tmp_path / "credentials").glob("secret-*.dpapi"))
    state_file = tmp_path / "credentials" / "state.json"
    secret = "fake-key-123456789"

    assert saved["configured"] is True
    assert secret.encode() not in secret_file.read_bytes()
    assert secret.encode() not in state_file.read_bytes()
    restarted = CredentialManager(tmp_path, protector=FakeProtector())
    assert restarted.resolve("model", target_fingerprint("https://model.example/v1")) == secret
    assert restarted.status("model")["credential_generation"] == 1


def test_persistent_protection_failure_never_writes_plaintext(tmp_path):
    manager = CredentialManager(tmp_path, protector=FakeProtector(fail=True))
    with pytest.raises(CredentialError) as caught:
        _save(manager)
    assert caught.value.reason == "secret_store_unavailable"
    assert not list((tmp_path / "credentials").glob("secret-*.dpapi"))
    assert not (tmp_path / "credentials" / "state.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is platform-specific")
def test_windows_dpapi_persistent_credential_survives_manager_recreation(tmp_path):
    manager = CredentialManager(tmp_path, protector=WindowsDpapiProtector())
    saved = _save(manager)
    restarted = CredentialManager(tmp_path, protector=WindowsDpapiProtector())
    assert saved["configured"] is True
    assert restarted.resolve("model", target_fingerprint("https://model.example/v1")) == "fake-key-123456789"


def test_process_secret_is_memory_only_and_restart_cannot_fall_back(tmp_path):
    manager = CredentialManager(tmp_path, protector=FakeProtector())
    _save(manager, mode="process")
    assert not list((tmp_path / "credentials").glob("secret-*.dpapi"))
    assert manager.resolve("model", target_fingerprint("https://model.example/v1")) == "fake-key-123456789"

    close_process_credentials(tmp_path)
    restarted = CredentialManager(tmp_path, protector=FakeProtector())
    assert restarted.status("model")["reason"] == "process_credential_missing_after_restart"
    with pytest.raises(CredentialError, match="after restart"):
        restarted.resolve("model", target_fingerprint("https://model.example/v1"), external_secret="stale-env-key")


def test_clear_erases_slot_secret_increments_generation_and_blocks_all_fallbacks(tmp_path):
    manager = CredentialManager(tmp_path, protector=FakeProtector())
    _save(manager)
    secret_file = next((tmp_path / "credentials").glob("secret-*.dpapi"))
    clear = manager.clear(slot="model", expected_generation=1, idempotency_key="clear-operation-0001")

    assert clear["state"] == "cleared"
    assert clear["credential_generation"] == 2
    assert not secret_file.exists()
    restarted = CredentialManager(tmp_path, protector=FakeProtector())
    with pytest.raises(CredentialError) as caught:
        restarted.resolve("model", target_fingerprint("https://model.example/v1"),
                          external_secret="fake-env-key", legacy_secret="legacy-key")
    assert caught.value.reason == "credential_cleared"
    assert restarted.status("model", external_secret="fake-env-key")["state"] == "cleared"

    _save(restarted, secret="re-entered-key-1234", generation=2, idem="explicit-reconfigure-01")
    assert restarted.resolve("model", target_fingerprint("https://model.example/v1")) == "re-entered-key-1234"


def test_generation_target_binding_and_idempotency_are_enforced(tmp_path):
    manager = CredentialManager(tmp_path, protector=FakeProtector())
    first = _save(manager)
    replay = _save(manager)
    assert replay == first
    with pytest.raises(CredentialError) as caught:
        _save(manager, secret="different-key-123456", idem="save-operation-0001")
    assert caught.value.reason == "idempotency_conflict"
    with pytest.raises(CredentialError) as caught:
        manager.resolve("model", target_fingerprint("https://other.example/v1"))
    assert caught.value.reason == "target_mismatch"
    with pytest.raises(CredentialError) as caught:
        _save(manager, secret="replacement-123456", generation=0, idem="save-operation-0002")
    assert caught.value.reason == "generation_conflict"


def test_legacy_gateway_secret_is_read_only_and_tombstone_disables_it(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    protector = FakeProtector()
    service = GatewayService(settings, protector=protector)
    service._directory.mkdir(parents=True)
    config_version = "a" * 32
    state = {"active_version": config_version, "versions": [{
        "config_version": config_version, "name": "legacy", "protocol": "chat_completions",
        "base_url": "https://model.example/v1", "model": "test-model", "allow_loopback_http": False,
        "trusted_private_https": False, "limits": {"rounds": 1, "tool_calls": 1, "timeout_seconds": 5, "tokens": 12},
    }]}
    service._config_path.write_text(json.dumps(state), encoding="utf-8")
    legacy_bytes = base64.b64encode(protector.protect(json.dumps({config_version: "legacy-fake-key-123"}).encode()))
    service._legacy_secrets_path.write_bytes(legacy_bytes)

    assert service.get_public_config()["configured"] is True
    assert service._legacy_secrets_path.read_bytes() == legacy_bytes
    CredentialManager(settings.runtime_dir, protector=protector).clear(
        slot="model", expected_generation=0, idempotency_key="clear-legacy-operation-1")
    service.clear_configuration_metadata()
    assert service.get_public_config()["configured"] is False
    assert not service._legacy_secrets_path.exists()


def test_credentials_api_reports_and_clears_active_legacy_gateway_key(tmp_path, monkeypatch):
    from app.agent_gateway import core
    from app.agent_gateway import security

    monkeypatch.setattr(core, "WindowsDpapiProtector", FakeProtector)
    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    service = GatewayService(settings, protector=FakeProtector())
    service._directory.mkdir(parents=True)
    version = "b" * 32
    state = {"active_version": version, "versions": [{
        "config_version": version, "name": "legacy", "protocol": "chat_completions",
        "base_url": "https://legacy-model.example/v1", "model": "fake-model", "allow_loopback_http": False,
        "trusted_private_https": False, "limits": {"rounds": 1, "tool_calls": 1, "timeout_seconds": 5, "tokens": 16},
    }]}
    service._config_path.write_text(json.dumps(state), encoding="utf-8")
    service._legacy_secrets_path.write_bytes(base64.b64encode(
        FakeProtector().protect(json.dumps({version: "fake-legacy-key-123"}).encode())))
    app = FastAPI()
    app.include_router(create_credentials_router(settings))
    client = TestClient(app)

    legacy = next(item for item in client.get("/api/credentials").json()["credentials"] if item["slot"] == "model")
    assert legacy["state"] == "configured"
    assert legacy["configured"] is True
    assert legacy["target_fingerprint"] == GatewayService._credential_target(service.get_public_config()["config"])
    assert "fake-legacy-key-123" not in json.dumps(legacy)
    assert service._legacy_secrets_path.exists()

    wrong_target = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": target_fingerprint("https://other-model.example/v1"),
        "secret": "fake-wrong-target-key", "storage_mode": "persistent",
        "expected_generation": legacy["credential_generation"], "idempotency_key": "wrong-model-target-01",
    })
    assert wrong_target.status_code == 409

    replacement = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": legacy["target_fingerprint"],
        "secret": "fake-replacement-model-key", "storage_mode": "persistent",
        "expected_generation": legacy["credential_generation"], "idempotency_key": "replace-legacy-model-01",
    })
    assert replacement.status_code == 200, replacement.text
    active = GatewayService(settings, protector=FakeProtector())
    active_public = active.get_public_config()
    active_version = active_public["config"]["config_version"]
    assert active_public["configured"] is True
    assert active._load_state()[1][active_version] == "fake-replacement-model-key"
    assert not service._legacy_secrets_path.exists()

    replay = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": legacy["target_fingerprint"],
        "secret": "fake-replacement-model-key", "storage_mode": "persistent",
        "expected_generation": legacy["credential_generation"], "idempotency_key": "replace-legacy-model-01",
    })
    assert replay.status_code == 200
    assert replay.json()["credential"]["credential_generation"] == replacement.json()["credential"]["credential_generation"]
    second = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": legacy["target_fingerprint"],
        "secret": "fake-second-model-key", "storage_mode": "process",
        "expected_generation": replacement.json()["credential"]["credential_generation"],
        "idempotency_key": "replace-legacy-model-02",
    })
    assert second.status_code == 200
    old_replay = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": legacy["target_fingerprint"],
        "secret": "fake-replacement-model-key", "storage_mode": "persistent",
        "expected_generation": legacy["credential_generation"], "idempotency_key": "replace-legacy-model-01",
    })
    assert old_replay.status_code == 200
    current = GatewayService(settings, protector=FakeProtector())
    current_version = current.get_public_config()["config"]["config_version"]
    assert current._load_state()[1][current_version] == "fake-second-model-key"
    assert current.get_public_config()["configured"] is True
    stale = client.put("/api/credentials/model", json={
        "provider": legacy["provider"], "target_fingerprint": legacy["target_fingerprint"],
        "secret": "fake-stale-model-key", "storage_mode": "persistent",
        "expected_generation": second.json()["credential"]["credential_generation"] - 1,
        "idempotency_key": "stale-legacy-model-01",
    })
    assert stale.status_code == 409

    cleared = client.request("DELETE", "/api/credentials/model", json={
        "expected_generation": second.json()["credential"]["credential_generation"],
        "idempotency_key": "clear-api-legacy-key-01",
    })
    assert cleared.status_code == 200
    after = next(item for item in client.get("/api/credentials").json()["credentials"] if item["slot"] == "model")
    assert after["state"] == "cleared"
    assert after["credential_generation"] == second.json()["credential"]["credential_generation"] + 1
    assert not service._legacy_secrets_path.exists()

    # A delayed retry of the original DELETE must not clear newer Gateway
    # metadata after a later credential configuration has been installed.
    service.save_config({"name": "later-config", "model": "fake-model", "protocol": "chat_completions",
                         "base_url": "https://later-model.example/v1", "api_key": "fake-later-model-key",
                         "storage_mode": "process"})
    replay_delete = client.request("DELETE", "/api/credentials/model", json={
        "expected_generation": second.json()["credential"]["credential_generation"],
        "idempotency_key": "clear-api-legacy-key-01",
    })
    assert replay_delete.status_code == 200
    assert GatewayService(settings, protector=FakeProtector()).get_public_config()["configured"] is True


def test_credentials_status_accepts_fixture_token_string_and_empty_token(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    app = FastAPI()
    app.include_router(create_credentials_router(settings))
    object.__setattr__(settings.data_source, "token", "fixture-token")
    with TestClient(app) as client:
        credentials = client.get("/api/credentials")
        assert credentials.status_code == 200
        market = next(item for item in credentials.json()["credentials"] if item["slot"] == "market_data:tushare")
        assert market["configured"] is True and market["origin"] == "external_environment"
        object.__setattr__(settings.data_source, "token", "")
        empty = client.get("/api/credentials")
        assert empty.status_code == 200
        market = next(item for item in empty.json()["credentials"] if item["slot"] == "market_data:tushare")
        assert market["configured"] is False and market["masked"] is None


def test_tushare_endpoint_change_reports_rebind_target_and_clear_stays_disabled(tmp_path, monkeypatch):
    from app.agent_gateway import security

    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime"),
                             "APP_TUSHARE_API_URL": "https://new-market.example.invalid",
                             "TUSHARE_TOKEN": "fake-old-env-token"}, project_root=tmp_path)
    manager = CredentialManager(settings.runtime_dir, protector=FakeProtector())
    old_target = target_fingerprint("https://old-market.example.invalid")
    new_target = target_fingerprint(settings.data_source.api_base_url)
    manager.save(slot="market_data:tushare", provider="tushare", target_fingerprint=old_target,
                 secret="fake-old-managed-token", storage_mode="persistent", expected_generation=0,
                 idempotency_key="save-old-market-target-01")
    app = FastAPI()
    app.include_router(create_credentials_router(settings))
    client = TestClient(app)

    mismatch = next(item for item in client.get("/api/credentials").json()["credentials"]
                    if item["slot"] == "market_data:tushare")
    assert mismatch["reason"] == "target_mismatch"
    assert mismatch["target_fingerprint"] == new_target
    assert mismatch["target_fingerprint"] != old_target

    cleared = client.request("DELETE", "/api/credentials/market_data:tushare", json={
        "expected_generation": mismatch["credential_generation"], "idempotency_key": "clear-market-target-01",
    })
    assert cleared.status_code == 200
    tombstone = next(item for item in client.get("/api/credentials").json()["credentials"]
                     if item["slot"] == "market_data:tushare")
    assert tombstone["state"] == "cleared"
    assert tombstone["target_fingerprint"] == new_target
    with pytest.raises(CredentialError) as caught:
        manager.resolve("market_data:tushare", new_target, external_secret="fake-old-env-token")
    assert caught.value.reason == "credential_cleared"

    rebound = client.put("/api/credentials/market_data:tushare", json={
        "provider": "tushare", "target_fingerprint": tombstone["target_fingerprint"],
        "secret": "fake-new-market-token", "storage_mode": "persistent",
        "expected_generation": tombstone["credential_generation"], "idempotency_key": "rebind-market-target-01",
    })
    assert rebound.status_code == 200
    assert manager.resolve("market_data:tushare", new_target) == "fake-new-market-token"
    with pytest.raises(CredentialError) as caught:
        manager.resolve("market_data:tushare", old_target)
    assert caught.value.reason == "target_mismatch"


def test_credentials_api_never_returns_secret_and_clear_is_generation_fenced(tmp_path, monkeypatch):
    from app.agent_gateway import security

    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime"),
                             "APP_TUSHARE_API_URL": "https://api.example.invalid"}, project_root=tmp_path)
    app = FastAPI()
    app.include_router(create_credentials_router(settings))
    client = TestClient(app)
    target = target_fingerprint("https://api.example.invalid")
    secret = "fake-api-token-123456"
    response = client.put("/api/credentials/market_data:tushare", json={
        "provider": "tushare", "target_fingerprint": target, "secret": secret,
        "storage_mode": "persistent", "expected_generation": 0, "idempotency_key": "api-save-operation-0001",
    })
    assert response.status_code == 200
    assert secret not in response.text
    assert "masked" in response.json()["credential"]
    stale = client.request("DELETE", "/api/credentials/market_data:tushare", json={
        "expected_generation": 0, "idempotency_key": "api-stale-clear-0001",
    })
    assert stale.status_code == 409
    cleared = client.request("DELETE", "/api/credentials/market_data:tushare", json={
        "expected_generation": 1, "idempotency_key": "api-clear-operation-0001",
    })
    assert cleared.status_code == 200
    assert client.get("/api/credentials").json()["credentials"][1]["state"] == "cleared"


def test_model_clear_removes_gateway_configuration_history(tmp_path, monkeypatch):
    from app.agent_gateway import security

    monkeypatch.setattr(security, "WindowsDpapiProtector", FakeProtector)
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    gateway = GatewayService(settings, protector=FakeProtector())
    saved = gateway.save_config({"name": "test", "protocol": "chat_completions",
        "base_url": "https://model.example/v1", "model": "fake-model",
        "api_key": "fake-model-key-123456", "storage_mode": "persistent"})
    generation = CredentialManager(settings.runtime_dir, protector=FakeProtector()).status("model")["credential_generation"]
    app = FastAPI()
    app.include_router(create_credentials_router(settings))
    response = TestClient(app).request("DELETE", "/api/credentials/model", json={
        "expected_generation": generation, "idempotency_key": "clear-model-config-0001",
    })

    assert response.status_code == 200
    state = json.loads(gateway._config_path.read_text(encoding="utf-8"))
    assert state["active_version"] is None
    assert state["versions"] == []
    assert gateway.get_public_config()["configured"] is False
