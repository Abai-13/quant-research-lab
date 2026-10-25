"""Public status and explicit lifecycle operations for provider credentials."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..credentials import CredentialError, CredentialManager
from ..config import Settings
from ..agent_gateway.core import GatewayError, GatewayService

_SLOTS = {
    "model": ("model", None),
    "market_data:tushare": ("tushare", "market_data:tushare"),
}


def create_credentials_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/credentials", tags=["credentials"])
    manager = CredentialManager(settings.runtime_dir)

    def public_slots() -> list[dict[str, Any]]:
        result = []
        for slot, (provider, _env_slot) in _SLOTS.items():
            external = None
            gateway_public: dict[str, Any] | None = None
            model_target: str | None = None
            if slot == "market_data:tushare":
                token_source = settings.data_source.token
                if isinstance(token_source, str):
                    # Supported fixture-mode settings may carry the token as
                    # a plain string; never infer or load a real credential.
                    external = token_source or None
                    target = None
                else:
                    external = getattr(token_source, "external_secret", None)
                    target = getattr(token_source, "target", None)
            else:
                target = None
                try:
                    gateway = GatewayService(settings)
                    gateway_public = gateway.get_public_config()
                    config = gateway_public.get("config")
                    if gateway_public.get("configured") and isinstance(config, dict):
                        model_target = GatewayService._credential_target(config)
                    else:
                        state, _ = gateway._load_state()
                        active = next((item for item in state.get("versions", [])
                                       if item.get("config_version") == state.get("active_version")), None)
                        if isinstance(active, dict):
                            model_target = GatewayService._credential_target(active)
                except GatewayError:
                    pass
                target = model_target
            public = manager.status(slot, external_secret=external, external_provider=provider,
                                    target_fingerprint=target)
            if (slot == "model" and not manager.has_managed_state(slot) and gateway_public
                    and gateway_public.get("configured")):
                # Phase 5's active config and secrets.dpapi remain a supported read-only source.
                # Reflect the credential the Gateway can actually use so the UI can clear/replace it.
                config = gateway_public.get("config")
                public.update({
                    "provider": "model",
                    "target_fingerprint": model_target,
                    "state": "configured",
                    "storage_mode": "persistent",
                    "configured": True,
                    "masked": config.get("key_masked") if isinstance(config, dict) else None,
                    "origin": "managed",
                    "reason": None,
                })
            result.append(public)
        return result

    @router.get("")
    async def get_credentials() -> dict[str, Any]:
        return {"ok": True, "credentials": public_slots()}

    @router.put("/{slot:path}")
    async def put_credential(slot: str, request: Request) -> JSONResponse:
        if slot not in _SLOTS:
            return JSONResponse({"ok": False, "reason": "credential_slot_invalid", "detail": "Credential slot is unsupported."}, status_code=404)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise CredentialError("invalid_credential", "Request body is invalid")
            if slot == "model":
                try:
                    result = GatewayService(settings).replace_active_credential(
                        secret=body.get("secret"), storage_mode=body.get("storage_mode"),
                        target=body.get("target_fingerprint"), expected_generation=body.get("expected_generation"),
                        idempotency_key=body.get("idempotency_key"),
                    )
                except GatewayError as exc:
                    return JSONResponse({"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=exc.status_code)
                return JSONResponse({"ok": True, "credential": result})
            result = manager.save(
                slot=slot,
                provider=body.get("provider", _SLOTS[slot][0]),
                target_fingerprint=body.get("target_fingerprint"),
                secret=body.get("secret"),
                storage_mode=body.get("storage_mode"),
                expected_generation=body.get("expected_generation"),
                idempotency_key=body.get("idempotency_key"),
            )
            return JSONResponse({"ok": True, "credential": result})
        except CredentialError as exc:
            return JSONResponse({"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=exc.status_code)
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_credential", "detail": "Request body is invalid."}, status_code=400)

    @router.delete("/{slot:path}")
    async def delete_credential(slot: str, request: Request) -> JSONResponse:
        if slot not in _SLOTS:
            return JSONResponse({"ok": False, "reason": "credential_slot_invalid", "detail": "Credential slot is unsupported."}, status_code=404)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise CredentialError("invalid_credential", "Request body is invalid")
            result = manager.clear(slot=slot, expected_generation=body.get("expected_generation"),
                                  idempotency_key=body.get("idempotency_key"))
            current = manager.status(slot)
            applied = (result.get("credential_generation") == body.get("expected_generation", -1) + 1
                       and current.get("credential_generation") == result.get("credential_generation")
                       and current.get("state") == "cleared")
            if slot == "model" and applied:
                try:
                    GatewayService(settings).clear_configuration_metadata()
                except GatewayError as exc:
                    return JSONResponse({"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=exc.status_code)
            elif slot == "market_data:tushare" and applied:
                token_source = settings.data_source.token
                if hasattr(token_source, "external_secret"):
                    token_source.external_secret = None
            return JSONResponse({"ok": True, "credential": result})
        except CredentialError as exc:
            return JSONResponse({"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=exc.status_code)
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_credential", "detail": "Request body is invalid."}, status_code=400)

    return router
