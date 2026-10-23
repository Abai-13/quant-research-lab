"""稳定原因码与异常类型。

所有面向状态接口和报告的错误都持有稳定 ``reason`` 代码，不把内部异常文本
当作可诊断信息来源；凭据与密钥类错误绝不携带密钥内容。
"""

from __future__ import annotations


class DataError(RuntimeError):
    """带稳定原因码的数据层错误。"""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class CredentialsRequiredError(DataError):
    """真实访问需要凭据，而当前环境没有配置凭据。"""

    def __init__(self, message: str = "数据源凭据未配置，真实导入被拒绝；离线夹具模式不受影响") -> None:
        super().__init__("credentials_required", message)


class TransportError(DataError):
    """网络传输失败（超时、连接中断等），可在预算内重试。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("transport_error", message)


class RateLimitedError(DataError):
    """供应商限频或类限频响应，按退避后重试。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("rate_limited", message)


class NoPermissionError(DataError):
    """结果性权限失败，重试无意义。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("no_permission", message)


class ContractError(DataError):
    """响应不符合字段契约（缺字段、类型不对、结构不完整），重试无意义。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("contract_error", message)


class PageIncompleteError(DataError):
    """返回行数少于响应声称的总数（缺页/截断），不允许把成功页伪装成完整成功。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("page_incomplete", message)


class UnknownApiError(DataError):
    """未知供应商错误文本；按未知处理，不猜测成权限或限频。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__("unknown_api_error", message)


RETRIABLE_REASONS = frozenset({"transport_error", "rate_limited"})
TERMINAL_REASONS = frozenset(
    {
        "no_permission",
        "contract_error",
        "page_incomplete",
        "unknown_api_error",
        "credentials_required",
    }
)
