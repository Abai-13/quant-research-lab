from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent_gateway import GatewayService


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0x5A for byte in value)

    def unprotect(self, value: bytes) -> bytes:
        return self.protect(value)


class DeepSeekProviderCompatibilityTests(unittest.TestCase):
    def test_only_official_hostname_gets_thinking_disabled_for_tool_and_summary_requests(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                settings = SimpleNamespace(runtime_dir=Path(directory) / "TEMP")
                captured = []

                async def fake_transport(endpoint, payload, api_key, timeout_seconds, **kwargs):
                    captured.append((endpoint, dict(payload)))
                    return 200, {"object": "chat.completion", "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "ok", "tool_calls": None},
                        "finish_reason": "stop"}], "usage": None}

                service = GatewayService(settings, protector=FakeProtector(), transport=fake_transport)
                tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
                cases = [
                    ("https://api.deepseek.com/v1", "ordinary-model", True),
                    ("https://api.deepseek.com/custom/v1", "ordinary-model", True),
                    ("https://api.example.com/v1", "deepseek-chat", False),
                    ("https://deepseek.example.com/v1", "ordinary-model", False),
                    ("https://api.deepseek.com.attacker.example/v1", "ordinary-model", False),
                ]
                for index, (base_url, model, expected) in enumerate(cases):
                    saved = service.save_config({
                        "name": f"Offline test {index}",
                        "protocol": "chat_completions",
                        "base_url": base_url,
                        "model": model,
                        "api_key": f"sk-fake-test-key-{index}-not-real",
                    })
                    for tool_request in (True, False):
                        await service.request_completion(saved["config"]["config_version"],
                            [{"role": "user", "content": "call lookup"}], [tool] if tool_request else [], timeout_seconds=1,
                            max_output_tokens=32, cancel_event=asyncio.Event())
                        endpoint, payload = captured[-1]
                        self.assertEqual("thinking" in payload, expected, (base_url, tool_request))
                        if expected:
                            self.assertEqual(payload["thinking"], {"type": "disabled"})
                        else:
                            self.assertNotIn("thinking", payload)

                self.assertTrue(captured[0][0].request_url.endswith("/v1/chat/completions"))
                self.assertTrue(captured[2][0].request_url.endswith("/custom/v1/chat/completions"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
