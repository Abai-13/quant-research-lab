from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent_gateway import GatewayError, GatewayService


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0x5A for byte in value)

    def unprotect(self, value: bytes) -> bytes:
        return self.protect(value)


class GatewayResponseSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(runtime_dir=Path(self.temp.name) / "TEMP")
        self.key = "sk-test-secret-abcdefghijklmnopqrstuvwxyz"
        self.version = GatewayService(self.settings, protector=FakeProtector()).save_config({
            "name": "Test", "protocol": "chat_completions", "base_url": "https://api.example.com/v1",
            "model": "test", "api_key": self.key,
        })["config"]["config_version"]

    def test_cancelled_late_response_is_discarded(self):
        async def run():
            cancel = asyncio.Event()

            async def late_response(*args, **kwargs):
                cancel.set()
                return 200, {"object": "chat.completion", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "ok", "tool_calls": None},
                    "finish_reason": "stop"}], "usage": None}

            service = GatewayService(self.settings, protector=FakeProtector(), transport=late_response)
            with self.assertRaises(GatewayError) as caught:
                await service.request_completion(self.version, [{"role": "user", "content": "x"}], [],
                    timeout_seconds=1, max_output_tokens=20, cancel_event=cancel)
            self.assertEqual(caught.exception.reason, "cancelled")

        asyncio.run(run())

    def test_provider_echoing_key_never_reaches_caller(self):
        async def run():
            async def echo(*args, **kwargs):
                return 200, {"object": "chat.completion", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": self.key, "tool_calls": None},
                    "finish_reason": "stop"}], "usage": None}

            service = GatewayService(self.settings, protector=FakeProtector(), transport=echo)
            with self.assertRaises(GatewayError) as caught:
                await service.request_completion(self.version, [{"role": "user", "content": "x"}], [],
                    timeout_seconds=1, max_output_tokens=20, cancel_event=asyncio.Event())
            self.assertEqual(caught.exception.reason, "invalid_provider_response")
            self.assertNotIn(self.key, caught.exception.detail)

        asyncio.run(run())
