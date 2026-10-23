"""适配器工厂：按配置选择真实来源或离线夹具。"""

from __future__ import annotations

from ...config import Settings
from .base import DataAdapter
from .fixture import FixtureAdapter
from .tushare import TushareAdapter

__all__ = ["build_adapter"]


def build_adapter(settings: Settings, source_code: str | None = None) -> DataAdapter:
    """构造适配器。

    真实来源缺少凭据时不立刻失败：适配器在真正取数时返回 ``credentials_required``，
    这样 ``source-check`` 仍可离线执行并如实报告“未配置凭据”。
    """
    code = source_code or settings.data_source.source_code
    if code == "tushare":
        return TushareAdapter(
            api_base_url=settings.data_source.api_base_url,
            token=settings.data_source.token,
            timeout_seconds=settings.import_retry.request_timeout_seconds,
            min_interval_seconds=settings.import_retry.min_interval_seconds,
        )
    if code == "tushare-fixture":
        return FixtureAdapter(settings.fixture_dir)
    raise ValueError(f"不支持的数据源: {code}")
