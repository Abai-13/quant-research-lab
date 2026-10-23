# AI 量化模拟交易系统

面向个人本地使用的股票数据研究与模拟交易工具。可以记录自己的持仓与现金，生成基于原持仓的渐进调整建议或同资金独立目标，并从账户快照创建独立模拟段，查看成交、费用、持仓和净资产变化。项目仓库：[Abai-13/quant-research-lab](https://github.com/Abai-13/quant-research-lab)。

后端使用 **FastAPI + SQLite**，前端使用 **React + TypeScript + Vite**。页面读取真实接口状态，明确区分服务存活、数据库就绪、数据准备程度和未实现的功能；没有数据时显示明确空状态，不展示虚构的净值、收益、持仓或行情。

## 当前功能

| 功能 | 内容 |
|---|---|
| 本地运行 | Windows 安装、启停脚本、服务与数据库状态页 |
| 数据管理 | 历史/增量导入、质量检查、固定数据版本、历史股票池、备份恢复和手动清理 |
| 持仓账本 | 期初持仓、现金、买卖、出入金、更正与历史快照 |
| 组合建议 | 原持仓渐进调整、同资金独立目标、交易成本和不可执行原因 |
| 模拟交易 | 独立模拟段、订单与部分成交、费用、可卖数量和逐交易日推进 |
| 公司行为与绩效 | 支持范围内的现金分红/送转、净资产和阶段收益记录 |
| 离线体验 | 无需数据密钥即可运行独立的合成数据演示 |

个人账本、分析建议和模拟成交分别保存。生成建议或推进模拟都不会改写用户录入的实际账户。当前网页提供最小操作界面，更完整的账本和查询操作通过下文 API 使用。

## 数据管理

- **可追溯的数据接入**：Tushare 官方通用接口适配器（真实访问）+ 离线夹具适配器（仅测试与演示）；原始响应按内容摘要存档，元信息与规范化结果入库。
- **批量导入与增量更新**：分页取数、限频、有界重试、分批检查点与中断恢复；增量边界按业务范围与成功检查点计算，并为修订留出可配置的重取窗口。
- **幂等与修订版本**：逻辑键 = 数据源 + 类别 + 证券/交易所 + 业务日期；内容一致不新增记录，内容变化追加新修订，旧修订与旧数据版本原样保留。
- **共享不可变分块版本**：版本不再逐版复制全部成员，而是引用内容寻址的不可变分块；未变化的分块被多个版本共享，版本 ID、摘要与旧版本读取保持稳定。
- **数据库安全迁移**：支持旧 schema 1/2/3 显式升级到 schema 4；先做含外部文件的备份并独立验证，再升级。损坏库、未知 schema、外来表一律保留现场，不删库重建。
- **固定版本读取**：价格、复权因子、日历、停牌、历史长度、最新行情日期与资格判断均可绑定某个数据版本；后来新增或修订的数据不会改变旧版本的结果。
- **五年工作窗口与有限清理**：增量发布默认生成滚动 `working` 版本（最近 5 个日历年，含 20 交易日预热与每证券锚点）；过期未引用版本可预览后归档回收，默认只预览、不自动删除。
- **质量检查与数据可用资格**：17 条版本化质量规则；统一入口 `check_eligibility(scope, as_of, data_version, purpose)` 区分展示、信号输入、成交参考、估值参考与严格历史重放。
- **按历史时点的股票池快照**：固定 `as_of + data_version + rule_version + 规则参数`，保留成员、排除原因与限制标记；退市、停牌、上市历史不足等限制显式传播。
- **最小可见效果**：状态页新增“数据状态”区块，展示来源、覆盖区间、最近成功导入、问题数量、数据版本与股票池快照数量。

## 阶段三新增功能：持仓、组合建议与模拟

- **个人持仓与实际账本**：录入截至日、现金（整数分）、证券数量、可选总成本/可卖量/取得日和锁定偏好；未知值保持 `null`。实际事件追加写入，支持期初、买卖、入金/出金、已确认公司行为和更正；预览不写库，提交带账户修订乐观并发与幂等键。旧快照保留可查。
- **组合建议**：默认渐进调整（从当前持仓向理想目标过渡）与可选同资金独立推荐共用同一账户修订、截至日、数据/股票池/规则版本、估值与预算。未入选目标的持仓受版本化保留缓冲保护，不自动清仓；锁定/不可卖/无估值/版本外持仓显式可见。建议有默认 24 小时有效期（版本化上限），同输入幂等复用，输入变化生成新的不可变 run；读取只反序列化工件，不重算。
- **可替换业务核心**：`SignalCalculator`、`SelectionPolicy`、`AllocationPolicy` 等 Protocol 与小型注册表；基线公式为 `momentum = P(t-5)/P(t-65)-1`、`trend = mean(P(t-119)..P(t))`，要求 120 个合格观察、无前视/填充，按动量降序与证券代码升序稳定排序。
- **模拟核心**：从已持久化账户快照创建独立模拟段；模拟期初/成交/现金/持仓/事件只写模拟域表，不写实际 `account_event`/`account_snapshot`/`account_position`。订单状态覆盖 pending/filled/partially_filled/unfilled/rejected/expired/cancelled；开盘参考价 + 版本化不利滑点；费用/滑点分项；卖出先于买入；停牌/涨跌停/规则/T+1 证据缺失时 fail-closed；T+1 批次按下一交易日解锁可卖股数；部分成交在后续交易日续做，同日重复推进不重复尝试；关闭时段内未完成与部分成交剩余量过期。
- **公司行为与绩效**：受支持现金分红/送转股按权益登记、除权应收、派息/上市到账处理；税后净额未确认保持 pending，不能冒充已确认现金。NAV = 现金 + 持仓市值 + 应收 - 应付；缺估值返回 `partial_unknown`，未知不当 0；收益按 `NAV_end/(NAV_previous + 外部现金流) - 1` 计算，缺上一期/分母非正/估值不完整时返回受限而非填 0。
- **最小 API/UI**：`/api/portfolio/*` 提供账户/事件预览与提交、建议生成与读取、模拟创建/下单/推进/查询、公司行为、绩效与默认规则组合；状态页新增“我的持仓、组合建议与模拟”区块。所有写路径复用同一服务核心。
- **隔离离线演示**：`/api/portfolio/demo/bootstrap` 在 `<运行目录>/phase3-demo` 创建合成数据库、合成行情/股票池/账户，页面显式标注“离线演示”；演示数据与个人主库分离，不读取真实行情。

## 使用范围与限制

- **个人本地研究与模拟**：不接券商、不实际下单，不承诺算法胜过用户或市场。模拟收益依赖数据、费用与成交假设，不代表实际投资收益。
- **真实数据源**：当前适配 Tushare；账号权限、覆盖、限频和费用取决于供应商与个人账号，未获得真实导入和目标规模的完整运行验证。免费来源适配尚未提供。
- **股票池仍受幸存者偏差限制**：当前只有“在市名单 + 退市名单”，缺完整历史上市/退市全集与完整历史停牌状态；快照会带 `historical_universe_incomplete`、`survivorship_bias_unresolved` 等限制标记。
- **严格历史重放资格受限**：供应商不提供逐条历史发布时刻，今天下载的历史数据只证明今天能取得，不能证明历史时点已知。
- **公司行为与费用**：支持部分现金分红/送转流程；复杂配股、合并和退市处置未覆盖。默认演示费率与交易规则均为合成假设，不能冒充真实券商费率。
- **没有行情不是 0 行情**：缺失价格保持缺失，异常值只标记不自动改写，陈旧数据只带标签。
- **更新与清理需手动触发**：打开软件不会自动下载行情；数据导入、增量更新与清理使用下文命令。五年工作窗口不承诺总磁盘固定，归档、备份与日志同样占用空间。
- **规模数据为合成结果**：规模与增长测量使用固定公式生成的合成证券，不代表真实市场覆盖、真实限频或真实历史规模。
- **当前未提供**：完整历史回测与逐日练习界面、三路自动对照、一键重置、自动调参、大模型 API 设置和 Agent 工作流。网页登录与公网部署也未配置。

## 本地部署速览

当前支持 Windows 10/11 本地运行。需要 Python 3.12、Node.js 20.19 或更新版本及 npm；**不需要行情或大模型密钥即可启动**（数据层在未配置凭据时保持“未配置/未接入”状态）。

```powershell
git clone https://github.com/Abai-13/quant-research-lab.git
cd quant-research-lab
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-local.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1
```

启动后访问 <http://127.0.0.1:5173>。后端接口位于 `http://127.0.0.1:8000/api`。停止服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-local.ps1
```

服务仅绑定本机回环地址。未配置登录、HTTPS 或公网访问，因此这些步骤是**本地部署/运行方式，不是公网生产部署方案**。端口、运行目录与数据库路径等可通过项目根目录的 `.env` 配置，详见下文。

## 环境要求

| 项目 | 要求 | 本次验证版本 |
|---|---|---|
| 操作系统 | Windows 10/11 | Windows 11 |
| PowerShell | Windows PowerShell 5.1 或 PowerShell 7 | Windows PowerShell 5.1 |
| Python | 3.12.x | 3.12.10 |
| Node.js | >= 20.19 | 24.16.0 |
| npm | 随 Node.js 安装 | 11.13.0 |

## 安装（一次性）

在项目根目录执行。脚本会使用项目内的 `backend\.venv` 和 `frontend\node_modules`，不会全局安装 Python 包或修改全局工具：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-local.ps1
```

- 后端：按 `backend\requirements.lock` 安装固定版本。
- 前端：按 `frontend\package-lock.json` 执行 `npm ci`。
- 安装失败时脚本以非零退出码结束，不会继续启动服务。

## 启动与停止

安装完成后，可从任意目录调用（脚本以自身位置定位项目根目录，支持中文和空格路径）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\path\to\quant-research-lab\scripts\start-local.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\path\to\quant-research-lab\scripts\stop-local.ps1"
```

默认地址：

- 页面：<http://127.0.0.1:5173>
- 后端健康检查：<http://127.0.0.1:8000/api/health>

启动脚本的行为：

- 后端与前端都只监听 `127.0.0.1`。
- 启动前检查端口；被占用时明确失败并提示修改配置，**不会终止其他进程，也不会静默换端口**。
- 前后端端口不能相同，相同配置在启动任何服务前直接失败。
- 前端启动后要求进程仍存活，且页面根路径返回包含应用根元素的 HTML，避免把后端端口误认为前端就绪。
- 在 `.runtime\state\processes.json` 记录自己启动的进程（PID、创建时间、可执行文件/脚本路径）。
- 重复启动时复用已核验的进程，不重复开服务。
- 启动失败或健康检查超时时，只清理本次启动且能核验身份的进程。

停止脚本的行为：

- 只处理 `processes.json` 中记录且仍能通过「PID + 创建时间 + 命令路径」核验的进程。
- PID 已被系统复用时视为过期记录并跳过，不会误杀其他进程。
- 重复停止是安全的；没有记录时只给出提示。
- 不关闭其他不属于本项目的进程。

## 离线演示

状态页的“我的持仓、组合建议与模拟”区块提供“启动离线演示”。它会调用
`GET /api/portfolio/demo/bootstrap`，在 `<运行目录>/phase3-demo` 创建独立的合成数据库
（合成行情、股票池、演示账户与模拟规则），页面始终显示“离线演示：合成数据与独立数据库”。
演示账户不会出现在个人主库；不会读取真实行情、不会写入用户账户。也可直接访问：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/portfolio/demo/bootstrap
```

演示中的建议/成交/费用均为合成假设，不代表任何真实券商费率、市场规则或投资收益。

体验顺序：

1. 点击“启动离线演示”，页面自动填入演示数据版本、股票池、日期和规则标识。
2. 在组合建议区选择渐进调整或独立推荐，点击生成并查看逐股结论。
3. 在模拟区点击创建模拟段，再按页面提供的日期登记示例买单并推进，查看模拟现金和持仓变化。

演示日期是固定样例，不是最新市场日期。当前模拟区只提供示例买单入口；其他方向订单、取消、关闭、事件查询和绩效操作使用 API。退出页面不会自动清空数据库，当前没有一键重置功能。

## 配置

复制示例文件后修改，前后端都会读取项目根目录的 `.env`：

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
```

| 配置 | 默认值 | 说明 |
|---|---|---|
| `APP_BACKEND_HOST` | `127.0.0.1` | 后端监听地址；本阶段无登录保护，保持回环地址 |
| `APP_BACKEND_PORT` | `8000` | 后端端口；前端 `/api` 代理自动跟随 |
| `APP_FRONTEND_PORT` | `5173` | 前端端口；不能与后端端口相同 |
| `APP_RUNTIME_DIR` | `<项目根>/.runtime` | 运行目录（数据库、日志、进程记录） |
| `APP_DB_PATH` | `<运行目录>/data/app.db` | SQLite 文件路径 |
| `APP_LOG_DIR` | `<运行目录>/logs` | 日志目录 |
| `APP_BACKUP_DIR` | `<运行目录>/backups` | 迁移前一致性备份目录 |
| `APP_DATA_RAW_DIR` | `<运行目录>/data/raw` | 原始响应档案目录 |
| `APP_LOCK_DIR` | `<运行目录>/locks` | 数据写入串行锁目录 |
| `APP_DATA_SOURCE` | `tushare` | 数据来源：`tushare` 或 `tushare-fixture` |
| `APP_TUSHARE_API_URL` | `https://api.tushare.pro` | Tushare 接口地址 |
| `APP_TUSHARE_TOKEN` | 空 | 数据源令牌；只写在自己的 `.env`，不提交版本库 |
| `APP_DATA_FIXTURE_DIR` | `backend/tests/fixtures/tushare` | 离线夹具目录（仅夹具模式） |
| `APP_IMPORT_MAX_ATTEMPTS` | `3` | 单页总尝试次数上限 |
| `APP_IMPORT_BACKOFF_SECONDS` | `1,3` | 重试退避秒数序列 |
| `APP_IMPORT_PAGE_WAIT_BUDGET` | `60` | 单页等待预算（秒） |
| `APP_IMPORT_BATCH_WAIT_BUDGET` | `600` | 整批等待预算（秒） |
| `APP_IMPORT_MIN_INTERVAL_SECONDS` | `0.35` | 两次请求最小间隔（限频） |
| `APP_IMPORT_REVISION_WINDOW_DAYS` | `5` | 增量时回取的天数，用于捕获修订 |
| `APP_IMPORT_LOCK_WAIT_SECONDS` | `30` | 写入锁等待上限（秒） |
| `APP_WORKING_WINDOW_YEARS` | `5` | 滚动工作窗口年数；增量版本只包含窗口内记录与锚点 |
| `APP_WARMUP_TRADING_DAYS` | `20` | 窗口起点前的预热交易日数 |

非法端口、空主机名、未知数据源等配置会让启动或命令明确失败，并给出原因。

`.runtime` 已被 `.gitignore` 忽略；不同 worktree 各自使用自己的 `.runtime`，不会共用数据库或进程记录。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 进程存活；正常返回 200，不表示数据库或业务功能就绪 |
| `GET /api/ready` | 数据库只读检查；就绪返回 200，否则 503 |
| `GET /api/system/status` | 功能准备状态；未实现项目返回明确状态与 `null` 数值 |
| `GET /api/data/status` | 数据状态摘要：来源、覆盖区间、逻辑记录/修订计数、最近导入、质量问题、有界数据版本与股票池快照列表 |
| `GET /api/data/versions?limit&offset` | 分页读取数据版本；`limit` 上限 200 |
| `GET /api/data/cleanup/previews?limit` | 只读列出维护库中的清理预览；维护库不存在时返回空列表 |
| `POST /api/portfolio/accounts` | 创建本地个人实际账户 |
| `GET /api/portfolio/accounts/{id}` | 当前账户状态（现金/持仓/未知字段） |
| `POST /api/portfolio/accounts/{id}/events/preview` | 只读预览实际事件（不写库） |
| `POST /api/portfolio/accounts/{id}/events` | 原子提交实际事件（带账户修订与幂等键） |
| `GET /api/portfolio/accounts/{id}/events?limit&offset`、`/snapshots?limit` | 有界事件/快照审计读取；事件列表返回总数和分页信息 |
| `POST /api/portfolio/recommendation/runs` | 生成渐进调整或同资金独立建议 |
| `GET /api/portfolio/recommendation/runs[/{run_id}]` | 有界列表/只读结果；过期与输入失效显式标注 |
| `POST /api/portfolio/simulation/segments` | 从已持久化账户快照创建模拟段 |
| `POST /api/portfolio/simulation/segments/{id}/orders`、`/advance`、`/close` | 模拟订单、推进与关闭 |
| `GET /api/portfolio/simulation/segments[/{id}[/state]]` | 有界模拟段列表、详情与状态读取 |
| `POST /api/portfolio/corporate-actions`、`/{id}/settle` | 登记与结算受支持公司行为 |
| `POST /api/portfolio/performance/points`、`GET /performance/summary` | 估值/收益点与摘要 |
| `GET /api/portfolio/demo/bootstrap` | 创建/复用隔离离线演示库（`synthetic_demo=true`） |

组合写接口要求在请求体显式传入 `demo=true` 才操作演示库，读取接口使用 `?demo=true`；缺省操作个人主库。演示数据始终标注合成，不写入个人账户。

示例（实际响应）：

```json
{"service":"ai-quant-simulator","name":"AI 量化模拟交易系统","version":"0.2.0","mode":"local","status":"ok"}
```

```json
{"status":"ready","database":{"status":"ready","schema_version":4,"instance_id":"520829cb-7a7c-45cc-a08f-ec7d136537ed","created_at":"2026-10-21T04:44:21+00:00"}}
```

```json
{
  "status": "not_connected",
  "detail": "尚未导入任何行情数据",
  "source": {"source_code": "tushare", "api_base_url": "https://api.tushare.pro", "credentials_present": false, "is_fixture": false},
  "coverage": {},
  "quality": {"total": 0, "by_severity": {"info": 0, "warn": 0, "high": 0}},
  "last_import": null,
  "data_versions": [],
  "pool_snapshots": [],
  "limitations": ["..."]
}
```

数据库未就绪时，`/api/ready` 返回 503，`database.reason` 为以下之一：`database_missing`、`database_corrupt`、`schema_incompatible`、`database_unavailable`、`migration_required`。检查过程只读，不会创建、删除或重建数据库。

## 数据库与迁移

当前数据库结构为 **schema 4**，在同一个 SQLite 文件中保存市场数据、账户、规则、建议、模拟、公司行为和估值收益。新库直接初始化为 schema 4；旧 schema 1/2/3 需显式迁移。主要逻辑实体：

| 逻辑实体 | 表 | 内容 |
|---|---|---|
| 来源/接口能力 | `source_registry` | 字段映射、单位、权限状态、覆盖、限频、费用与证据 |
| 导入批次与请求 | `import_batch`、`import_request_log` | 范围、状态、分页、尝试次数、错误类别、检查点 |
| 原始数据档案 | `raw_archive` | 不可变响应文件引用与内容摘要（不含凭据） |
| 规范化记录修订 | `trade_calendar`、`security_info`、`daily_price`、`adj_factor`、`suspension_event`、`corporate_action` | 各自带来源、时间口径、修订号与内容摘要 |
| 数据版本 | `data_version`、`data_version_member` | 版本元信息（含 `manifest_version`、`member_storage`、窗口字段）与旧式成员行（兼容读取） |
| 共享分块 | `data_chunk`、`data_chunk_member`、`data_version_chunk` | 不可变内容分块、块成员与版本对分块的引用；未变化分块跨版本复用 |
| 覆盖统计 | `coverage_stats` | 逻辑记录数与物理修订行数分开，带陈旧标记 |
| 质量问题 | `quality_issue` | 规则、严重度、受影响证券/日期/字段与证据 |
| 股票池快照 | `pool_snapshot`、`pool_member`、`pool_exclusion` | 截止时点、规则版本、数据版本、成员与排除原因 |
| 维护与保留 | `maintenance_generation`、`retention_policy`、`retention_hold`、`archive_manifest`、`cleanup_journal`、`schema_history` | 状态证人、保留政策、引用保护、归档清单、清理日志与迁移历史 |
| 阶段三账户/规则 | `account`、`account_event`、`account_snapshot`、`account_position`、`rule_bundle`、`account_constraint` | 实际/模拟账户域、追加事件、不可变修订快照、版本化规则/费用组合 |
| 阶段三建议 | `analysis_run`、`analysis_item`、`analysis_artifact`、`portfolio_target`、`target_item` | 输入/输出工件、逐股结论、理想/过渡目标 |
| 阶段三模拟 | `simulation_segment`、`simulation_order`、`simulation_fill`、`simulation_event`、`simulation_account_state`、`simulation_position_state`、`simulation_lot_state` | 模拟域独立事件/现金/持仓/可卖批次与订单成交 |
| 阶段三公司行为/绩效 | `corporate_action_application`、`valuation_snapshot`、`performance_point` | 应收/结算、估值快照与收益点 |

维护进程的预览/动作记录放在独立的 `<运行目录>/maintenance.db`，不写入业务库；前缀 `cleanup preview` 生成的预览只写该侧库。

迁移规则：

- 迁移由显式命令触发，**不会在服务启动时自动改写旧库**；readiness 与状态查询只读。
- 支持 `schema 1 → 2 → 3 → 4`、`schema 2 → 3 → 4` 与 `schema 3 → 4`；新库直接初始化为 schema 4。阶段三 3→4 为纯增量迁移，不改写既有行。未知 schema、损坏库、含外来表的库保留现场并拒绝。
- 迁移前创建备份包（业务库 + raw 原始档案 + 归档文件 + 维护侧库快照 + `backup-manifest.json`），并在临时目录独立恢复验证；核对失败即中止。
- 结构变更、元信息与迁移历史尽量在同一业务事务内提交；提交后中断按实际 schema 与分块成员核对恢复，不伪造历史记录。
- 旧 `dv_...` 版本 ID、`manifest_version = 1` 与旧摘要永久保留；迁移只把成员读取切换到分块存储，`--reclaim-legacy-members` 才在核对通过后删除重复的旧成员行，且删除前后旧版本仍可读。
- 安全回退依靠已验证的备份包与归档，不执行盲目降级；恢复只写入明确指定且为空的目标路径，不自动覆盖运行库。

## 数据接入操作

所有数据操作都是**手动命令**，服务启动时不会自动下载任何数据，测试也不会触发真实网络请求。以下命令在项目根目录执行（Windows PowerShell），先为当前终端设置模块搜索路径：

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'backend')
```

### 0. 先看当前状态

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli status
```

### 1. 核对来源与凭据

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli source-check
backend\.venv\Scripts\python.exe -m app.data.cli source-check --probe   # 额外发一次最小请求
```

`source-check` 输出每个接口的字段映射、单位、权限状态、限频、费用、文档链接与**未知项**。未配置凭据时 `credential_check` 为 `missing`，`--probe` 会返回 `credentials_required`，不会伪造成功。

### 2. 显式迁移数据库（schema 1/2/3 → 4）

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli migrate
backend\.venv\Scripts\python.exe -m app.data.cli backup --out .runtime\backups   # 单独备份
```

新建的空库在启动或首次导入时初始化为 schema 4；已有 schema 1、schema 2 或 schema 3 库必须先执行 `migrate`。升级到 schema 4 不会重置账户或行情数据。旧成员转换为分块存储时，`--skip-convert` 可暂缓转换，`--reclaim-legacy-members` 才会在核对通过后回收重复的旧成员行。迁移失败或中断时，依据维护库中的运行状态与已验证备份核对后再处理，不删除原库重来。

备份包可以独立恢复验证：

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli restore --from .runtime\backups\<备份包> --to .runtime\restore-check
```

`restore` 只恢复到新目标，不覆盖运行库；归档版本可用 `restore --version-id <版本 ID> --target-db <新库>` 恢复到空库。

### 3. 导入历史数据

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli import-history `
  --start 20240102 --end 20240131 `
  --codes "000001.SZ,600000.SH" `
  --categories calendar,security,daily,adj_factor,suspension,corporate_action
```

- `--categories` 可选值：`calendar`、`security`、`daily`、`adj_factor`、`suspension`、`corporate_action`；省略表示全部。
- `--codes` 是**先固定的小样本**，用于验证流程与核对行数；它不代表完整历史证券全集。
- 日线默认按证券逐只取；加 `--daily-by-trade-date` 改为按交易日取全市场（需要先用 `--trade-date` 指定交易日）。
- 默认整批失败不发布新版本；要发布缩小范围时显式加 `--allow-partial`，版本会写明失败范围与质量问题。
- 重复导入同一范围不会产生重复记录或重复修订（只会新增请求日志）。

### 4. 增量更新

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli import-incremental `
  --categories daily,adj_factor --codes "000001.SZ,600000.SH"
```

增量起点按**已入库的业务范围与成功检查点**计算（不是进程启动时间），并向前回取 `APP_IMPORT_REVISION_WINDOW_DAYS` 天以捕获修订。中断后可用 `--resume-batch <批次 ID>` 从检查点继续。

### 5. 质量检查

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli quality-check --as-of 20240131
backend\.venv\Scripts\python.exe -m app.data.cli quality-check --as-of 20240131 --record
```

规则版本固定为 `qr-2026-10-22.1`，覆盖交易日缺口、分页覆盖缺口、重复键冲突、非有限价格、错误 OHLC、负量额、单位不一致、日期乱序、未知证券、上市前/退市后记录、停牌与行情冲突、公司行为缺口、陈旧数据、未来时点记录与历史可用时刻未证明。`--record` 会把问题写入 `quality_issue` 表。

检查按 `(ts_code, trade_date)` 有序流式读取，`--batch-size`（默认 5000）只控制每次读取的行数，不改变跨批的缺口、重复键与统计语义；`--data-version <版本 ID>` 只检查该版本包含的成员，而不是当前最新修订。

### 6. 查看数据版本

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli versions --limit 20 --offset 0 --summary
```

数据版本清单不可变：相同内容重复发布复用同一版本号，旧版本的成员集合与时间戳不会被改写。`--summary` 省略较大的 scope/质量字段；版本列表默认有界，完整历史用 `--offset` 分页。

### 7. 生成历史股票池快照

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli pool-snapshot `
  --as-of 20240131 --min-history-days 120 --liquidity-window-days 20 --min-avg-amount 50000000
```

- 快照固定 `as_of + data_version + rule_version + 参数`，同样的输入必然得到同样的成员与排除记录；新增未来行情不会改变已生成的快照。
- `--min-history-days`、`--liquidity-window-days`、`--min-avg-amount` 是示例默认参数，**不是已批准的生产参数**；阈值应预先固定，不能按结果临时调优。
- 输出含成员、排除原因与限制标记（如 `historical_universe_incomplete`、`survivorship_bias_unresolved`）。

### 8. 判断数据是否够用

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli eligibility `
  --purpose signal_input --as-of 20240131 --codes "000001.SZ,600000.SH"
```

`--purpose` 可选值与含义：

| 用途 | 判定要点 |
|---|---|
| `display` | 允许带陈旧或缺失标签展示 |
| `valuation_reference` | 允许使用陈旧价格，但必须带陈旧标签 |
| `signal_input` | 要求截止时点有行情、历史长度足够、且没有高严重度质量问题 |
| `execution_reference` | 要求指定交易日有价格、当日是交易日且未全日停牌 |
| `strict_historical_replay` | 在信号输入条件之上还需要历史发布时刻证据；当前来源不提供，因此受限 |

输出为 JSON，包含 `eligible`、`reason_code`、`reasons`、`affected_scope`、`latest_data_date`、`quality` 与 `source_limits`。

### 9. 查看统计与覆盖

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli status
backend\.venv\Scripts\python.exe -m app.data.cli stats
backend\.venv\Scripts\python.exe -m app.data.cli coverage --refresh
```

`status` 返回轻量摘要：逻辑记录数与物理修订行数分开计数，版本与快照列表有界（默认 20 个版本、10 个快照），并给出各自总数。`coverage --refresh` 显式重算覆盖统计；普通状态查询只读摘要，不触发全库扫描或网络取数，摘要缺失/陈旧会明确标记。

### 10. 清理预览与应用（默认不删除）

```powershell
# 预览：只读业务库，写维护库，不改任何行情
backend\.venv\Scripts\python.exe -m app.data.cli cleanup preview --source tushare --as-of 20241231

# 查看预览与动作状态
backend\.venv\Scripts\python.exe -m app.data.cli cleanup status
backend\.venv\Scripts\python.exe -m app.data.cli cleanup status --preview-id <预览 ID>

# 应用（默认先创建并验证操作前备份；预览过期或数据库已变化会被拒绝）
backend\.venv\Scripts\python.exe -m app.data.cli cleanup apply --preview-id <预览 ID>
```

预览列出拟过期版本、拟归档分块、拟回收记录、受保护引用与估计空间，不接受隐含删除。同时按有限保留政策列出：未引用原始响应档案（30 天）、请求明细日志（90 天）、未引用质量问题（400 天，先汇总再删除）与普通备份包（保留最近 3 个 verified 恢复点，含本次新建恢复点）。每类对象给出数量、可回收空间、保护原因与截断状态；未知目录、越界路径与其他实例的文件永不删除。

应用前会核对 `instance_id`、数据库路径、政策版本、`generation` 与状态摘要；任何后续写入都会使旧预览失效。默认先创建并独立验证操作前备份包，再开始删除；显式替代恢复点（`--no-recovery-point --recovery-point`）还必须通过实例、规范化数据库路径与**完整内容指纹**核对，过旧、克隆分叉、被篡改或不完整的包都会被拒绝。恢复点会在任何删除/提交之前精确登记到维护库；若业务事务已提交但维护库尚未收尾，同一预览重试会按业务 journal 核对事实并完成收尾，且**在重试删除任何文件之前**重新完整验证登记包的数据库、全部外部文件与内容指纹。登记缺失、包内 DB 或外部文件损坏、manifest 被替换或内容证明不符时明确拒绝，保留待删文件、全部旧恢复点保护与其他备份，预览保持未完成；修复原包后可安全重试。不会按时间或 generation 猜测恢复点，也不会用清理后的新备份掩盖旧状态不可恢复。

存在未完成清理（仍有待删文件，或恢复点处于提交前登记但未完成收尾的 `recorded` 状态）时，**新的清理预览会被明确阻止**（`pending_cleanup_unresolved`），不会接管旧待办、释放旧保护或删除旧恢复点备份。请先对原预览完成中断收尾或人工恢复其精确登记的恢复点后重试；原预览的中断收尾只处理它自己的待删文件，其他未完成任务的待办与保护保持不变。完成后新清理即可正常运行。

已 `applied` 但仍有待删文件失败的原预览，可在完整复验其恢复点后重试自己的文件收尾（`finalized_pending_files`），不受预览 30 分钟有效期限制；已提交的 journal 收尾同样不受 TTL 阻断，未实际执行的过期预览仍会被拒绝。仍有待办的其他任务即使恢复点为 `active`，也不会被新清理全局释放或当作普通备份回收。

默认只保留最近 5 个日历年工作窗口、最近 8 周普通版本、每来源最后一个可用版本、被 hold/股票池快照引用的版本，以及每证券窗口前锚点。归档先写临时文件、fsync、完整读回校验，再原子改名并在同一事务内回收在线副本；仍被引用的数据不会被删除。外部原始文件先登记到维护库待删清单，业务行删除提交后才删除文件；中断或删除失败的文件保留待办状态，由原预览/原清理任务重试，不凭位置或时间扫描删除未知文件。

### 11. 引用保护与保留政策

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli hold add --target-type version --target-id <版本 ID> --reason "研究引用"
backend\.venv\Scripts\python.exe -m app.data.cli hold add --target-type snapshot --target-id <快照 ID> --reason "历史结论"
backend\.venv\Scripts\python.exe -m app.data.cli hold list
backend\.venv\Scripts\python.exe -m app.data.cli hold remove --hold-id <hold ID>
backend\.venv\Scripts\python.exe -m app.data.cli retention show
```

支持的 hold 类型：`version`、`snapshot`、`record_range`（`类别|起|止`）、`chunk`、`raw_archive`（原始响应摘要）。未知类型直接拒绝，不会被静默忽略。

`cleanup status [--preview-id]` 还会显示恢复点登记状态（`recorded`/`active`/`released`/`aborted`）、待删外部文件与质量汇总，便于核对提交后中断的收尾进度。

### 12. 校验归档

```powershell
backend\.venv\Scripts\python.exe -m app.data.cli verify-archive --archive-id <归档 ID>
```

校验归档文件摘要、manifest、内容摘要与记录数；归档缺失或损坏时拒绝把该版本报告为可回收。

### 离线夹具演示（不触网、不冒充真实数据）

`backend/tests/fixtures/` 中的数据是**合成的**，只用于测试与演示，不能替代真实历史导入：

```powershell
$env:APP_DATA_SOURCE = "tushare-fixture"
backend\.venv\Scripts\python.exe -m app.data.cli import-history `
  --start 20240102 --end 20240628 --codes "000001.SZ,600000.SH,000003.SZ"
```

## 数据字典与时间口径

| 字段 | 含义 |
|---|---|
| `trade_date` / `cal_date` / `end_date` | 交易所业务日期或事件生效日期（`YYYYMMDD`），不用下载日期代替 |
| `source_published_at` | 供应商可证明的发布/修订时刻；未知为 `NULL` |
| `observed_at` / `ingested_at` | 本系统实际观察与落库时刻，UTC 带时区 |
| `historical_available_at` | 可证明该值在历史上何时可用；不能由业务日期凭空推定，当前为 `NULL` |
| `as_of` | 本次查询或股票池允许使用信息的截止时点 |
| `data_version` / `rule_version` | 固定数据成员与解释规则的不可变标识 |

数据库时间戳统一使用带时区的 UTC ISO-8601；界面按 Asia/Shanghai 展示。交易日期仍按交易所日历解释。

三种价格用途分开提供，互不混用：

| 用途 | 内容 | 约束 |
|---|---|---|
| 原始成交参考价 | 未复权 OHLC、币种与单位 | 不保证一定能成交；成交规则属于阶段 3 |
| 信号计算价 | 派生复权价、算法版本 `sig-v1`、基准日与因子锚点 | 只使用 `trade_date <= as_of` 的复权因子，不让未来公司行为倒灌 |
| 估值参考价 | 指定业务日期可得的未复权价格 | 返回价格日期与陈旧原因；缺失保持缺失 |

日线成交量按供应商“手”换算为股（×100），成交额按“千元”换算为元（×1000）；来源原始数值保存在 `source_volume_raw` / `source_amount_raw`，可逐条核对，不与其他来源静默拼接。

## 测试与验证

后端测试（使用临时目录，不触碰 `.runtime`，不触网）。以下两种调用运行同一套用例：

```powershell
# 在仓库根目录
backend\.venv\Scripts\python.exe -m pytest

# 或在 backend 目录
cd backend
.\.venv\Scripts\python.exe -m pytest
```

前端类型检查与生产构建：

```powershell
cd frontend
npm run typecheck
npm run build
```

规模与增长基准（离线合成数据，运行时生成到临时目录，不提交大文件）：

```powershell
# 存储层：写入/发布/质量/状态 + 250 次清单层发布
backend\.venv\Scripts\python.exe scripts\bench_scale.py --mode storage --codes 100 --years 5

# 完整导入管线：历史导入 + 30 次烟测 + 20 次日常增量/有限修订
backend\.venv\Scripts\python.exe scripts\bench_scale.py --mode pipeline --codes 100 --years 5
```

脚本输出 JSON（耗时、p95、Python 堆峰值、进程峰值 RSS、各目录逻辑/实际字节、版本/分块/引用计数），并保留结果目录供核对；显式 `--workdir` 时只在其下创建新的唯一子目录，不删除既有目录。

启动生命周期验证：

```powershell
# 1. 启动两次：第二次应提示复用，不新增进程
.\scripts\start-local.ps1
.\scripts\start-local.ps1

# 2. 检查接口
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/ready
Invoke-RestMethod http://127.0.0.1:8000/api/system/status
Invoke-RestMethod http://127.0.0.1:8000/api/data/status

# 3. 停止两次：第二次应提示没有记录
.\scripts\stop-local.ps1
.\scripts\stop-local.ps1
```

## 故障排查

| 现象 | 处理 |
|---|---|
| 提示端口被占用 | 修改 `.env` 中的端口，或自行确认并停止占用该端口的程序；脚本不会自动杀进程 |
| 提示后端虚拟环境/前端依赖未安装 | 先运行 `install-local.ps1` |
| `/api/ready` 返回 503 `database_missing` | 确认启动流程完成；检查 `APP_DB_PATH` 与目录权限 |
| `/api/ready` 返回 503 `migration_required` | 旧库仍是 schema 1/2/3；在仓库根目录设置 `$env:PYTHONPATH = (Join-Path $PWD 'backend')`，执行 `backend\.venv\Scripts\python.exe -m app.data.cli migrate`；先核对配置并保留备份 |
| `/api/ready` 返回 503 `database_corrupt` 或 `schema_incompatible` | 保留数据库文件并检查日志。本应用不自动删库；是否移走/修复由用户决定 |
| 导入返回 `credentials_required` | 在 `.env` 里配置 `APP_TUSHARE_TOKEN`；不要把令牌提交到版本库 |
| 导入返回 `no_permission` / `contract_error` | 账号没有该接口权限或请求参数不被接受；按 `source-check` 的文档链接核对，不会自动重试到成功 |
| 导入返回 `rate_limited` | 降低 `APP_IMPORT_MIN_INTERVAL_SECONDS` 以外的取数频率需求，或调大等待预算；重试次数有界，不会无限重试 |
| 导入状态为 `failed` 且没有新版本 | 已发布旧版本仍可读；检查 `import_request_log` 的 `error_class` 与 `detail` 后再重试 |
| 提示已有数据导入正在写入 | 另一个导入正在进行；等待或先停止它，本应用不会终止其他进程 |
| 清理提示 `preview_stale` / `preview_expired` | 预览后数据库已变化或预览超时；重新生成预览再应用，不会使用旧计划删除数据 |
| 清理提示 `recovery_point_required` / `recovery_point_invalid` | 应用需要已验证的操作前恢复点；允许自动创建，或传入通过验证的备份包 |
| 恢复点提示 `recovery_point_instance_mismatch` / `recovery_point_database_mismatch` | 备份属于其他实例或数据库；不会用于本库清理 |
| 恢复点提示 `recovery_point_state_mismatch` / `recovery_point_tampered` | 备份内容不是本库操作前状态（过旧、克隆分叉、不完整或被改动）；拒绝清理 |
| 中断恢复提示 `recovery_point_revalidation_failed` | 提交前登记的恢复点缺失、manifest 被替换、包内数据库/外部文件损坏或内容指纹不符；保留待删文件与全部旧保护，修复原包后可重试 |
| 清理提示 `pending_cleanup_unresolved` | 仍有其他未完成清理的待删文件或 `recorded` 恢复点；新清理不会接管，请先完成原预览的中断收尾或人工恢复其恢复点 |
| 资格/池/质量提示 `version_source_mismatch` | 指定的数据版本属于其他来源；不会用调用方声明的来源覆盖版本真实来源，也不回退到最新数据 |
| 清理提示 `cleanup_post_commit_failed` | 业务清理已提交但外部文件或维护库收尾失败；用 `cleanup status` 核对，重试同一预览会按 journal 收尾 |
| 备份提示 `backup_verification_failed` / `backup_missing_file` | 备份包不完整或外部归档缺失；不会报告成功，也不会据此清理在线数据 |
| 页面显示后端不可用 | 查看 `.runtime\logs\backend.err.log` 和 `backend.out.log`，确认启动脚本成功 |
| 页面显示数据库未就绪 | 查看后端日志和 `/api/ready` 的原因类别 |

日志与运行数据均在 `.runtime` 下，不进入版本库。

## 目录

```text
backend/            FastAPI 后端、依赖锁定、数据层与测试
  app/data/         数据层：适配器、规范化、修订存储、质量、价格、资格、股票池、导入管线、CLI
  app/portfolio/    账户账本、可替换算法、组合建议、模拟、公司行为与绩效
  app/api/          持仓、建议、模拟与绩效 HTTP 接口
  tests/fixtures/   离线合成夹具（不是真实行情）
frontend/           React + TypeScript + Vite 前端
scripts/            install/start/stop PowerShell 脚本与共享函数
.runtime/           本 worktree 的数据库、日志、进程记录（忽略）
```
