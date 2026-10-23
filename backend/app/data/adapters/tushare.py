"""Tushare 取数适配器（真实访问）。

- 只使用官方通用接口 ``POST {api_base_url}``，请求体为
  ``{"api_name", "token", "params", "fields"}``；
- 令牌只放在请求体中，不写日志、不进档案、不进配置快照；
- 供应商返回 ``code != 0`` 时按官方错误语义分类：权限、限频、未知错误分别处理，
  权限与契约错误不重试；
- 文档未逐项核验的字段一律记为未知，不在代码里假造覆盖结论。

参考：https://tushare.pro/document/2 （核验日期见来源能力矩阵）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..errors import (
    ContractError,
    CredentialsRequiredError,
    NoPermissionError,
    RateLimitedError,
    TransportError,
    UnknownApiError,
)
from .base import DataAdapter, DataRequest, FetchOutcome

# 官方文档给出的单次返回行数上限（未在真实账号上核验，保持文档口径）。
MAX_ROWS: dict[str, int] = {
    "trade_cal": 6000,
    "stock_basic": 6000,
    "daily": 6000,
    "adj_factor": 6000,
    "suspend_d": 5000,
    "dividend": 2000,
}

# 各接口需要的官方字段清单（用于声明请求字段，不用于校验返回结构）。
REQUEST_FIELDS: dict[str, str] = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "stock_basic": "ts_code,symbol,name,market,exchange,list_date,delist_date,list_status",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_type,suspend_timing",
    "dividend": (
        "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,"
        "cash_div_tax,record_date,ex_date,pay_date,div_listdate,base_share"
    ),
}

_PERMISSION_MARKERS = ("权限", "积分", "没有访问", "无权限", "订阅")
_RATE_MARKERS = ("每分钟", "频次", "超过", "限流", "太多", "次数")
_CONTRACT_MARKERS = ("字段", "参数", "缺少", "不支持")


def classify_api_error(code: int, message: str) -> Exception:
    """把供应商错误文本映射为稳定原因码；不猜测，保留未知。"""
    text = (message or "").strip()
    if any(marker in text for marker in _PERMISSION_MARKERS):
        return NoPermissionError(f"数据源拒绝访问该接口（code={code}）", text)
    if any(marker in text for marker in _RATE_MARKERS):
        return RateLimitedError(f"数据源限频（code={code}）", text)
    if any(marker in text for marker in _CONTRACT_MARKERS):
        return ContractError(f"数据源报告请求参数或字段问题（code={code}）", text)
    return UnknownApiError(f"数据源返回未知错误（code={code}）", text)


class TushareAdapter(DataAdapter):
    """真实 Tushare 适配器。"""

    source_code = "tushare"

    def __init__(
        self,
        *,
        api_base_url: str,
        token: str | None,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 0.35,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._token = token or ""
        self._timeout = timeout_seconds
        self._min_interval = max(0.0, min_interval_seconds)
        self._transport = transport

    @property
    def has_credentials(self) -> bool:
        return bool(self._token)

    def min_interval_seconds(self) -> float:
        return self._min_interval

    def max_rows(self, interface: str) -> int:
        return MAX_ROWS.get(interface, 6000)

    def _request_body(self, request: DataRequest) -> dict[str, Any]:
        params = dict(request.params)
        if request.page_no > 1:
            params.setdefault("limit", str(self.max_rows(request.interface)))
            params.setdefault("offset", str(self.max_rows(request.interface) * (request.page_no - 1)))
        return {
            "api_name": request.interface,
            "token": self._token,
            "params": params,
            "fields": REQUEST_FIELDS.get(request.interface, ""),
        }

    def fetch_page(self, request: DataRequest) -> FetchOutcome:
        if not self._token:
            return FetchOutcome(
                request=request,
                status="error",
                error=CredentialsRequiredError(),
            )
        body = self._request_body(request)
        raw_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(self._api_base_url, content=raw_bytes, headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            return FetchOutcome(request=request, status="error", error=TransportError("请求超时", str(exc)))
        except httpx.HTTPError as exc:
            return FetchOutcome(request=request, status="error", error=TransportError("网络请求失败", str(exc)))

        if response.status_code in (401, 403):
            return FetchOutcome(
                request=request,
                status="error",
                error=NoPermissionError(f"数据源返回 HTTP {response.status_code}", None),
            )
        if response.status_code == 429:
            return FetchOutcome(
                request=request,
                status="error",
                error=RateLimitedError("数据源返回 HTTP 429（限频）", None),
            )
        if response.status_code >= 400:
            return FetchOutcome(
                request=request,
                status="error",
                error=UnknownApiError(f"数据源返回 HTTP {response.status_code}", None),
            )

        payload_bytes = response.content
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return FetchOutcome(
                request=request,
                status="error",
                error=ContractError("数据源响应不是合法 JSON", str(exc)),
            )
        if not isinstance(payload, dict):
            return FetchOutcome(
                request=request,
                status="error",
                error=ContractError("数据源响应结构不是对象", None),
            )
        code = payload.get("code")
        if code not in (0, None):
            error = classify_api_error(int(code), str(payload.get("msg") or ""))
            return FetchOutcome(request=request, status="error", error=error)
        data = payload.get("data")
        if not isinstance(data, dict) or "items" not in data or "fields" not in data:
            return FetchOutcome(
                request=request,
                status="error",
                error=ContractError("数据源响应缺少 data.fields / data.items", None),
            )
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not isinstance(fields, list) or not isinstance(items, list):
            return FetchOutcome(
                request=request,
                status="error",
                error=ContractError("数据源响应的 fields/items 不是数组", None),
            )
        rows = [dict(zip(fields, item)) for item in items]
        return FetchOutcome(
            request=request,
            status="ok" if rows else "empty",
            rows=rows,
            fields=list(fields),
            raw_bytes=payload_bytes,
            declared_total=None,
        )


__all__ = ["MAX_ROWS", "REQUEST_FIELDS", "TushareAdapter", "classify_api_error"]
