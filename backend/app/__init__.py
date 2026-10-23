"""本地后端。

阶段 1 提供配置、SQLite 元信息与状态接口；阶段 2 在此基础上增加数据接入、
质量检查、历史股票池与数据状态接口。仍不包含策略、回测、账户或调度实现。
"""

__version__ = "0.2.0"

SERVICE_NAME = "ai-quant-simulator"
DISPLAY_NAME = "AI 量化模拟交易系统"
MODE = "local"
