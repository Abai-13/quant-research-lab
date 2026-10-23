"""阶段三连接边界：保证写入新表前启用 SQLite 外键约束。

SQLite 的外键按连接生效（``PRAGMA foreign_keys`` 默认关闭）。阶段三新增的
账户/规则/分析/模拟表带 FOREIGN KEY 引用其父记录；所有阶段三写入都必须经由
这里打开（或显式启用约束的）连接，否则孤儿引用不会被数据库拒绝——这是既定
边界，不做静默兜底。schema 4 的跨域触发器（模拟账户必须指向实际账户、多态
owner 存在性等）不依赖该 pragma，在任何连接上都生效。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["portfolio_connect", "enable_portfolio_constraints"]


def enable_portfolio_constraints(conn: sqlite3.Connection) -> sqlite3.Connection:
    """在连接上启用外键约束；必须在事务开始前调用（事务内执行是空操作）。

    本函数只设置 pragma，不提交、不关闭调用方已有事务；调用方负责自己的事务
    边界。连接若已处于事务中，会抛出 RuntimeError 以避免"看似启用实则未生效"。
    """
    if conn.in_transaction:
        raise RuntimeError(
            "enable_portfolio_constraints 必须在事务开始前调用；"
            "当前连接已在事务中，外键 pragma 可能不会生效"
        )
    conn.execute("PRAGMA foreign_keys = ON")
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if int(enabled) != 1:
        raise RuntimeError("PRAGMA foreign_keys=ON 未能生效（意外返回 0）")
    return conn


def portfolio_connect(db_path: str | Path) -> sqlite3.Connection:
    """打开一个阶段三写入连接：Row 工厂 + 外键约束启用。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return enable_portfolio_constraints(conn)
