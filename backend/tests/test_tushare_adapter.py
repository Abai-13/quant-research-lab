"""Tushare 适配器：响应契约校验与错误分类（全部使用本地 MockTransport，不触网）。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.data.adapters.base import DataRequest
from app.data.adapters.tushare import TushareAdapter, classify_api_error
from app.data.errors import (
    ContractError,
    CredentialsRequiredError,
    NoPermissionError,
    RateLimitedError,
    TransportError,
    UnknownApiError,
)

TOKEN = "test-token-value-not-real"


def _adapter(handler, **kwargs) -> TushareAdapter:
    return TushareAdapter(
        api_base_url="https://api.example.invalid",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _ok_payload(items, fields=("ts_code", "trade_date", "close")) -> dict:
    return {"code": 0, "msg": None, "data": {"fields": list(fields), "items": items}}


def test_successful_response_is_parsed_into_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload([["000001.SZ", "20240102", 10.0]]))

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={"ts_code": "000001.SZ"}))

    assert outcome.status == "ok"
    assert outcome.rows == [{"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0}]
    assert outcome.error is None


def test_empty_response_is_a_legitimate_empty_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload([]))

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={}))

    assert outcome.status == "empty"
    assert outcome.is_success is True
    assert outcome.rows == []


def test_token_is_sent_in_request_body_but_never_in_response_archive() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=_ok_payload([["000001.SZ", "20240102", 10.0]]))

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={}))

    assert TOKEN in seen["body"]
    assert TOKEN not in outcome.raw_bytes.decode("utf-8")


def test_permission_error_is_terminal_and_not_retried() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"code": 40203, "msg": "抱歉，您没有访问该接口的权限"})

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={}))

    assert outcome.status == "error"
    assert isinstance(outcome.error, NoPermissionError)
    assert calls["count"] == 1


def test_rate_limit_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40209, "msg": "每分钟最多访问该接口500次"})

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={}))

    assert isinstance(outcome.error, RateLimitedError)


def test_unknown_api_error_is_not_guessed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 50001, "msg": "系统繁忙"})

    adapter = _adapter(handler)
    outcome = adapter.fetch_page(DataRequest(interface="daily", params={}))

    assert isinstance(outcome.error, UnknownApiError)


def test_http_status_codes_are_classified() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    def throttled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    assert isinstance(_adapter(forbidden).fetch_page(DataRequest(interface="daily")).error, NoPermissionError)
    assert isinstance(_adapter(throttled).fetch_page(DataRequest(interface="daily")).error, RateLimitedError)


def test_contract_violations_are_errors_not_empty_success() -> None:
    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>error</html>")

    def missing_fields(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    first = _adapter(not_json).fetch_page(DataRequest(interface="daily"))
    second = _adapter(missing_fields).fetch_page(DataRequest(interface="daily"))

    assert isinstance(first.error, ContractError)
    assert isinstance(second.error, ContractError)
    assert first.status == "error"
    assert second.status == "error"


def test_network_failure_is_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    outcome = _adapter(handler).fetch_page(DataRequest(interface="daily"))

    assert isinstance(outcome.error, TransportError)


def test_missing_credentials_blocks_real_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("没有凭据时不应发出请求")

    adapter = TushareAdapter(
        api_base_url="https://api.example.invalid", token=None, transport=httpx.MockTransport(handler)
    )
    outcome = adapter.fetch_page(DataRequest(interface="daily"))

    assert isinstance(outcome.error, CredentialsRequiredError)


def test_error_classifier_keeps_unknown_errors_unknown() -> None:
    assert isinstance(classify_api_error(1, "随机错误"), UnknownApiError)
    assert isinstance(classify_api_error(1, "积分不足"), NoPermissionError)
    assert isinstance(classify_api_error(1, "超过访问频次"), RateLimitedError)


def test_request_params_are_logged_without_secrets() -> None:
    request = DataRequest(interface="daily", params={"ts_code": "000001.SZ", "token": TOKEN})

    logged = request.log_params()

    assert "token" not in logged
    assert logged["ts_code"] == "000001.SZ"
    assert TOKEN not in json.dumps(logged)
