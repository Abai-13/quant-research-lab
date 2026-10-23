"""ASGI 入口：供 uvicorn 以 ``app.asgi:app`` 加载。"""

from __future__ import annotations

from .config import ConfigError
from .main import create_app

try:
    app = create_app()
except ConfigError as exc:
    raise SystemExit(f"配置错误：{exc}") from exc
