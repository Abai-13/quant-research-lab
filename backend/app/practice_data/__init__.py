"""阶段四练习数据域：免费行情源核验、版本共享与显式更新。

边界：`
- 本模块只提供练习隔离库（``runtime/practice/app.db``）中共享的数据版本；
  不修改全局 schema、不触碰 `.runtime/data/app.db` 等真实运行库。
- 只在显式 ``POST /data/refresh`` 时访问网络；GET 与应用启动只读本地状态。
- 免费客户端许可不等于行情数据许可：来源声明按官方文档如实标注受限。
"""

from .api import (
    create_practice_data_router,
    reset_practice_connect_provider,
    set_practice_connect_provider,
)

__all__ = [
    "create_practice_data_router",
    "set_practice_connect_provider",
    "reset_practice_connect_provider",
]
