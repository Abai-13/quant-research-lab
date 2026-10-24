from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from app.agent_gateway.security import normalize_base_url
from app.agent_gateway.transport import guarded_post


class GatewayTransportTests(unittest.TestCase):
    def test_scheme_specific_nondefault_ports_are_preserved(self):
        for url, expected in [
            ("https://api.example.com:80/v1", "https://api.example.com:80/v1"),
            ("http://127.0.0.1:443/v1", "http://127.0.0.1:443/v1"),
        ]:
            with self.subTest(url=url):
                endpoint = normalize_base_url(url, allow_loopback_http=url.startswith("http://"))
                self.assertEqual(endpoint.base_url, expected)

    def test_guarded_http_transport_uses_loopback_opt_in_and_never_follows_redirect(self):
        async def run():
            captured = {}
            crlf = bytes((13, 10))

            async def handler(reader, writer):
                request = await reader.readuntil(crlf + crlf)
                captured["request"] = request
                headers = {}
                for line in request.split(crlf)[1:]:
                    if b":" in line:
                        key, value = line.split(b":", 1)
                        headers[key.lower()] = value.strip()
                body = await reader.readexactly(int(headers[b"content-length"]))
                captured["body"] = json.loads(body)
                response_body = b'{"id":"x","object":"chat.completion","choices":[],"usage":null}'
                prefix = (b"HTTP/1.1 302 Found" + crlf + b"Location: http://127.0.0.1:9/steal" + crlf
                          + b"Content-Length: " + str(len(response_body)).encode() + crlf
                          + b"Connection: close" + crlf + crlf)
                writer.write(prefix + response_body)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                endpoint = normalize_base_url(f"http://127.0.0.1:{port}/v1", allow_loopback_http=True)
                status, response = await guarded_post(endpoint, {"model": "test", "messages": []},
                    "test-key", 2, allow_loopback_http=True, trusted_private_https=False)
                self.assertEqual((status, response), (302, {}))
                self.assertTrue(captured["request"].startswith(b"POST /v1/chat/completions HTTP/1.1"))
                self.assertIn(b"Authorization: Bearer test-key", captured["request"])
                self.assertEqual(captured["body"]["model"], "test")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())

    def test_dns_timeout_is_bounded_before_any_connect(self):
        async def run():
            loop = asyncio.get_running_loop()

            async def stalled_resolve(*args, **kwargs):
                await asyncio.sleep(2)

            with patch.object(loop, "getaddrinfo", stalled_resolve):
                endpoint = normalize_base_url("https://slow.example.com/v1")
                with self.assertRaises(asyncio.TimeoutError):
                    await guarded_post(endpoint, {"model": "test"}, "test-key", 0.05,
                        allow_loopback_http=False, trusted_private_https=False)

        asyncio.run(run())
