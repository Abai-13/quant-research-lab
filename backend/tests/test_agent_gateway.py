from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent_gateway import GatewayError, GatewayService
from app.agent_gateway.core import normalize_completion
from app.agent_gateway.security import WindowsDpapiProtector, normalize_base_url, resolve_public_addresses


class FakeProtector:
    """Only a test seam; production never uses this reversible protector."""

    def __init__(self, key: bytes = b"test-only"):
        self.key = key

    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ self.key[index % len(self.key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self.protect(ciphertext)


def make_settings(path: Path):
    return SimpleNamespace(runtime_dir=path / "TEMP")


def make_config(**changes):
    body = {
        "name": "Local test",
        "protocol": "chat_completions",
        "base_url": "https://api.example.com/v1",
        "model": "test-model",
        "api_key": "sk-test-secret-abcdefghijklmnopqrstuvwxyz",
    }
    body.update(changes)
    return body


def make_response(*, tool_calls=None, usage=None):
    message = {"role": "assistant", "content": None if tool_calls else "ok", "tool_calls": tool_calls}
    return {"object": "chat.completion", "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop"}], "usage": usage}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = make_settings(Path(self.temp.name))

    def test_key_round_trip_is_versioned_masked_and_not_plaintext(self):
        service = GatewayService(self.settings, protector=FakeProtector())
        public = service.save_config(make_config())
        key = make_config()["api_key"].encode()
        self.assertTrue(public["configured"])
        self.assertTrue(public["config"]["key_configured"])
        self.assertNotEqual(public["config"]["key_masked"], make_config()["api_key"])
        self.assertNotIn("api_key", json.dumps(public))
        self.assertNotIn(key, service._secrets_path.read_bytes())
        self.assertNotIn(key, service._config_path.read_bytes())
        secret_map = service._load_state()[1]
        self.assertEqual(secret_map[public["config"]["config_version"]], make_config()["api_key"])

    def test_target_change_requires_a_new_key(self):
        service = GatewayService(self.settings, protector=FakeProtector())
        public = service.save_config(make_config())
        with self.assertRaisesRegex(GatewayError, "API key"):
            service.save_config(make_config(base_url="https://other.example.com/v1",
                expected_config_version=public["config"]["config_version"], api_key=None))
        changed = service.save_config(make_config(base_url="https://other.example.com/v1",
            expected_config_version=public["config"]["config_version"]))
        self.assertNotEqual(changed["config"]["config_version"], public["config"]["config_version"])

    def test_missing_protection_material_fails_closed(self):
        service = GatewayService(self.settings, protector=FakeProtector())
        service.save_config(make_config())
        service._protector = FakeProtector(b"wrong key")
        with self.assertRaisesRegex(GatewayError, "cannot be decrypted"):
            service.get_public_config()
        with self.assertRaises(GatewayError) as caught:
            GatewayService(self.settings, protector=None).get_public_config()
        self.assertEqual(caught.exception.reason, "secret_store_unavailable")

    def test_explicit_reconfiguration_recovers_missing_protected_key(self):
        service = GatewayService(self.settings, protector=FakeProtector())
        initial = service.save_config(make_config())
        service._secrets_path.unlink()
        replacement_key = "sk-replacement-secret-abcdefghijklmnopqrstuvwxyz"
        active = service.save_config(make_config(api_key=replacement_key,
            expected_config_version=initial["config"]["config_version"]))
        self.assertTrue(active["configured"])
        secrets_by_version = service._load_state()[1]
        self.assertEqual(secrets_by_version, {active["config"]["config_version"]: replacement_key})

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is Windows-specific")
    def test_windows_dpapi_protects_and_round_trips_a_test_value(self):
        protector = WindowsDpapiProtector()
        value = b"test-only credential material"
        protected = protector.protect(value)
        self.assertNotEqual(protected, value)
        self.assertEqual(protector.unprotect(protected), value)

    def test_url_credentials_queries_private_addresses_and_http_are_rejected(self):
        urls = [
            "https://user:pass@example.com/v1", "https://example.com/v1?token=x",
            "https://example.com/v1#fragment", "https://127.0.0.1/v1",
            "http://example.com/v1", "https://[::ffff:127.0.0.1]/v1", "https://host name/v1",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_base_url(url)

    def test_explicit_loopback_http_and_private_https_options(self):
        endpoint = normalize_base_url("http://127.0.0.1:8080/v1", allow_loopback_http=True)
        self.assertTrue(endpoint.request_url.endswith("/v1/chat/completions"))
        self.assertTrue(normalize_base_url("https://10.20.30.40/v1", trusted_private_https=True).is_ip)
        with self.assertRaises(ValueError):
            normalize_base_url("https://10.20.30.40/v1")

    def test_mixed_dns_answer_is_rejected(self):
        def fake_getaddrinfo(host, port, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

        with patch("app.agent_gateway.security.socket.getaddrinfo", fake_getaddrinfo):
            with self.assertRaisesRegex(ValueError, "disallowed"):
                resolve_public_addresses("mixed.example", 443, allow_loopback_http=False,
                    trusted_private_https=False, scheme="https")

    def test_completion_normalization_rejects_malformed_tool_arguments(self):
        payload = make_response(tool_calls=[{"id": "call-1", "type": "function", "function": {
            "name": "lookup", "arguments": "[]"}}])
        with self.assertRaisesRegex(ValueError, "object"):
            normalize_completion(payload)
        valid = normalize_completion(make_response(usage={
            "prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}))
        self.assertEqual(valid["usage"], {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9})

    def test_completion_timeout_and_cancel(self):
        async def run():
            started = asyncio.Event()

            async def stalled(*args, **kwargs):
                started.set()
                await asyncio.sleep(5)

            service = GatewayService(self.settings, protector=FakeProtector(), transport=stalled)
            saved = service.save_config(make_config())
            cancel_event = asyncio.Event()
            task = asyncio.create_task(service.request_completion(
                saved["config"]["config_version"], [{"role": "user", "content": "hello"}], [],
                timeout_seconds=1, max_output_tokens=30, cancel_event=cancel_event))
            await started.wait()
            cancel_event.set()
            with self.assertRaises(GatewayError) as caught:
                await task
            self.assertEqual(caught.exception.reason, "cancelled")
            with self.assertRaises(GatewayError) as caught:
                await service.request_completion(saved["config"]["config_version"],
                    [{"role": "user", "content": "hello"}], [], timeout_seconds=0.05,
                    max_output_tokens=30, cancel_event=asyncio.Event())
            self.assertEqual(caught.exception.reason, "timeout")
        asyncio.run(run())

    def test_capability_detection_requires_consent_and_reports_real_usage(self):
        async def run():
            call = {"id": "call-1", "type": "function", "function": {
                "name": "gateway_capability_probe", "arguments": '{"value":"ok"}'}}

            async def fake_transport(endpoint, payload, api_key, timeout_seconds, **kwargs):
                self.assertEqual(api_key, make_config()["api_key"])
                self.assertEqual(payload["tools"][0]["function"]["name"], "gateway_capability_probe")
                return 200, make_response(tool_calls=[call], usage={
                    "prompt_tokens": 19, "completion_tokens": 5, "total_tokens": 24})

            service = GatewayService(self.settings, protector=FakeProtector(), transport=fake_transport)
            saved = service.save_config(make_config())
            with self.assertRaisesRegex(GatewayError, "consent"):
                await service.test_config({"config_version": saved["config"]["config_version"], "test_tools": True})
            result = await service.test_config({"config_version": saved["config"]["config_version"],
                                                "consent": True, "test_tools": True})
            self.assertTrue(result["reachable"])
            self.assertTrue(result["tool_supported"])
            self.assertEqual(result["usage"]["total_tokens"], 24)
        asyncio.run(run())

    def test_saved_limits_are_enforced_by_each_completion(self):
        async def run():
            async def unexpected_transport(*args, **kwargs):
                raise AssertionError("request must be rejected before transport")

            service = GatewayService(self.settings, protector=FakeProtector(), transport=unexpected_transport)
            saved = service.save_config(make_config(limits={"tokens": 10, "timeout_seconds": 5}))
            with self.assertRaises(GatewayError) as caught:
                await service.request_completion(saved["config"]["config_version"],
                    [{"role": "user", "content": "hello"}], [], timeout_seconds=5,
                    max_output_tokens=11, cancel_event=asyncio.Event())
            self.assertEqual(caught.exception.reason, "request_limit_exceeded")
            with self.assertRaises(GatewayError) as caught:
                await service.request_completion(saved["config"]["config_version"],
                    [{"role": "user", "content": "x" * 1_100_000}], [], timeout_seconds=5,
                    max_output_tokens=10, cancel_event=asyncio.Event())
            self.assertEqual(caught.exception.reason, "invalid_request")
        asyncio.run(run())
