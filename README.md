# AI 量化模拟交易系统

面向量化研究与模拟交易的持续迭代项目。项目计划逐步建设行情接入、模拟账户、策略研究与回测等能力；当前已完成**阶段 1：本地运行基础**，为后续功能提供前后端结构、运行状态检查和持久化元信息。项目仓库：[Abai-13/quant-research-lab](https://github.com/Abai-13/quant-research-lab)。

阶段 1 使用 **FastAPI + SQLite** 提供后端服务，使用 **React + TypeScript + Vite** 提供单页状态界面。页面读取真实接口状态，明确区分服务存活、数据库就绪和业务功能准备情况；尚未实现的功能显示状态而不填入模拟数据。

## 阶段 1 已实现

- 本地 FastAPI 后端与 React/TypeScript 前端，前后端作为独立服务运行，并由 Vite 代理 `/api` 请求。
- `GET /api/health`、`GET /api/ready`、`GET /api/system/status`，分别报告服务存活、SQLite 就绪和各功能模块状态。
- SQLite 元信息初始化与校验，保存 schema 版本、实例 ID 和创建时间；重复启动会保留实例信息，损坏或不兼容时不会覆盖数据库。
- 状态页面支持后端断连检测与恢复，提供桌面和移动端布局。
- PowerShell 本地安装、启动和停止脚本，包含端口、进程身份和启动就绪检查。
- 后端 30 项测试、前端类型检查与生产构建验证。

当前**没有**行情接入、模拟账户、持仓、交易、策略、回测、定时任务或 AI 功能，也没有云端部署配置。系统不会展示虚构的净值、收益、持仓、成交或曲线。

## 本地部署速览

当前支持 Windows 10/11 本地运行。需要 Python 3.12、Node.js 20.19 或更新版本及 npm；不需要行情或大模型密钥。

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

服务仅绑定本机回环地址。阶段 1 未配置登录、HTTPS 或公网访问，因此这些步骤是**本地部署/运行方式，不是公网生产部署方案**。端口、运行目录与数据库路径等可通过项目根目录的 `.env` 配置，详见下文。

## 环境要求

| 项目 | 要求 | 本次验证版本 |
|---|---|---|
| 操作系统 | Windows 10/11 | Windows 11 |
| PowerShell | Windows PowerShell 5.1 或 PowerShell 7 | Windows PowerShell 5.1 |
| Python | 3.12.x | 3.12.10 |
| Node.js | >= 20.19 | 24.16.0 |
| npm | 随 Node.js 安装 | 11.13.0 |

不需要行情、Tushare 或大模型密钥即可启动。

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
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\path\to\phase-1-foundation\scripts\start-local.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\path\to\phase-1-foundation\scripts\stop-local.ps1"
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
- 不关闭 Orca 终端或其他不属于本项目的进程。

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

非法端口、空主机名等配置会让启动明确失败，并给出原因。

`.runtime` 已被 `.gitignore` 忽略；不同 worktree 各自使用自己的 `.runtime`，不会共用数据库或进程记录。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 进程存活；正常返回 200，不表示数据库或业务功能就绪 |
| `GET /api/ready` | 数据库只读检查；就绪返回 200，否则 503 |
| `GET /api/system/status` | 功能准备状态；未实现项目返回明确状态与 `null` 数值 |

示例（实际响应）：

```json
{"service":"ai-quant-simulator","name":"AI 量化模拟交易系统","version":"0.1.0","mode":"local","status":"ok"}
```

```json
{"status":"ready","database":{"status":"ready","schema_version":1,"instance_id":"520829cb-7a7c-45cc-a08f-ec7d136537ed","created_at":"2026-10-21T04:44:21+00:00"}}
```

```json
{
  "service": "ai-quant-simulator",
  "name": "AI 量化模拟交易系统",
  "version": "0.1.0",
  "mode": "local",
  "generated_at": "2026-10-21T04:44:28+00:00",
  "market_data": {"status": "not_connected"},
  "account": {"status": "not_initialized", "net_value": null, "cumulative_return": null},
  "strategy": {"status": "not_implemented"},
  "scheduler": {"status": "not_implemented"},
  "ai": {"status": "not_configured"},
  "limitations": ["..."]
}
```

数据库未就绪时，`/api/ready` 返回 503，`database.reason` 为以下之一：`database_missing`、`database_corrupt`、`schema_incompatible`、`database_unavailable`。检查过程只读，不会创建、删除或重建数据库。

## SQLite 元信息

阶段 1 只创建一张 `app_metadata` 表，保存：

- `schema_version`：当前为 `1`
- `created_at`：首次初始化时间（UTC）
- `instance_id`：首次初始化生成的实例标识

启动时以事务初始化；重复启动和重启保留原有值。数据库损坏、包含非本应用的表或 schema 版本不一致时，服务会保留文件并报告未就绪，不会自动修复或覆盖。

## 测试与验证

后端测试（使用临时目录，不触碰 `.runtime`）。以下两种调用运行同一套用例：

```powershell
# 在仓库根目录
backend\.venv\Scripts\python.exe -m pytest

# 或在 backend 目录
cd backend
.\.venv\Scripts\python.exe -m pytest
```

前端类型检查与生产构建（构建产物仅用于验证，不作为阶段 1 的部署方式）：

```powershell
cd frontend
npm run typecheck
npm run build
```

启动生命周期验证：

```powershell
# 1. 启动两次：第二次应提示复用，不新增进程
.\scripts\start-local.ps1
.\scripts\start-local.ps1

# 2. 检查接口
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/ready
Invoke-RestMethod http://127.0.0.1:8000/api/system/status

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
| `/api/ready` 返回 503 `database_corrupt` 或 `schema_incompatible` | 保留数据库文件并检查日志。阶段 1 不自动删库；是否移走/修复由用户决定 |
| 页面显示后端不可用 | 查看 `.runtime\logs\backend.err.log` 和 `backend.out.log`，确认启动脚本成功 |
| 页面显示数据库未就绪 | 查看后端日志和 `/api/ready` 的原因类别 |

日志与运行数据均在 `.runtime` 下，不进入版本库。

## 目录

```text
backend/            FastAPI 后端、依赖锁定、测试
frontend/           React + TypeScript + Vite 前端
scripts/            install/start/stop PowerShell 脚本与共享函数
.runtime/           本 worktree 的数据库、日志、进程记录（忽略）
```

## 阶段 1 明确未实现

行情下载与交易日历、模拟账户与持仓、选股/调仓/回测/优化、定时任务、AI 周报、登录认证、HTTPS 与云端部署配置。页面与接口只显示这些功能的真实准备状态。
