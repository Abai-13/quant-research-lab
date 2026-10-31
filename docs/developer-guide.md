# 开发者指南：离线扩展

系统为若干计算职责提供明确的 Python Protocol 和按名称注册入口，不是任意代码上传/执行平台。建议在源码中实现并审查替代组件，再由受控应用代码选择其名称。不要把用户上传的源码自动导入或执行。

## 核心接口和注册

`backend/app/portfolio/signals.py` 导出 `SignalCalculator`，实现 `code`、`version` 和 `calculate(*, view, as_of, ts_code) -> SignalResult`。`view` 是绑定数据版本的只读视图；返回结果应保留证券、截止日、观察数、版本、明确排除原因和 Decimal 数值。

`selection.py` 的 `SelectionPolicy.select(signals) -> SelectionResult` 接收信号列表并返回确定性排序及逐项排除原因。`allocation.py` 的 `AllocationPolicy.allocate(AllocationRequest) -> AllocationResult` 只处理现金与已选择证券的目标权重；金额单位为整数分，约束来自版本化 bundle，比例上限使用整数 bp。它不是执行或调仓接口。

对应模块的 `*_REGISTRY` 是 `ComponentRegistry`。基线实现已登记；新增命名组件由应用集成代码显式调用 `registry.register(name, Implementation)`，调用 `registry.require(name)` 选择并实例化。名称不合法或同名但实现不同会被拒绝；同名同实现重复注册会幂等复用。注册表不做运行时 Protocol 校验，接口契约、可实例化性和边界应由维护者通过测试验证。不要从任意目录自动扫描、导入模块或联网下载组件。接口、组件版本、规则内容版本和数据版本应分别记录，避免把组件版本误作数据时点。

离线运行的最小可复算示例位于 `backend/examples/deterministic_sample.py`。它在 TEMP 中建立合成数据版本，调用生产基线信号计算器，以 Decimal 手算交叉核对；再显式注册一个命名扩展筛选器，调用真实筛选和分配类并断言对账。导入脚本时不会加载应用模块或改动注册表；执行时数据只写入临时目录，并向 stdout 输出结果。该脚本是信号与目标分配样本，不是可下单的历史模拟；“模拟工作台”的内置演示是单独的数据/规则版本，虽有手动用户订单成交回归场景，也不意味着高级受限合成数据或系统路径均有成交资格。

## 数据接口的边界

数据管理来源适配和历史模拟行情来源是两条不同的适配边界。数据管理使用 `backend/app/data/adapters/base.py` 定义的来源协议与 `registry.py` 注册表，由 `tushare.py` 和离线 `fixture.py` 提供当前实现。历史模拟数据准备通过 `backend/app/practice_data/sources.py`、`fetch.py` 和 `bridge.py` 的服务接口处理，最终绑定一个已准备版本。不要据此宣称项目已有统一的任意数据插件系统；来源注册、授权、质量检查和模拟资格仍须通过现有服务校验。

离线开发优先使用 `tushare-fixture` 或脚本内显式的 synthetic 数据。不要放入真实 Key、访问收费供应商或向用户正式数据库写入样本。若示例需要落盘，应显式接收输出路径或创建隔离 TEMP；不要在模块导入时下载数据、初始化数据库、注册用户组件或执行作业。

## 版本、单位和时间

- 金额 API/存储字段以 `_cents` 表示整数分；bp 是万分之一，100bp=1%；股数为整数。
- 行情价格精度由价格来源字段决定；信号用 `Decimal` 计算，不应在中间步骤擅自四舍五入。估值转为分时实现采用半入处理。
- 信号必须绑定 `purpose=signal_input`、固定数据版本、合格股票池与 `as_of`。信号只读取截至该日的数据；有效观察序列缺项不以前值补齐。
- 订单预览/提交、推进和公司行为使用其现有服务/API 与幂等约束。目标分配不代表订单；不可将用户组件输出直接写入账本。
- 输出 `null` 表示缺少足够证据或不可比较，不表示数值零。保留服务端的拒绝码和限制说明。

## 最小替换样例

示意新增筛选组件时，把下面代码放在受控源码模块中，由应用初始化代码显式调用 `register_ranker()`；不要在导入时执行该函数。实际生产实现还应覆盖 Protocol 字段、版本兼容、空输入、重复证券代码、确定性并列和错误处理。

```python
from app.portfolio.selection import (
    BaselineMomentumRankSelection,
    SELECTION_POLICY_REGISTRY,
)


class StableCodeRanker(BaselineMomentumRankSelection):
    code = "example_stable_code_ranker"
    version = "1.0.0"


def register_ranker() -> None:
    # 只在受控应用初始化时调用；不要在模块顶层调用。
    SELECTION_POLICY_REGISTRY.register(StableCodeRanker.code, StableCodeRanker)
```

替换组件后，应使用固定输入、固定数据/规则版本，对比基线和新组件的确定性输出；测试协议边界与错误语义，再通过既有服务入口检查完整输入绑定。不得绕过来源资格、账户 revision、订单预览、用户确认或研究/检验区间限制。

## 本地验证

从仓库根目录运行离线样本：

```powershell
py -3.12 .\backend\examples\deterministic_sample.py
```

## 历史练习的算法注册与运行

`GET /api/practice/algorithms` 从生产的 SignalCalculator、SelectionPolicy 与 AllocationPolicy 注册表枚举可执行组合，返回观察计划可直接冻结的 `algorithm_id`、组合 `version`、名称、说明、参数、限制和完整 `components` 配置。执行器用 `resolve_algorithm(algorithm_id, version, parameters)` 解析固定组合，或调用 `calculate_registered_algorithm(conn, context, account_state, algorithm_id=..., algorithm_version=..., parameters=...)`。底层 `calculate_practice_targets(conn, context, account_state, configuration)` 对每个组件名称、版本和参数作严格校验，不会静默切回基线。

该服务接收 portfolio 服务绑定的 `signal_input` 上下文，使用其固定 `VersionView`、revealed date、证券范围与规则组合，调用已注册组件并返回冻结组件、逐证券信号/排除理由、选择与分配结果。金额字段以整数分表示；股数以股表示。服务只读，不写账本、不推进练习、不创建成交。扩展算法须由受控源码显式登记，不支持动态上传或导入任意代码。

运行完整离线演示：

```powershell
py -3.12 .\backend\examples\registered_algorithm_sample.py
```

演示数据仅写入隔离 TEMP，只验证合成输入下的组件调用与复算，不代表真实收益或模型智能。

样例使用满足默认流动性门槛的合成成交额，断言真实服务选中 SYN-A、给出 360000 分目标，并与独立 Decimal 手算对账；不靠降低生产筛选门槛制造正向结果。

## 有限自动模拟

观察计划的创建与启动分别确认：`POST /api/practice/observe-plans` 只保存授权，`/{execution_id}/start` 才运行；暂停、撤销和恢复均使用授权 `expected_version` 与 `idempotency_key`，恢复另外要求 `consent=true`。下单与推进日期独立授权，最多20步。`max_position_shares` 限制模拟账户持仓总股数，`max_order_value_cents` 限制单笔成交本金（整数分）。没有推进权限时最多生成一次订单批次并结束，待人工推进。

每一步在同一 SQLite 事务内检查当前权限、数据/算法/规则/费用版本，调用手动流程共用的订单和推进服务，保存进度。暂停/撤销返回后旧执行令牌不能继续写；有外部修改或待执行的人工订单则停止。未知下一日价格造成超限时整步回滚。进程重启将运行计划置为暂停，用户核对后明确恢复；不会自动续跑或重复已提交步骤。运行器使用确定性注册算法，不调用模型。真实行情仍须通过来源和用途资格检查；明确合成演示的假设不适用于真实来源。

五年演示缓存使用 `phase3-demo-v2-5y`，原 `phase3-demo` 及旧版本引用保留。存储清理只覆盖未引用的准备数据版本；磁盘已分配文件的实际回收不承诺与估算字节相等。

运行扩展测试时使用项目指定的 Python 环境和现有测试命令。新示例不要调用网络，不读 `.env`，不修改共享依赖，不碰正式库。若确需增加依赖或需要外部数据/服务，应通过项目维护流程先明确依赖及验证范围。
