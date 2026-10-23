"""离线夹具适配器（仅用于测试与演示，绝不冒充真实行情）。

夹具目录下的每个 ``<interface>.json`` 描述若干条“请求匹配 → 分页响应”：

```json
{
  "interface": "daily",
  "max_rows": 6000,
  "responses": [
    {
      "match": {"trade_date": "20240102"},
      "pages": [
        {"fields": ["ts_code", "trade_date"], "items": [["000001.SZ", "20240102"]], "total": 1}
      ]
    }
  ]
}
```

- ``match`` 中的键必须与请求参数一致（按字符串比较），匹配不到时按“合法空范围”返回；
- 分页超出范围时返回空页，用于验证分页结束判定；
- 分页可以带 ``error`` 段，用于离线复现限频、权限、契约错误与缺页。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import (
    ContractError,
    CredentialsRequiredError,
    DataError,
    NoPermissionError,
    PageIncompleteError,
    RateLimitedError,
    TransportError,
    UnknownApiError,
)
from .base import DataAdapter, DataRequest, FetchOutcome

_ERROR_BUILDERS = {
    "transport_error": lambda msg: TransportError(msg or "模拟网络失败"),
    "rate_limited": lambda msg: RateLimitedError(msg or "模拟限频"),
    "no_permission": lambda msg: NoPermissionError(msg or "模拟无权限"),
    "contract_error": lambda msg: ContractError(msg or "模拟字段契约错误"),
    "page_incomplete": lambda msg: PageIncompleteError(msg or "模拟缺页"),
    "unknown_api_error": lambda msg: UnknownApiError(msg or "模拟未知错误"),
    "credentials_required": lambda msg: CredentialsRequiredError(msg or "模拟缺少凭据"),
}


class FixtureAdapter(DataAdapter):
    """从本地 JSON 夹具读取响应。"""

    source_code = "tushare-fixture"

    def __init__(self, fixtures_dir: Path) -> None:
        self._dir = Path(fixtures_dir)
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def fixtures_dir(self) -> Path:
        return self._dir

    def _load(self, interface: str) -> dict[str, Any]:
        if interface in self._cache:
            return self._cache[interface]
        path = self._dir / f"{interface}.json"
        if not path.is_file():
            self._cache[interface] = {"responses": [], "max_rows": 6000}
            return self._cache[interface]
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._cache[interface] = payload
        return payload

    def max_rows(self, interface: str) -> int:
        return int(self._load(interface).get("max_rows") or 6000)

    def _matching_response(self, payload: dict[str, Any], request: DataRequest) -> dict[str, Any] | None:
        params = {key: str(value) for key, value in request.params.items()}
        for entry in payload.get("responses", []):
            match = entry.get("match") or {}
            if all(str(params.get(key)) == str(value) for key, value in match.items()):
                return entry
        return None

    def fetch_page(self, request: DataRequest) -> FetchOutcome:
        payload = self._load(request.interface)
        entry = self._matching_response(payload, request)
        if entry is None:
            return FetchOutcome(request=request, status="empty", raw_bytes=b"")
        pages = entry.get("pages") or []
        index = max(request.page_no, 1) - 1
        if index >= len(pages):
            return FetchOutcome(request=request, status="empty", raw_bytes=b"")
        page = pages[index]
        if "error" in page:
            error_spec = page.get("error") or {}
            kind = str(error_spec.get("kind") or "unknown_api_error")
            message = str(error_spec.get("message") or "")
            builder = _ERROR_BUILDERS.get(kind)
            error: DataError = (
                builder(message) if builder is not None else UnknownApiError(message or kind)
            )
            return FetchOutcome(request=request, status="error", error=error)
        fields = list(page.get("fields") or [])
        items = list(page.get("items") or [])
        rows = [dict(zip(fields, item)) for item in items]
        declared_total = page.get("total")
        raw_bytes = json.dumps(
            {"code": 0, "msg": None, "data": {"fields": fields, "items": items}},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return FetchOutcome(
            request=request,
            status="ok" if rows else "empty",
            rows=rows,
            fields=fields,
            raw_bytes=raw_bytes,
            declared_total=int(declared_total) if isinstance(declared_total, int) else None,
        )


__all__ = ["FixtureAdapter"]
