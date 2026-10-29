"""Versioned model configuration and explicit Chat Completions adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.config import Settings

from ..credentials import CredentialManager, CredentialError
from ..credentials.service import target_fingerprint

from .security import (
    SecretProtectionError,
    SecretProtector,
    WindowsDpapiProtector,
    normalize_base_url,
    protect_storage_path,
)
from .transport import TransportError, guarded_post

PROTOCOL = "chat_completions"
DEFAULT_LIMITS = {"rounds": 8, "tool_calls": 20, "timeout_seconds": 120, "tokens": 4096}
HARD_LIMITS = {"rounds": 8, "tool_calls": 20, "timeout_seconds": 120, "tokens": 8192}
_VERSION_RE = re.compile(r"^[0-9a-f]{32}$")
_CONFIG_LOCK = threading.RLock()


class GatewayError(RuntimeError):
    """Public-safe gateway failure with a stable reason and status."""

    def __init__(self, reason: str, detail: str, status_code: int = 400, *,
                 diagnostic: str | None = None, usage: dict[str, int | None] | None = None,
                 finish_reason: str | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code
        self.diagnostic = diagnostic
        self.usage = usage
        self.finish_reason = finish_reason


class ProviderResponseError(ValueError):
    """Response validation failure carrying only a fixed public diagnostic."""

    def __init__(self, diagnostic: str):
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


_SAFE_FINISH_REASONS = {"stop", "tool_calls", "length", "content_filter"}


def _safe_finish_reason(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    finish = choices[0].get("finish_reason")
    return finish if isinstance(finish, str) and finish in _SAFE_FINISH_REASONS else "unknown"


def _trusted_response_usage(response: object) -> dict[str, int | None]:
    empty: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if not isinstance(response, dict):
        return empty
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return empty
    values = (raw.get("prompt_tokens"), raw.get("completion_tokens"), raw.get("total_tokens"))
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
           for value in values):
        return empty
    prompt, completion, total = values
    if prompt is not None and completion is not None and total is not None and total != prompt + completion:
        return empty
    return {"input_tokens": prompt, "output_tokens": completion, "total_tokens": total}


def _key_mask(key: str) -> str:
    if len(key) < 8:
        return "••••••••"
    return f"{key[:3]}{'•' * min(max(len(key) - 6, 8), 24)}{key[-3:]}"


def _validate_limits(value: object) -> dict[str, int]:
    if value is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(value, dict):
        raise GatewayError("invalid_config", "Limits must be an object")
    limits = dict(DEFAULT_LIMITS)
    for key, item in value.items():
        if key not in limits or isinstance(item, bool) or not isinstance(item, int):
            raise GatewayError("invalid_config", "One or more limits are invalid")
        lower = 1
        if item < lower or item > HARD_LIMITS[key]:
            raise GatewayError("invalid_config", "One or more limits exceed the supported range")
        limits[key] = item
    return limits


def _safe_endpoint_config(raw: dict[str, Any]) -> dict[str, Any]:
    loopback = raw.get("allow_loopback_http", False)
    trusted = raw.get("trusted_private_https", False)
    if not isinstance(loopback, bool) or not isinstance(trusted, bool):
        raise GatewayError("invalid_config", "Endpoint trust options must be boolean")
    try:
        endpoint = normalize_base_url(raw.get("base_url"), allow_loopback_http=loopback,
                                      trusted_private_https=trusted)
    except ValueError as exc:
        raise GatewayError("unsafe_endpoint", str(exc)) from exc
    return {"base_url": endpoint.base_url, "allow_loopback_http": loopback,
            "trusted_private_https": trusted}


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protect_storage_path(path.parent)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        protect_storage_path(temp)
        os.replace(temp, path)
        protect_storage_path(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class GatewayService:
    """Service object with explicit test seams for secret protection and transport."""

    def __init__(self, settings: Settings, *, protector: SecretProtector | None = None,
                 transport: Callable[..., Any] | None = None):
        self.settings = settings
        self._protector = protector
        self._transport = transport or guarded_post
        self._directory = Path(settings.runtime_dir) / "agent" / "gateway"
        self._config_path = self._directory / "config.json"
        self._legacy_secrets_path = self._directory / "secrets.dpapi"

    def _get_protector(self) -> SecretProtector:
        if self._protector is not None:
            return self._protector
        try:
            self._protector = WindowsDpapiProtector()
            return self._protector
        except SecretProtectionError as exc:
            raise GatewayError("secret_store_unavailable", str(exc), 503) from exc

    def _load_state(self, *, required: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
        if not self._config_path.exists():
            if required:
                raise GatewayError("configuration_missing", "Model configuration is unavailable", 404)
            return {"active_version": None, "versions": []}, {}
        try:
            protect_storage_path(self._directory)
            state = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("versions"), list):
                raise ValueError
            secrets_by_version: dict[str, str] = {}
            active = next((item for item in state["versions"] if item.get("config_version") == state.get("active_version")), None)
            manager = self._credential_manager()
            if active and active.get("credential_generation") is not None:
                try:
                    key = manager.resolve("model", active["credential_target_fingerprint"],
                        expected_generation=active["credential_generation"])
                    secrets_by_version[active["config_version"]] = key
                except CredentialError as exc:
                    if exc.reason not in {"credential_missing", "credential_cleared", "process_credential_missing"}:
                        raise
            elif active and manager.has_managed_state("model"):
                # A newly managed value supersedes a legacy config even if a crash interrupted
                # binding its generation into config.json. Never fall back to the older DPAPI blob.
                target = self._credential_target(active)
                entry = manager.status("model", target_fingerprint=target)
                if entry["configured"]:
                    key = manager.resolve("model", target, expected_generation=entry["credential_generation"])
                    secrets_by_version[active["config_version"]] = key
            elif active and self._legacy_secrets_path.exists():
                # Phase 5 secrets.dpapi is read-only compatibility input.
                sealed = base64.b64decode(self._legacy_secrets_path.read_bytes(), validate=True)
                plain = self._get_protector().unprotect(sealed)
                legacy = json.loads(plain.decode("utf-8"))
                if not isinstance(legacy, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in legacy.items()):
                    raise ValueError
                secrets_by_version = legacy
            return state, secrets_by_version
        except GatewayError:
            raise
        except SecretProtectionError as exc:
            raise GatewayError("secret_store_unavailable", str(exc), 503) from exc
        except Exception as exc:
            raise GatewayError("secret_store_unavailable", "Stored configuration cannot be decrypted; reconfigure the API key", 503) from exc

    def _credential_manager(self) -> CredentialManager:
        return CredentialManager(self.settings.runtime_dir, protector=self._protector)

    @property
    def _secrets_path(self) -> Path:
        manager = self._credential_manager()
        generation = manager.status("model")["credential_generation"]
        return manager._secret_path("model", generation or 1)

    def clear_configuration_metadata(self) -> None:
        """Remove model endpoint/version history while retaining no legacy secret writes."""
        if not self._config_path.exists():
            return
        try:
            state = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("versions"), list):
                raise ValueError
            state["versions"] = []
            state["active_version"] = None
            _atomic_private_write(self._config_path, json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            # Explicit credential clear also discards the read-only phase 5 ciphertext.
            try:
                self._legacy_secrets_path.unlink()
            except FileNotFoundError:
                pass
        except Exception as exc:
            raise GatewayError("configuration_unavailable", "Model configuration could not be cleared", 503) from exc

    @staticmethod
    def _credential_target(record: dict[str, Any]) -> str:
        encoded = json.dumps({key: record[key] for key in ("base_url", "protocol", "allow_loopback_http", "trusted_private_https")},
                             separators=(",", ":"), sort_keys=True)
        return target_fingerprint(encoded)

    def get_public_config(self) -> dict[str, Any]:
        state, secret_map = self._load_state()
        active = state.get("active_version")
        record = next((item for item in state["versions"] if item.get("config_version") == active), None)
        if record is None:
            return {"configured": False, "config": None, "limits": dict(DEFAULT_LIMITS)}
        key = secret_map.get(active)
        configured = isinstance(key, str) and bool(key)
        if not configured:
            return {"configured": False, "config": None, "limits": record.get("limits", dict(DEFAULT_LIMITS))}
        public = {key: record[key] for key in (
            "config_version", "name", "protocol", "base_url", "model", "allow_loopback_http",
            "trusted_private_https", "limits")}
        public.update({"key_configured": True, "key_masked": _key_mask(key)})
        return {"configured": True, "config": public, "limits": dict(record["limits"])}

    def save_config(self, body: object) -> dict[str, Any]:
        with _CONFIG_LOCK:
            return self._save_config(body)

    def replace_active_credential(self, *, secret: str, storage_mode: str, target: str,
                                  expected_generation: int, idempotency_key: str) -> dict[str, Any]:
        """Replace the active model Key while keeping its validated endpoint binding."""
        if not isinstance(secret, str) or len(secret.strip()) < 8 or len(secret) > 4096 or "\r" in secret or "\n" in secret:
            raise GatewayError("invalid_config", "API key must contain at least 8 characters")
        if storage_mode not in {"persistent", "process"}:
            raise GatewayError("invalid_config", "Storage mode must be persistent or process")
        if not isinstance(target, str) or not re.fullmatch(r"[a-f0-9]{64}", target):
            raise GatewayError("invalid_config", "Credential target fingerprint is invalid")
        with _CONFIG_LOCK:
            state, _ = self._load_state(required=True)
            active_version = state.get("active_version")
            record = next((item for item in state["versions"] if item.get("config_version") == active_version), None)
            if record is None:
                raise GatewayError("configuration_missing", "Configure a model endpoint before saving its credential", 409)
            current_target = self._credential_target(record)
            if target != current_target:
                raise GatewayError("target_mismatch", "Reload the active model endpoint before saving its credential", 409)
            manager = self._credential_manager()
            try:
                public = manager.save(slot="model", provider="model", target_fingerprint=current_target,
                    secret=secret, storage_mode=storage_mode, expected_generation=expected_generation,
                    idempotency_key=idempotency_key)
            except CredentialError as exc:
                raise GatewayError(exc.reason, exc.detail, exc.status_code) from exc
            latest = manager.status("model", target_fingerprint=current_target)
            if public["target_fingerprint"] != current_target:
                raise GatewayError("idempotency_conflict", "Idempotent credential request belongs to a different model target", 409)
            if public["credential_generation"] < latest["credential_generation"]:
                # A replay of an older successful request must never roll config.json back.
                return public
            record["credential_target_fingerprint"] = current_target
            record["credential_generation"] = public["credential_generation"]
            try:
                _atomic_private_write(self._config_path,
                    json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                # Explicit replacement discards the old read-only compatibility ciphertext.
                try:
                    self._legacy_secrets_path.unlink()
                except FileNotFoundError:
                    pass
            except Exception as exc:
                raise GatewayError("secret_store_unavailable", "Model credential binding could not be stored", 503) from exc
            return public

    def _save_config(self, body: object) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GatewayError("invalid_config", "Configuration body must be an object")
        name, model, protocol = body.get("name"), body.get("model"), body.get("protocol", PROTOCOL)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise GatewayError("invalid_config", "Configuration name is required")
        if protocol != PROTOCOL:
            raise GatewayError("unsupported_protocol", "Only chat_completions is supported")
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 160:
            raise GatewayError("invalid_config", "Model name is required")
        endpoint = _safe_endpoint_config(body)
        limits = _validate_limits(body.get("limits"))
        api_key = body.get("api_key")
        storage_mode = body.get("storage_mode", "persistent")
        if storage_mode not in {"persistent", "process"}:
            raise GatewayError("invalid_config", "Storage mode must be persistent or process")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key.strip()) < 8 or len(api_key) > 4096
                                    or "\r" in api_key or "\n" in api_key):
            raise GatewayError("invalid_config", "API key must contain at least 8 characters")

        try:
            state, secrets_by_version = self._load_state()
        except GatewayError as exc:
            if exc.reason != "secret_store_unavailable" or not api_key:
                raise
            # An explicitly re-supplied key can recover from lost DPAPI material; old versions
            # remain descriptive but cannot be called until individually reconfigured.
            try:
                state = json.loads(self._config_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or not isinstance(state.get("versions"), list):
                    raise ValueError
                secrets_by_version = {}
            except Exception as load_error:
                raise GatewayError("secret_store_unavailable", "Stored configuration cannot be recovered; reconfigure it", 503) from load_error
        current_version = state.get("active_version")
        expected_version = body.get("expected_config_version")
        if expected_version is not None and expected_version != current_version:
            raise GatewayError("config_version_conflict", "Active configuration changed; reload and retry", 409)
        current = next((item for item in state["versions"] if item.get("config_version") == current_version), None)
        target_changed = current is None or any(
            current.get(key) != endpoint.get(key) for key in ("base_url", "allow_loopback_http", "trusted_private_https")
        ) or current.get("protocol") != protocol
        if target_changed and not api_key:
            raise GatewayError("key_required", "Provide the API key again when the protocol or target changes")
        if current_version is None and not api_key:
            raise GatewayError("key_required", "Provide an API key to configure the model")
        key_value = api_key if api_key is not None else secrets_by_version.get(current_version)
        if not key_value:
            raise GatewayError("key_required", "Stored API key is unavailable; reconfigure it")
        if current is not None and current.get("credential_generation") is None and api_key is None:
            raise GatewayError("key_required", "Re-enter the API key to move this legacy configuration into managed storage")
        version = secrets.token_hex(16)
        record = {
            "config_version": version,
            "name": name.strip(),
            "protocol": PROTOCOL,
            **endpoint,
            "model": model.strip(),
            "limits": limits,
        }
        if key_value in json.dumps(record, separators=(",", ":"), ensure_ascii=False):
            raise GatewayError("invalid_config", "API key cannot be included in public configuration fields")
        target = self._credential_target(record)
        manager = self._credential_manager()
        before = manager.status("model", target_fingerprint=target)
        generation = int(before["credential_generation"])
        if api_key is not None:
            try:
                managed = manager.save(slot="model", provider="model", target_fingerprint=target, secret=api_key,
                    storage_mode=storage_mode, expected_generation=generation, idempotency_key=f"gateway-config-{version}")
            except CredentialError as exc:
                raise GatewayError(exc.reason, exc.detail, exc.status_code) from exc
            generation = managed["credential_generation"]
        elif current and current.get("credential_target_fingerprint") != target:
            raise GatewayError("key_required", "Provide the API key again when the protocol or target changes")
        record["credential_target_fingerprint"] = target
        record["credential_generation"] = generation
        state["versions"].append(record)
        state["active_version"] = version
        try:
            _atomic_private_write(self._config_path, json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("secret_store_unavailable", "Configuration could not be stored securely", 503) from exc
        return self.get_public_config()

    async def request_completion(self, config_version: str, messages: list[dict[str, Any]],
                                 tools: list[dict[str, Any]], *, timeout_seconds: float,
                                 max_output_tokens: int, cancel_event: asyncio.Event) -> dict[str, Any]:
        if not isinstance(config_version, str) or not _VERSION_RE.fullmatch(config_version):
            raise GatewayError("configuration_missing", "Configuration version is unavailable", 404)
        if cancel_event.is_set():
            raise GatewayError("cancelled", "Model request cancelled", 409)
        state, secrets_by_version = self._load_state(required=True)
        record = next((item for item in state["versions"] if item.get("config_version") == config_version), None)
        api_key = secrets_by_version.get(config_version)
        if record is not None and record.get("credential_generation") is not None:
            try:
                api_key = self._credential_manager().resolve("model", record["credential_target_fingerprint"],
                    expected_generation=record["credential_generation"])
            except CredentialError as exc:
                raise GatewayError(exc.reason, exc.detail, exc.status_code) from exc
        if record is None or not isinstance(api_key, str) or not api_key:
            raise GatewayError("configuration_missing", "Configuration version or API key is unavailable", 404)
        if not isinstance(messages, list) or not messages or len(messages) > 100:
            raise GatewayError("invalid_request", "Messages are invalid")
        if not isinstance(tools, list) or len(tools) > 64:
            raise GatewayError("invalid_request", "Tools are invalid")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 120:
            raise GatewayError("invalid_request", "Request timeout is outside the supported range")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 8192:
            raise GatewayError("invalid_request", "Token limit is outside the supported range")
        if timeout_seconds > record["limits"]["timeout_seconds"] or max_output_tokens > record["limits"]["tokens"]:
            raise GatewayError("request_limit_exceeded", "Request exceeds the bound configuration limits", 400)
        if not all(isinstance(message, dict) and message.get("role") in {"system", "developer", "user", "assistant", "tool"}
                   for message in messages):
            raise GatewayError("invalid_request", "Messages contain an unsupported role")
        if not all(isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict)
                   for tool in tools):
            raise GatewayError("invalid_request", "Tool definitions are invalid")
        try:
            endpoint = normalize_base_url(record["base_url"], allow_loopback_http=record["allow_loopback_http"],
                                          trusted_private_https=record["trusted_private_https"])
            # Preserve an intentionally configured base path. Chat Completions endpoint is appended once.
            payload: dict[str, Any] = {"model": record["model"], "messages": messages,
                                       "max_tokens": max_output_tokens, "n": 1, "stream": False}
            if tools:
                payload.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
            if endpoint.hostname == "api.deepseek.com":
                # Agent requests do not retain DeepSeek reasoning_content between calls;
                # disable thinking for tool turns and fresh evidence-only summaries.
                payload["thinking"] = {"type": "disabled"}
            encoded_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            if len(encoded_payload.encode("utf-8")) > 1_048_576:
                raise GatewayError("invalid_request", "Model request exceeds the supported size")
            if api_key in encoded_payload:
                raise GatewayError("invalid_request", "API key cannot be included in model messages or tools")
        except GatewayError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise GatewayError("unsafe_endpoint", "Stored model endpoint is invalid", 400) from exc

        async def do_request() -> tuple[int, dict[str, Any]]:
            return await self._transport(
                endpoint, payload, api_key, float(timeout_seconds),
                allow_loopback_http=record["allow_loopback_http"],
                trusted_private_https=record["trusted_private_https"],
            )

        task = asyncio.create_task(do_request())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({task, cancel_wait}, timeout=float(timeout_seconds),
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done and cancel_event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise GatewayError("cancelled", "Model request cancelled", 409)
            if cancel_event.is_set():
                raise GatewayError("cancelled", "Model request cancelled", 409)
            if task not in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise GatewayError("timeout", "Model request timed out", 504)
            try:
                status, response = task.result()
            except TransportError as exc:
                raise GatewayError(exc.reason, "Could not reach the configured model endpoint", 502) from exc
            except asyncio.TimeoutError as exc:
                raise GatewayError("timeout", "Model request timed out", 504) from exc
            except Exception as exc:
                raise GatewayError("provider_error", "Model endpoint request failed", 502) from exc
            if status != 200:
                # Never expose provider response bodies; they can echo authorization material.
                raise GatewayError("provider_http_error", f"Model endpoint returned HTTP {status}", 502)
            try:
                try:
                    serialized_response = json.dumps(response, separators=(",", ":"), ensure_ascii=False)
                except (TypeError, ValueError):
                    serialized_response = ""
                if api_key in serialized_response:
                    raise GatewayError("invalid_provider_response", "Model endpoint returned an invalid response", 502,
                                       diagnostic="protected_data")
                normalized = normalize_completion(response)
                if cancel_event.is_set():
                    raise GatewayError("cancelled", "Model request cancelled", 409)
                return normalized
            except GatewayError:
                raise
            except ValueError as exc:
                diagnostic = exc.diagnostic if isinstance(exc, ProviderResponseError) else "invalid_provider_response"
                raise GatewayError("invalid_provider_response", "Model endpoint returned an invalid response", 502,
                                   diagnostic=diagnostic, usage=_trusted_response_usage(response),
                                   finish_reason=_safe_finish_reason(response)) from exc
        finally:
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def test_config(self, body: object) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("consent") is not True or body.get("test_tools") is not True:
            raise GatewayError("consent_required", "Connection test requires explicit consent and tool capability check", 400)
        version = body.get("config_version")
        state, _ = self._load_state(required=True)
        if version != state.get("active_version"):
            raise GatewayError("config_version_conflict", "Reload the active configuration before testing", 409)
        definition = {"type": "function", "function": {
            "name": "gateway_capability_probe", "description": "Return the supplied constant.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                           "required": ["value"], "additionalProperties": False},
        }}
        start = time.monotonic()
        try:
            active_record = next(item for item in state["versions"] if item.get("config_version") == version)
            result = await self.request_completion(
                version, [{"role": "user", "content": "Call gateway_capability_probe with value ok."}],
                [definition], timeout_seconds=min(20, active_record["limits"]["timeout_seconds"]),
                max_output_tokens=min(256, active_record["limits"]["tokens"]), cancel_event=asyncio.Event(),
            )
            calls = result["message"]["tool_calls"]
            supported = (len(calls) == 1 and calls[0]["function"]["name"] == "gateway_capability_probe"
                         and json.loads(calls[0]["function"]["arguments"]) == {"value": "ok"})
            return {"reachable": True, "tool_supported": supported,
                    "reason": None if supported else "tool_call_not_returned",
                    "diagnostic": None if supported else {"code": "probe_tool_mismatch"},
                    "usage": {**result["usage"], "elapsed_ms": int((time.monotonic() - start) * 1000)}}
        except GatewayError as exc:
            reachable = exc.reason in {"provider_http_error", "invalid_provider_response"}
            usage = exc.usage or {"input_tokens": None, "output_tokens": None, "total_tokens": None}
            diagnostic: dict[str, str] | None = None
            if exc.diagnostic == "protected_data":
                diagnostic = {"code": "protected_data"}
                usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
            elif exc.diagnostic is not None:
                diagnostic = {"code": exc.diagnostic, "finish_reason": exc.finish_reason}
            return {"reachable": reachable, "tool_supported": False, "reason": exc.reason,
                    "diagnostic": diagnostic,
                    "usage": {**usage, "elapsed_ms": int((time.monotonic() - start) * 1000)}}


def normalize_completion(response: object) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("object") != "chat.completion":
        raise ProviderResponseError("invalid_completion_object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderResponseError("invalid_choices")
    choice = choices[0]
    if choice.get("index") != 0:
        raise ProviderResponseError("invalid_choice_index")
    finish = choice.get("finish_reason")
    if not isinstance(finish, str) or finish not in _SAFE_FINISH_REASONS:
        raise ProviderResponseError("invalid_finish_reason")
    raw_message = choice.get("message")
    if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
        raise ProviderResponseError("invalid_assistant_message")
    content = raw_message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderResponseError("invalid_content")
    raw_calls = raw_message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list) or len(raw_calls) > 32:
        raise ProviderResponseError("invalid_tool_calls")
    normalized_calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderResponseError("invalid_tool_call")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128 or call_id in seen:
            raise ProviderResponseError("invalid_tool_id")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ProviderResponseError("invalid_tool_function")
        name, arguments = function["name"], function.get("arguments")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or not isinstance(arguments, str) or len(arguments) > 65536:
            raise ProviderResponseError("invalid_tool_arguments")
        def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ProviderResponseError("duplicate_tool_argument_key")
                result[key] = value
            return result

        try:
            decoded = json.loads(arguments, object_pairs_hook=unique_pairs,
                                 parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
        except json.JSONDecodeError as exc:
            raise ProviderResponseError("invalid_tool_arguments_json") from exc
        except ValueError as exc:
            if isinstance(exc, ProviderResponseError):
                raise
            raise ProviderResponseError("invalid_tool_arguments_json") from exc
        if not isinstance(decoded, dict):
            raise ProviderResponseError("tool_arguments_not_object")
        normalized_calls.append({"id": call_id, "type": "function",
                                 "function": {"name": name, "arguments": json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)}})
        seen.add(call_id)
    if finish == "tool_calls" and not normalized_calls:
        raise ProviderResponseError("tool_finish_without_call")
    usage_raw = response.get("usage")
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if usage_raw is not None:
        if not isinstance(usage_raw, dict):
            raise ProviderResponseError("invalid_usage")
        for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
            item = usage_raw.get(source)
            if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
                raise ProviderResponseError("invalid_usage")
            usage[target] = item
        if (usage["input_tokens"] is not None and usage["output_tokens"] is not None
                and usage["total_tokens"] is not None
                and usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]):
            raise ProviderResponseError("inconsistent_usage")
    return {"message": {"role": "assistant", "content": content, "tool_calls": normalized_calls},
            "usage": usage, "finish_reason": finish}


def get_public_config(settings: Settings) -> dict[str, Any]:
    return GatewayService(settings).get_public_config()


def save_config(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    return GatewayService(settings).save_config(body)


async def test_config(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    return await GatewayService(settings).test_config(body)


async def request_completion(settings: Settings, config_version: str, messages: list[dict[str, Any]],
                             tools: list[dict[str, Any]], *, timeout_seconds: float,
                             max_output_tokens: int, cancel_event: asyncio.Event) -> dict[str, Any]:
    return await GatewayService(settings).request_completion(
        config_version, messages, tools, timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens, cancel_event=cancel_event,
    )
