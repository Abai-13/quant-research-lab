"""Single-attempt HTTP/1.1 transport with validated DNS pinning and TLS hostname checks."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from typing import Any

from .security import Endpoint

_MAX_RESPONSE_BYTES = 1_048_576
_MAX_HEADER_BYTES = 32_768


class TransportError(RuntimeError):
    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


async def _readline(reader: asyncio.StreamReader, maximum: int = 8192) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise TransportError("Malformed endpoint response") from exc
    if len(line) > maximum:
        raise TransportError("Endpoint response headers are too large")
    return line[:-2]


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, list[str]]) -> bytes:
    lengths = headers.get("content-length", [])
    transfer = headers.get("transfer-encoding", [])
    if len(lengths) > 1 or (lengths and transfer):
        raise TransportError("Malformed endpoint response")
    if lengths:
        try:
            size = int(lengths[0])
        except ValueError as exc:
            raise TransportError("Malformed endpoint response") from exc
        if size < 0 or size > _MAX_RESPONSE_BYTES:
            raise TransportError("Endpoint response is too large")
        body = await reader.readexactly(size)
    elif transfer:
        if len(transfer) != 1 or transfer[0].lower() != "chunked":
            raise TransportError("Unsupported endpoint response framing")
        parts: list[bytes] = []
        total = 0
        while True:
            line = await _readline(reader, 128)
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError as exc:
                raise TransportError("Malformed endpoint response") from exc
            if size < 0 or total + size > _MAX_RESPONSE_BYTES:
                raise TransportError("Endpoint response is too large")
            if size == 0:
                for _ in range(64):
                    trailer = await _readline(reader, 8192)
                    if not trailer:
                        return b"".join(parts)
                raise TransportError("Malformed endpoint response")
            part = await reader.readexactly(size)
            if await reader.readexactly(2) != b"\r\n":
                raise TransportError("Malformed endpoint response")
            parts.append(part)
            total += size
    else:
        parts = []
        total = 0
        while True:
            part = await reader.read(min(65536, _MAX_RESPONSE_BYTES + 1 - total))
            if not part:
                break
            parts.append(part)
            total += len(part)
            if total > _MAX_RESPONSE_BYTES:
                raise TransportError("Endpoint response is too large")
        body = b"".join(parts)
    encodings = headers.get("content-encoding", ["identity"])
    if any(value.lower() not in {"identity"} for value in encodings):
        raise TransportError("Unsupported endpoint response encoding")
    return body


async def post_json(endpoint: Endpoint, addresses: tuple[str, ...], payload: dict[str, Any],
                    api_key: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    """POST exactly once, connecting to a validated IP and verifying the original TLS name."""
    last_error: Exception | None = None
    # Multiple globally routable A/AAAA answers are acceptable; retrying is deliberately absent.
    # Connect only to the first address returned by the validation pass.
    address = addresses[0]
    writer: asyncio.StreamWriter | None = None
    try:
        context = ssl.create_default_context() if endpoint.scheme == "https" else None
        reader, writer = await asyncio.open_connection(
            host=address,
            port=endpoint.port,
            ssl=context,
            server_hostname=endpoint.hostname if context and not endpoint.is_ip else None,
            limit=_MAX_HEADER_BYTES,
        )
        authority_host = f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
        default_port = 443 if endpoint.scheme == "https" else 80
        authority = authority_host if endpoint.port == default_port else f"{authority_host}:{endpoint.port}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request_path = endpoint.path + "/chat/completions"
        headers = (
            f"POST {request_path} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Authorization: Bearer {api_key}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: application/json\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()
        status_line = await _readline(reader, 1024)
        try:
            version, raw_status, _reason = status_line.decode("ascii").split(" ", 2)
            status = int(raw_status)
            if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status <= 599:
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise TransportError("Malformed endpoint response") from exc
        # Reject informational responses and protocol upgrades; no redirect is followed.
        if status < 200 or status == 101:
            return status, {}
        parsed_headers: dict[str, list[str]] = {}
        consumed = 0
        for _ in range(100):
            line = await _readline(reader)
            consumed += len(line) + 2
            if consumed > _MAX_HEADER_BYTES:
                raise TransportError("Endpoint response headers are too large")
            if not line:
                break
            if line[:1] in b" \t" or b":" not in line:
                raise TransportError("Malformed endpoint response")
            key, value = line.split(b":", 1)
            try:
                name = key.decode("ascii").lower()
                text = value.decode("ascii").strip()
            except UnicodeError as exc:
                raise TransportError("Malformed endpoint response") from exc
            parsed_headers.setdefault(name, []).append(text)
        else:
            raise TransportError("Endpoint response has too many headers")
        response = await _read_body(reader, parsed_headers)
        if status != 200:
            return status, {}
        try:
            value = json.loads(response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TransportError("Endpoint returned invalid JSON", status) from exc
        if not isinstance(value, dict):
            raise TransportError("Endpoint returned an invalid response", status)
        return status, value
    except TransportError:
        raise
    except (OSError, ssl.SSLError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
        last_error = exc
        raise TransportError("Could not reach the configured model endpoint") from last_error
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass


async def guarded_post(endpoint: Endpoint, payload: dict[str, Any], api_key: str,
                       timeout_seconds: float, *, allow_loopback_http: bool,
                       trusted_private_https: bool) -> tuple[int, dict[str, Any]]:
    """Re-resolve each request; require every DNS answer to pass before pinning one IP."""
    try:
        try:
            literal = ipaddress.ip_address(endpoint.hostname)
            records = [(0, 0, 0, "", (literal.compressed, endpoint.port))]
        except ValueError:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(endpoint.hostname, endpoint.port,
                                                       type=socket.SOCK_STREAM),
                timeout=timeout_seconds,
            )
        addresses = tuple(dict.fromkeys(ipaddress.ip_address(row[4][0]).compressed for row in records))
        parsed = [ipaddress.ip_address(item) for item in addresses]
        if not addresses:
            raise ValueError("Endpoint has no addresses")
        if any(item.is_loopback for item in parsed):
            if endpoint.scheme == "http" and allow_loopback_http and all(item.is_loopback for item in parsed):
                pass
            elif endpoint.scheme == "https" and trusted_private_https and all(item.is_loopback for item in parsed):
                pass
            else:
                raise ValueError("Endpoint resolves to a disallowed address")
        elif any(not item.is_global for item in parsed):
            safe_private = all(item.is_private and not item.is_link_local and not item.is_multicast
                               and not item.is_reserved and not item.is_unspecified for item in parsed)
            if endpoint.scheme != "https" or not trusted_private_https or not safe_private:
                raise ValueError("Endpoint resolves to a disallowed address")
    except asyncio.TimeoutError:
        raise
    except ValueError as exc:
        raise TransportError(str(exc)) from exc
    except OSError as exc:
        raise TransportError("Endpoint DNS resolution failed") from exc
    return await asyncio.wait_for(post_json(endpoint, addresses, payload, api_key, timeout_seconds),
                                  timeout=timeout_seconds)
