"""适配器边界：请求、原始响应与规范化数据页。

适配器只做三件事：

1. 把“接口 + 参数”翻译成供应商请求并获取原始响应（归档内容不含凭据）；
2. 校验响应符合官方文档字段契约，缺契约字段视为错误而不是空成功；
3. 把原始行按类别规范化（单位换算见 :mod:`app.data.records`）。

适配器不负责重试、限频与写入；这些属于 :mod:`app.data.pipeline`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..digests import sha256_of_object
from ..errors import DataError
from ..timeutil import utc_now_iso


@dataclass(frozen=True)
class DataRequest:
    """一次逻辑取数请求（一页）。"""

    interface: str
    params: dict[str, Any] = field(default_factory=dict)
    page_no: int = 1
    range_hint: str | None = None

    def secret_free_params(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.params.items()
            if not any(marker in str(key).lower() for marker in ("token", "secret", "password"))
        }

    def log_params(self) -> dict[str, Any]:
        return self.secret_free_params()


@dataclass
class FetchOutcome:
    """一次取数的结果。

    ``status`` 取值：

    - ``ok``：响应成功且包含数据行；
    - ``empty``：响应成功但没有数据行（合法空范围或分页结束）；
    - ``error``：取数失败，``error`` 持有稳定原因码。
    """

    request: DataRequest
    status: str = "ok"
    rows: list[dict[str, Any]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    raw_bytes: bytes = b""
    declared_total: int | None = None
    error: DataError | None = None
    observed_at: str = field(default_factory=utc_now_iso)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status in ("ok", "empty")

    @property
    def error_reason(self) -> str | None:
        return self.error.reason if self.error is not None else None

    def content_fingerprint(self) -> str:
        """业务内容指纹：只看接口、参数与数据行，不含请求时间。"""
        return sha256_of_object(
            {
                "interface": self.request.interface,
                "params": self.request.log_params(),
                "rows": self.rows,
                "count": self.declared_total,
            }
        )


class DataAdapter:
    """取数适配器接口。"""

    source_code: str = ""

    def min_interval_seconds(self) -> float:
        """两次请求之间的最小间隔（限频口径）；离线夹具为 0。"""
        return 0.0

    def max_rows(self, interface: str) -> int:
        """单次请求的最大行数（文档口径），用于判断是否还有下一页。"""
        return 6000

    def fetch_page(self, request: DataRequest) -> FetchOutcome:
        raise NotImplementedError

    def probe(self) -> FetchOutcome:
        """轻量连通性探测；默认取接口的第一页。"""
        return self.fetch_page(DataRequest(interface="trade_cal", params={}, page_no=1))


__all__ = ["DataAdapter", "DataRequest", "FetchOutcome"]
