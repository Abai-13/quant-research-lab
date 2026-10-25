"""DPAPI protected credential storage with explicit process-only support.

Only metadata and generation fences are stored as JSON. Persistent secret values are
DPAPI protected; process mode keeps secret bytes only in this interpreter's memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from ..agent_gateway.security import SecretProtector

_LOCK = threading.RLock()
_PROCESS_SECRETS: dict[str, dict[str, str]] = {}
_SLOT_RE = re.compile(r"^[a-z][a-z0-9:_-]{0,79}$")
_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")


class CredentialError(RuntimeError):
    def __init__(self, reason: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


def target_fingerprint(value: str) -> str:
    """Return a stable opaque fingerprint for a provider target."""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _atomic_private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from ..agent_gateway.security import protect_storage_path

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        protect_storage_path(path.parent)
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.kernel32.SetFileAttributesW(str(temporary), 0x2)
            except Exception:
                pass
        os.replace(temporary, path)
        protect_storage_path(path)
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("credential_store_unavailable", "Credential storage permissions or file access are unavailable", 503) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class CredentialManager:
    """One runtime-scoped credential manager. The protector is injectable for tests."""

    def __init__(self, runtime_dir: Path | str, *, protector: SecretProtector | None = None):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.directory = self.runtime_dir / "credentials"
        self._metadata_path = self.directory / "state.json"
        self._protector = protector
        self._runtime_key = str(self.runtime_dir)

    def _get_protector(self) -> SecretProtector:
        if self._protector is not None:
            return self._protector
        try:
            from ..agent_gateway.security import SecretProtectionError, WindowsDpapiProtector

            self._protector = WindowsDpapiProtector()
            return self._protector
        except Exception as exc:
            raise CredentialError("secret_store_unavailable", "Protected credential storage is unavailable", 503) from exc

    def _state(self) -> dict[str, Any]:
        if not self._metadata_path.exists():
            return {"schema_version": 1, "slots": {}}
        try:
            state = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != 1 or not isinstance(state.get("slots"), dict):
                raise ValueError
            for slot, entry in state["slots"].items():
                if not _SLOT_RE.fullmatch(slot) or not isinstance(entry, dict):
                    raise ValueError
                if not isinstance(entry.get("generation"), int) or entry["generation"] < 0:
                    raise ValueError
            for slot, entry in state["slots"].items():
                keep_generation = entry.get("secret_generation") if entry.get("state") == "configured" and entry.get("storage_mode") == "persistent" else None
                self._cleanup_secret_files(slot, keep_generation)
            return state
        except Exception as exc:
            raise CredentialError("credential_state_unavailable", "Credential state cannot be read", 503) from exc

    def _write_state(self, state: Mapping[str, Any]) -> None:
        _atomic_private_write(self._metadata_path, json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    def _secret_stem(self, slot: str) -> str:
        suffix = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:20]
        return f"secret-{suffix}"

    def _secret_path(self, slot: str, generation: int) -> Path:
        return self.directory / f"{self._secret_stem(slot)}-{generation}.dpapi"

    def _cleanup_secret_files(self, slot: str, keep_generation: int | None) -> None:
        prefix = self._secret_stem(slot) + "-"
        keep_name = self._secret_path(slot, keep_generation).name if keep_generation is not None else None
        for path in self.directory.glob(f"{prefix}*.dpapi"):
            if path.name != keep_name:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _persistent_secret(self, slot: str, generation: int) -> str | None:
        path = self._secret_path(slot, generation)
        if not path.exists():
            return None
        try:
            sealed = base64.b64decode(path.read_bytes(), validate=True)
            decoded = json.loads(self._get_protector().unprotect(sealed).decode("utf-8"))
            if not isinstance(decoded, dict) or set(decoded) != {"secret"} or not isinstance(decoded.get("secret"), str):
                raise ValueError
            return decoded["secret"]
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("secret_store_unavailable", "Protected credential cannot be decrypted; reconfigure it", 503) from exc

    @staticmethod
    def _validate(slot: str, provider: str, target: str, secret: str, storage_mode: str) -> None:
        if not isinstance(slot, str) or not _SLOT_RE.fullmatch(slot):
            raise CredentialError("invalid_credential", "Credential slot is invalid")
        if not isinstance(provider, str) or not provider or len(provider) > 80:
            raise CredentialError("invalid_credential", "Credential provider is invalid")
        if not isinstance(target, str) or not _FINGERPRINT_RE.fullmatch(target):
            raise CredentialError("invalid_credential", "Credential target fingerprint is invalid")
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 8192 or "\r" in secret or "\n" in secret:
            raise CredentialError("invalid_credential", "Credential value is invalid")
        if storage_mode not in {"persistent", "process"}:
            raise CredentialError("invalid_credential", "Storage mode must be persistent or process")

    def _public(self, slot: str, entry: Mapping[str, Any] | None, *, external_secret: str | None = None,
                external_provider: str | None = None, target: str | None = None) -> dict[str, Any]:
        generation = int((entry or {}).get("generation", 0))
        if entry and entry.get("state") == "cleared":
            return {"slot": slot, "provider": entry.get("provider", "unknown"),
                    "target_fingerprint": target if target is not None else entry.get("target_fingerprint"),
                    "state": "cleared", "storage_mode": entry.get("storage_mode", "persistent"), "configured": False,
                    "masked": None, "credential_generation": generation, "origin": "none", "reason": "cleared"}
        if entry and entry.get("state") == "configured":
            mode = entry.get("storage_mode")
            secret = None
            reason = None
            if mode == "persistent":
                try:
                    secret = self._persistent_secret(slot, int(entry.get("secret_generation", generation)))
                except CredentialError:
                    reason = "protected_credential_unavailable"
            elif mode == "process":
                secret = _PROCESS_SECRETS.get(self._runtime_key, {}).get(slot)
                if not secret:
                    reason = "process_credential_missing_after_restart"
            configured = bool(secret and (target is None or target == entry.get("target_fingerprint")))
            return {"slot": slot, "provider": entry["provider"],
                    "target_fingerprint": target if target is not None else entry["target_fingerprint"],
                    "state": "configured" if configured else ("unavailable" if reason or secret else "missing"),
                    "storage_mode": mode, "configured": configured, "masked": _mask(secret) if configured else None,
                    "credential_generation": generation, "origin": "managed" if configured else "none",
                    "reason": reason or ("target_mismatch" if secret and target and target != entry.get("target_fingerprint") else None)}
        if external_secret:
            return {"slot": slot, "provider": external_provider or "external", "target_fingerprint": target,
                    "state": "configured", "storage_mode": "process", "configured": True, "masked": _mask(external_secret),
                    "credential_generation": generation, "origin": "external_environment", "reason": None}
        return {"slot": slot, "provider": (entry or {}).get("provider", "unknown"),
                "target_fingerprint": target if target is not None else (entry or {}).get("target_fingerprint"),
                "state": "missing", "storage_mode": None,
                "configured": False, "masked": None, "credential_generation": generation, "origin": "none", "reason": None}

    def status(self, slot: str, *, external_secret: str | None = None, external_provider: str | None = None,
               target_fingerprint: str | None = None) -> dict[str, Any]:
        with _LOCK:
            state = self._state()
            entry = state["slots"].get(slot)
            return self._public(slot, entry, external_secret=external_secret, external_provider=external_provider,
                                target=target_fingerprint)

    def save(self, *, slot: str, provider: str, target_fingerprint: str, secret: str,
             storage_mode: str, expected_generation: int, idempotency_key: str) -> dict[str, Any]:
        self._validate(slot, provider, target_fingerprint, secret, storage_mode)
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise CredentialError("invalid_credential", "expected_generation is required")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CredentialError("invalid_credential", "idempotency_key is required")
        idem_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        payload_hash = hmac.new(idempotency_key.encode(),
            json.dumps([slot, provider, target_fingerprint, secret, storage_mode], separators=(",", ":")).encode(),
            hashlib.sha256).hexdigest()
        with _LOCK:
            state = self._state()
            idem = state.setdefault("idempotency", {})
            if idem_hash in idem:
                receipt = idem[idem_hash]
                if receipt.get("payload_hash") != payload_hash:
                    raise CredentialError("idempotency_conflict", "Idempotency key was reused with different input", 409)
                return dict(receipt["result"])
            previous = state["slots"].get(slot)
            generation = int((previous or {}).get("generation", 0))
            if generation != expected_generation:
                raise CredentialError("generation_conflict", "Credential state changed; reload and retry", 409)
            next_generation = generation + 1
            if storage_mode == "persistent":
                # Protect before writing either metadata or secret files. Never fall back to plaintext.
                try:
                    protected = self._get_protector().protect(json.dumps({"secret": secret}, separators=(",", ":")).encode("utf-8"))
                except Exception as exc:
                    raise CredentialError("secret_store_unavailable", "Protected credential storage is unavailable", 503) from exc
                _atomic_private_write(self._secret_path(slot, next_generation), base64.b64encode(protected))
            else:
                _PROCESS_SECRETS.setdefault(self._runtime_key, {})[slot] = secret
            state["slots"][slot] = {"provider": provider, "target_fingerprint": target_fingerprint,
                                    "state": "configured", "storage_mode": storage_mode,
                                    "origin": "managed", "generation": next_generation}
            if storage_mode == "persistent":
                state["slots"][slot]["secret_generation"] = next_generation
            result = self._public(slot, state["slots"][slot])
            idem[idem_hash] = {"payload_hash": payload_hash, "result": result}
            while len(idem) > 256:
                idem.pop(next(iter(idem)))
            self._write_state(state)
            if storage_mode == "persistent":
                _PROCESS_SECRETS.get(self._runtime_key, {}).pop(slot, None)
            self._cleanup_secret_files(slot, next_generation if storage_mode == "persistent" else None)
            return dict(result)

    def clear(self, *, slot: str, expected_generation: int, idempotency_key: str) -> dict[str, Any]:
        if not isinstance(slot, str) or not _SLOT_RE.fullmatch(slot):
            raise CredentialError("invalid_credential", "Credential slot is invalid")
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise CredentialError("invalid_credential", "expected_generation is required")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CredentialError("invalid_credential", "idempotency_key is required")
        idem_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        payload_hash = hashlib.sha256(f"clear:{slot}".encode()).hexdigest()
        with _LOCK:
            state = self._state()
            idem = state.setdefault("idempotency", {})
            if idem_hash in idem:
                receipt = idem[idem_hash]
                if receipt.get("payload_hash") != payload_hash:
                    raise CredentialError("idempotency_conflict", "Idempotency key was reused with different input", 409)
                return dict(receipt["result"])
            previous = state["slots"].get(slot)
            generation = int((previous or {}).get("generation", 0))
            if generation != expected_generation:
                raise CredentialError("generation_conflict", "Credential state changed; reload and retry", 409)
            state["slots"][slot] = {"provider": (previous or {}).get("provider", "unknown"),
                                    "target_fingerprint": (previous or {}).get("target_fingerprint"),
                                    "state": "cleared", "storage_mode": (previous or {}).get("storage_mode", "persistent"),
                                    "origin": "none", "generation": generation + 1}
            self._write_state(state)
            result = self._public(slot, state["slots"][slot])
            idem[idem_hash] = {"payload_hash": payload_hash, "result": result}
            while len(idem) > 256:
                idem.pop(next(iter(idem)))
            self._write_state(state)
            _PROCESS_SECRETS.get(self._runtime_key, {}).pop(slot, None)
            self._cleanup_secret_files(slot, None)
            return dict(result)

    def resolve(self, slot: str, target: str, *, expected_generation: int | None = None,
                external_secret: str | None = None, external_provider: str | None = None,
                legacy_secret: str | None = None) -> str:
        if not isinstance(slot, str) or not _SLOT_RE.fullmatch(slot) or not _FINGERPRINT_RE.fullmatch(target or ""):
            raise CredentialError("invalid_credential", "Credential reference is invalid")
        with _LOCK:
            entry = self._state()["slots"].get(slot)
            generation = int((entry or {}).get("generation", 0))
            if expected_generation is not None and generation != expected_generation:
                raise CredentialError("generation_conflict", "Credential generation changed", 409)
            if entry and entry.get("state") == "cleared":
                raise CredentialError("credential_cleared", "Credential was explicitly cleared", 409)
            if entry and entry.get("state") == "configured":
                if entry.get("target_fingerprint") != target:
                    raise CredentialError("target_mismatch", "Credential is bound to a different provider target", 409)
                if entry.get("storage_mode") == "persistent":
                    secret = self._persistent_secret(slot, int(entry.get("secret_generation", generation)))
                else:
                    secret = _PROCESS_SECRETS.get(self._runtime_key, {}).get(slot)
                    if not secret:
                        raise CredentialError("process_credential_missing", "Process credential is unavailable after restart; reconfigure it", 409)
                if not secret:
                    raise CredentialError("credential_unavailable", "Configured credential is unavailable; reconfigure it", 503)
                return secret
            if legacy_secret:
                return legacy_secret
            if external_secret:
                return external_secret
            raise CredentialError("credential_missing", "Credential is not configured", 404)

    def has_tombstone(self, slot: str) -> bool:
        with _LOCK:
            entry = self._state()["slots"].get(slot)
            return bool(entry and entry.get("state") == "cleared")

    def has_managed_state(self, slot: str) -> bool:
        """Whether a managed configured/cleared entry exists for this slot."""
        with _LOCK:
            return slot in self._state()["slots"]


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return f"{'*' * max(4, min(12, len(secret) - 4))}{secret[-4:]}"


class CredentialTokenSource:
    """Lazy Tushare token reference passed through the existing adapter factory."""

    def __init__(self, runtime_dir: Path, target: str, external_secret: str | None, *, provider: str = "tushare"):
        self.runtime_dir = Path(runtime_dir)
        self.target = target
        self.external_secret = external_secret or None
        self.provider = provider

    def resolve(self) -> str:
        return CredentialManager(self.runtime_dir).resolve("market_data:tushare", self.target,
            external_secret=self.external_secret, external_provider=self.provider)

    def __bool__(self) -> bool:
        return bool(self.external_secret) or CredentialManager(self.runtime_dir).status(
            "market_data:tushare", external_secret=self.external_secret, external_provider=self.provider,
            target_fingerprint=self.target)["configured"]


def close_process_credentials(runtime_dir: Path | str | None = None) -> None:
    """Drop process-only credential material during application shutdown."""
    with _LOCK:
        if runtime_dir is None:
            _PROCESS_SECRETS.clear()
            return
        key = str(Path(runtime_dir).resolve())
        _PROCESS_SECRETS.pop(key, None)


def resolve_credential(slot: str, target_fingerprint: str, expected_generation: int | None = None,
                       *, runtime_dir: Path | str, external_secret: str | None = None) -> str:
    return CredentialManager(runtime_dir).resolve(slot, target_fingerprint, expected_generation=expected_generation,
                                                  external_secret=external_secret)


def credential_generation(runtime_dir: Path | str, slot: str) -> int:
    return int(CredentialManager(runtime_dir).status(slot)["credential_generation"])
