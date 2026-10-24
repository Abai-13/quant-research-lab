"""Secret protection and endpoint validation for the model gateway."""

from __future__ import annotations

import ctypes
import ipaddress
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit


class SecretProtectionError(RuntimeError):
    """Protection material is unavailable or failed; plaintext fallback is forbidden."""


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class WindowsDpapiProtector:
    """Per-user Windows DPAPI protection; DPAPI holds the master material outside the DB."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretProtectionError("OS-protected secret storage is unavailable")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @classmethod
    def _blob(cls, value: bytes) -> tuple["WindowsDpapiProtector._Blob", object]:
        buffer = ctypes.create_string_buffer(value)
        blob = cls._Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def _crypt(self, value: bytes, decrypt: bool) -> bytes:
        source, source_buffer = self._blob(value)
        target = self._Blob()
        if decrypt:
            function = self._crypt32.CryptUnprotectData
            function.argtypes = [ctypes.POINTER(self._Blob), ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                 ctypes.POINTER(self._Blob)]
            function.restype = ctypes.c_int
            ok = function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(target))
        else:
            function = self._crypt32.CryptProtectData
            function.argtypes = [ctypes.POINTER(self._Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                 ctypes.POINTER(self._Blob)]
            function.restype = ctypes.c_int
            ok = function(ctypes.byref(source), "AI Quant Simulator model credential", None,
                          None, None, 0x1, ctypes.byref(target))
        _ = source_buffer
        if not ok:
            raise SecretProtectionError("OS secret protection is unavailable; reconfigure the API key")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            self._kernel32.LocalFree(target.pbData)

    def protect(self, plaintext: bytes) -> bytes:
        return self._crypt(plaintext, False)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._crypt(ciphertext, True)


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    request_url: str
    scheme: str
    hostname: str
    port: int
    path: str
    is_ip: bool


def normalize_base_url(value: object, *, allow_loopback_http: bool = False,
                       trusted_private_https: bool = False) -> Endpoint:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Base URL is invalid")
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError
        if "\\" in value or "%" in parts.netloc:
            raise ValueError
        host = parts.hostname.rstrip(".").lower()
        if not host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            normalized_host = address.compressed
            is_ip = True
            loopback = address.is_loopback
            private = not address.is_global
        except ValueError:
            normalized_host = host.encode("idna").decode("ascii")
            is_ip = False
            loopback = normalized_host == "localhost" or normalized_host.endswith(".localhost")
            private = False
        parsed_port = parts.port
        port = parsed_port if parsed_port is not None else (443 if parts.scheme.lower() == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        scheme = parts.scheme.lower()
        if not is_ip:
            labels = normalized_host.split(".")
            if len(normalized_host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
            ):
                raise ValueError
        if scheme == "http" and not (allow_loopback_http and loopback):
            raise ValueError
        if scheme == "https" and private:
            safe_private_ip = (is_ip and (address.is_loopback or
                               (address.is_private and not address.is_link_local and not address.is_multicast
                                and not address.is_reserved and not address.is_unspecified)))
            if not trusted_private_https or not safe_private_ip:
                raise ValueError
        if scheme == "https" and not private and not is_ip and trusted_private_https:
            # The opt-in is only meaningful for explicitly configured private IP endpoints.
            trusted_private_https = False
        authority_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        default_port = 443 if scheme == "https" else 80
        authority = authority_host if port == default_port else f"{authority_host}:{port}"
        path = quote((parts.path or "").rstrip("/"), safe="/%:@!$&'()*+,;=-._~")
        canonical = urlunsplit((scheme, authority, path, "", ""))
        request_url = canonical + "/chat/completions"
        return Endpoint(canonical, request_url, scheme, normalized_host, port, path, is_ip)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Base URL is not an allowed HTTP(S) endpoint") from exc


def resolve_public_addresses(hostname: str, port: int, *, allow_loopback_http: bool,
                             trusted_private_https: bool, scheme: str) -> tuple[str, ...]:
    """Resolve once per connection and reject mixed or non-public answers by default."""
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = (literal.compressed,)
    except ValueError:
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Endpoint DNS resolution failed") from exc
        addresses = tuple(dict.fromkeys(ipaddress.ip_address(row[4][0]).compressed for row in records))
    if not addresses:
        raise ValueError("Endpoint has no addresses")
    parsed = [ipaddress.ip_address(item) for item in addresses]
    if any(item.is_loopback for item in parsed):
        if scheme == "http" and allow_loopback_http and all(item.is_loopback for item in parsed):
            return addresses
        if scheme == "https" and trusted_private_https and all(item.is_loopback for item in parsed):
            return addresses
        raise ValueError("Endpoint resolves to a disallowed address")
    if any(not item.is_global for item in parsed):
        safe_private = all(item.is_private and not item.is_link_local and not item.is_multicast
                           and not item.is_reserved and not item.is_unspecified for item in parsed)
        if scheme == "https" and trusted_private_https and safe_private:
            return addresses
        raise ValueError("Endpoint resolves to a disallowed address")
    return addresses


def protect_storage_path(path: Path) -> None:
    """Restrict persistent gateway files to the current OS identity."""
    if sys.platform == "win32":
        try:
            who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, check=True, timeout=5).stdout
            sid = next((field.strip().strip('"') for field in who.split(",")
                        if field.strip().strip('"').startswith("S-1-")), None)
            if not sid:
                raise OSError
            grant = f"*{sid}:(OI)(CI)F" if path.is_dir() else f"*{sid}:F"
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                           capture_output=True, text=True, check=True, timeout=10)
            return
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretProtectionError("Cannot establish private gateway storage permissions") from exc
    try:
        path.chmod(0o700 if path.is_dir() else 0o600)
    except OSError as exc:
        raise SecretProtectionError("Cannot establish private gateway storage permissions") from exc
