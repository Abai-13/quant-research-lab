"""阶段三 S3：确定性组件注册表（SignalCalculator/SelectionPolicy/AllocationPolicy）。

三个组件接口各持一个独立注册表实例（隔离命名空间）：

- 登记名固定为 1..64 字符的小写字母/数字/下划线/连字符，首字符为字母或数字；
- 同名同实现的重复登记幂等（返回 reused=True）；同名不同实现直接拒绝，
  绝不覆盖既有实现——替换策略必须换名登记，避免静默偷换基线行为;
- ``names()`` 恒为升序，保证任何枚举输出确定性；
- 注册表本身不含时间戳/随机数，可被逐字段断言。
"""

from __future__ import annotations

import re
from typing import Any

from ..data.errors import DataError

__all__ = ["ComponentRegistry", "REGISTRY_NAME_PATTERN"]

REGISTRY_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ComponentRegistry:
    """单一组件类别的小型确定性注册表。"""

    def __init__(self, *, kind: str) -> None:
        if not isinstance(kind, str) or not kind.strip():
            raise DataError("registry_kind_invalid", f"注册表类别非法: {kind!r}")
        self.kind = kind
        self._items: dict[str, Any] = {}

    def register(self, name: str, implementation: Any) -> dict[str, Any]:
        """登记一个实现；同名同实现幂等，同名异实现拒绝。"""
        if not isinstance(name, str) or not REGISTRY_NAME_PATTERN.match(name):
            raise DataError(
                "registry_name_invalid",
                f"注册名 {name!r} 非法；须匹配 {REGISTRY_NAME_PATTERN.pattern}",
            )
        existing = self._items.get(name)
        if existing is not None:
            if existing is implementation:
                return {"name": name, "kind": self.kind, "reused": True}
            raise DataError(
                "registry_name_conflict",
                f"注册名 {name!r} 已绑定另一个 {self.kind} 实现；替换策略请换新登记名",
            )
        self._items[name] = implementation
        return {"name": name, "kind": self.kind, "reused": False}

    def get(self, name: str) -> Any | None:
        return self._items.get(name)

    def require(self, name: str) -> Any:
        implementation = self._items.get(name)
        if implementation is None:
            known = ", ".join(self.names())
            raise DataError(
                "registry_component_unknown",
                f"未登记的 {self.kind} 组件 {name!r}；已登记: {known or '(空)'}",
            )
        return implementation

    def names(self) -> list[str]:
        return sorted(self._items)
