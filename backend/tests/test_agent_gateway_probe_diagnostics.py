from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent_gateway import GatewayService

_DEFAULT_USAGE = object()


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0x5A for byte in value)

    def unprotect(self, value: bytes) -> bytes:
        return self.protect(value)


def completion(*, name="gateway_capability_probe", arguments='{"value":"ok"}', finish="tool_calls", usage=_DEFAULT_USAGE):
    if usage is _DEFAULT_USAGE:
        usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    return {"object": "chat.completion", "choices": [{"index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": "probe-1", "type": "function", "function": {
                             "name": name, "arguments": arguments}}]}}], "usage": usage}


class ProbeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(runtime_dir=Path(self.temp.name) / "TEMP")
        self.key = "sk-test-secret-abcdefghijklmnopqrstuvwxyz"

    def service(self, response=None, *, error=None, capture=None):
        async def transport(_endpoint, payload, _api_key, _timeout, **_kwargs):
            if capture is not None:
                capture.append(payload)
            if error is not None:
                raise error
            return 200, response

        service = GatewayService(self.settings, protector=FakeProtector(), transport=transport)
        saved = service.save_config({"name": "Probe test", "protocol": "chat_completions",
            "base_url": "https://api.example.com/v1", "model": "test", "api_key": self.key})
        self.version = saved["config"]["config_version"]
        return service

    def probe(self, service):
        return asyncio.run(service.test_config({"config_version": self.version, "consent": True, "test_tools": True}))

    def test_truncated_arguments_keep_consistent_usage_and_safe_finish_reason(self):
        service = self.service(completion(arguments='{"value":'))
        result = self.probe(service)
        self.assertEqual(result["reason"], "invalid_provider_response")
        self.assertEqual(result["diagnostic"], {"code": "invalid_tool_arguments_json", "finish_reason": "tool_calls"})
        self.assertEqual({key: result["usage"][key] for key in ("input_tokens", "output_tokens", "total_tokens")},
                         {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16})

        result = self.probe(self.service(completion(arguments='{"value":"ok","value":"ok"}')))
        self.assertEqual(result["diagnostic"]["code"], "duplicate_tool_argument_key")
        self.assertEqual(result["usage"]["total_tokens"], 16)

    def test_unknown_or_inconsistent_usage_is_not_reported_as_zero(self):
        for usage in (None, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99},
                      {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}):
            with self.subTest(usage=usage):
                result = self.probe(self.service(completion(arguments="{", usage=usage)))
                self.assertIsNone(result["usage"]["input_tokens"])
                self.assertIsNone(result["usage"]["output_tokens"])
                self.assertIsNone(result["usage"]["total_tokens"])

    def test_invalid_object_and_unknown_finish_are_safe(self):
        invalid_object = {"object": {"private": "hidden"}, "choices": []}
        result = self.probe(self.service(invalid_object))
        self.assertEqual(result["diagnostic"]["code"], "invalid_completion_object")
        unknown_finish = completion(finish="provider-secret-value")
        result = self.probe(self.service(unknown_finish))
        self.assertEqual(result["diagnostic"], {"code": "invalid_finish_reason", "finish_reason": "unknown"})
        self.assertNotIn("provider-secret-value", str(result))

    def test_protected_response_or_exception_is_never_exposed(self):
        response = completion(arguments="{", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
        response["provider_debug"] = self.key
        result = self.probe(self.service(response))
        self.assertEqual(result["diagnostic"], {"code": "protected_data"})
        self.assertTrue(all(result["usage"][key] is None for key in ("input_tokens", "output_tokens", "total_tokens")))
        self.assertNotIn(self.key, str(result))

        service = self.service(error=RuntimeError(f"provider leaked {self.key}"))
        result = self.probe(service)
        self.assertEqual(result["reason"], "provider_error")
        self.assertNotIn(self.key, str(result))

    def test_probe_requires_exact_tool_and_value_and_retains_valid_compatibility(self):
        for name, arguments in (("other_tool", '{"value":"ok"}'),
                                ("gateway_capability_probe", '{"value":"not-ok"}')):
            result = self.probe(self.service(completion(name=name, arguments=arguments)))
            self.assertTrue(result["reachable"])
            self.assertFalse(result["tool_supported"])
            self.assertEqual(result["diagnostic"], {"code": "probe_tool_mismatch"})
        captured = []
        result = self.probe(self.service(completion(), capture=captured))
        self.assertTrue(result["reachable"])
        self.assertTrue(result["tool_supported"])
        self.assertIsNone(result["diagnostic"])
        self.assertEqual(captured[0]["max_tokens"], 256)


if __name__ == "__main__":
    unittest.main()
