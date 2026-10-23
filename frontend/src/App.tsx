import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { getJson } from "./api";
import { PortfolioPanel } from "./PortfolioPanel";
import type { DataStatus, Health, Ready, SystemStatus } from "./types";

type Tone = "ok" | "warn" | "error" | "neutral";

interface Snapshot {
  health: Health | null;
  ready: Ready | null;
  system: SystemStatus | null;
  data: DataStatus | null;
  healthError: string | null;
  readyError: string | null;
  systemError: string | null;
  dataError: string | null;
  checkedAt: Date | null;
}

const EMPTY_SNAPSHOT: Snapshot = {
  health: null,
  ready: null,
  system: null,
  data: null,
  healthError: null,
  readyError: null,
  systemError: null,
  dataError: null,
  checkedAt: null,
};

const REFRESH_INTERVAL_MS = 10_000;

const DB_REASON_LABELS: Record<string, string> = {
  database_missing: "数据库尚未初始化",
  database_corrupt: "数据库文件损坏",
  schema_incompatible: "数据库 schema 不兼容",
  database_unavailable: "数据库当前不可访问",
  migration_required: "数据库需要先执行迁移",
};

const FEATURE_STATUS: Record<string, { label: string; tone: Tone }> = {
  not_connected: { label: "未接入", tone: "neutral" },
  not_initialized: { label: "未初始化", tone: "neutral" },
  not_implemented: { label: "未实现", tone: "neutral" },
  not_configured: { label: "未配置", tone: "neutral" },
  import_failed: { label: "导入失败", tone: "error" },
  pending_check: { label: "待检查", tone: "warn" },
  partial_coverage: { label: "部分覆盖", tone: "warn" },
  limited: { label: "受限", tone: "warn" },
  available: { label: "可用", tone: "ok" },
  available_in_range: { label: "范围内可用", tone: "ok" },
  migration_required: { label: "需要迁移", tone: "error" },
};

const UNKNOWN_FEATURE = { label: "状态未知", tone: "neutral" as Tone };

const FEATURES: Array<{
  key: "market_data" | "account" | "strategy" | "scheduler" | "ai";
  label: string;
  detail: string;
}> = [
  { key: "market_data", label: "行情数据", detail: "阶段 2 建立数据接入与质量检查基础" },
  { key: "account", label: "持仓与模拟", detail: "阶段三提供个人持仓、模拟成交、公司行为与 NAV/收益" },
  { key: "strategy", label: "组合建议", detail: "阶段三提供渐进调整与同资金独立推荐；不构成投资建议" },
  { key: "scheduler", label: "定时任务", detail: "尚未实现每日更新或每周调仓调度" },
  { key: "ai", label: "AI 分析与周报", detail: "未配置模型，也不作为本地启动依赖" },
];

const DATA_COVERAGE_LABELS: Record<string, string> = {
  calendar: "交易日历",
  security: "股票基础资料",
  daily: "日线行情",
  adj_factor: "复权因子",
  suspension: "停复牌",
  corporate_action: "公司行为",
};

function formatTime(date: Date): string {
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function StatusChip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`chip chip-${tone}`}>
      <span className="chip-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

function StatusCard({
  title,
  tone,
  label,
  children,
}: {
  title: string;
  tone: Tone;
  label: string;
  children?: ReactNode;
}) {
  return (
    <article className="card status-card">
      <div className="card-top">
        <h3>{title}</h3>
        <StatusChip tone={tone}>{label}</StatusChip>
      </div>
      <div className="card-body">{children}</div>
    </article>
  );
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>(EMPTY_SNAPSHOT);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const [healthResult, readyResult, systemResult, dataResult] = await Promise.allSettled([
        getJson<Health>("/api/health", controller.signal),
        getJson<Ready>("/api/ready", controller.signal),
        getJson<SystemStatus>("/api/system/status", controller.signal),
        getJson<DataStatus>("/api/data/status", controller.signal),
      ]);

      if (controller.signal.aborted) {
        return;
      }

      const next: Snapshot = {
        health: null,
        ready: null,
        system: null,
        data: null,
        healthError: null,
        readyError: null,
        systemError: null,
        dataError: null,
        checkedAt: new Date(),
      };

      if (healthResult.status === "fulfilled") {
        const response = healthResult.value;
        if (response.ok && isObject(response.body)) {
          next.health = response.body as unknown as Health;
        } else {
          next.healthError = `后端返回 HTTP ${response.status}`;
        }
      } else {
        next.healthError = "无法连接后端服务";
      }

      if (readyResult.status === "fulfilled") {
        const response = readyResult.value;
        if (isObject(response.body) && "database" in response.body) {
          next.ready = response.body as unknown as Ready;
        } else {
          next.readyError = `数据库状态接口返回 HTTP ${response.status}`;
        }
      } else {
        next.readyError = "无法获取数据库状态";
      }

      if (systemResult.status === "fulfilled") {
        const response = systemResult.value;
        if (response.ok && isObject(response.body) && "market_data" in response.body) {
          next.system = response.body as unknown as SystemStatus;
        } else {
          next.systemError = `功能状态接口返回 HTTP ${response.status}`;
        }
      } else {
        next.systemError = "无法获取功能状态";
      }

      if (dataResult.status === "fulfilled") {
        const response = dataResult.value;
        if (isObject(response.body) && "source" in response.body) {
          next.data = response.body as unknown as DataStatus;
        } else {
          next.dataError = `数据状态接口返回 HTTP ${response.status}`;
        }
      } else {
        next.dataError = "无法获取数据状态";
      }

      setSnapshot(next);
    } finally {
      if (abortRef.current === controller) {
        inFlightRef.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      abortRef.current?.abort();
      inFlightRef.current = false;
    };
  }, [refresh]);

  const system = snapshot.system;
  const database = snapshot.ready?.database ?? null;
  const connected = snapshot.health !== null;

  const backendTone: Tone = connected ? "ok" : "error";
  const backendLabel = connected ? "正常" : "未连接";

  let databaseTone: Tone = "neutral";
  let databaseLabel = "等待检查";
  let databaseDetail: ReactNode = <p className="muted">正在读取数据库状态…</p>;

  if (!connected) {
    databaseTone = "neutral";
    databaseLabel = "无法判断";
    databaseDetail = <p className="muted">后端未连接时，无法判断数据库状态。</p>;
  } else if (snapshot.readyError) {
    databaseTone = "error";
    databaseLabel = "状态不可用";
    databaseDetail = <p className="muted">暂时无法获取数据库就绪状态。</p>;
  } else if (database?.status === "ready") {
    databaseTone = "ok";
    databaseLabel = "正常";
    databaseDetail = (
      <dl className="details">
        <div>
          <dt>schema 版本</dt>
          <dd>{database.schema_version}</dd>
        </div>
        <div>
          <dt>创建时间</dt>
          <dd>{formatDateTime(database.created_at)}</dd>
        </div>
      </dl>
    );
  } else if (database?.status === "not_ready") {
    const missing = database.reason === "database_missing";
    databaseTone = missing ? "warn" : "error";
    databaseLabel = DB_REASON_LABELS[database.reason] ?? "未就绪";
    databaseDetail = (
      <p className="muted">
        {missing
          ? "启动后端时会自动初始化数据库；若仍显示此状态，请检查运行目录权限和配置。"
          : "数据库未通过就绪检查。后端仍可响应健康检查，请查看本地日志并保留数据库文件。"}
      </p>
    );
  } else if (snapshot.checkedAt) {
    databaseTone = "neutral";
    databaseLabel = "未检查";
    databaseDetail = <p className="muted">等待下一次检查。</p>;
  }

  const checkedLabel = snapshot.checkedAt ? formatTime(snapshot.checkedAt) : "尚未检查";

  const data = snapshot.data;
  const dataMeta = data ? FEATURE_STATUS[data.status] ?? UNKNOWN_FEATURE : UNKNOWN_FEATURE;
  const dataTone: Tone = snapshot.dataError ? "error" : dataMeta.tone;
  const dataLabel = snapshot.dataError ? "状态不可用" : dataMeta.label;

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="eyebrow">本地模拟 · 阶段 2</p>
          <h1>AI 量化模拟交易系统</h1>
          <p className="subtitle">数据接入与质量检查</p>
        </div>
        <div className="header-actions">
          <span className="mode-badge">本地模式</span>
          <button
            type="button"
            className="refresh-button"
            onClick={() => void refresh()}
            disabled={loading}
          >
            {loading ? "检查中…" : "重新检查"}
          </button>
        </div>
      </header>

      <main>
        {snapshot.healthError ? (
          <div className="banner" role="alert">
            <strong>后端服务不可用</strong>
            <p>
              无法读取运行状态。请确认 start-local.ps1 已成功执行，且前后端端口配置一致。
              页面每 10 秒自动重试，恢复后会更新为真实状态。
            </p>
          </div>
        ) : null}

        <section className="section" aria-labelledby="runtime-heading">
          <div className="section-heading">
            <h2 id="runtime-heading">运行状态</h2>
            <p>后端存活与数据库就绪分别检查，状态直接来自接口响应。</p>
          </div>
          <div className="status-grid">
            <StatusCard title="后端服务" tone={backendTone} label={backendLabel}>
              {snapshot.health ? (
                <dl className="details">
                  <div>
                    <dt>服务</dt>
                    <dd>{snapshot.health.service}</dd>
                  </div>
                  <div>
                    <dt>版本</dt>
                    <dd>v{snapshot.health.version}</dd>
                  </div>
                  <div>
                    <dt>模式</dt>
                    <dd>{snapshot.health.mode}</dd>
                  </div>
                </dl>
              ) : (
                <p className="muted">未收到健康检查响应。</p>
              )}
            </StatusCard>

            <StatusCard title="数据库" tone={databaseTone} label={databaseLabel}>
              {databaseDetail}
            </StatusCard>

            <StatusCard title="上次检查" tone="neutral" label={checkedLabel}>
              <p className="muted">每 10 秒自动检查一次；断连后不会保留旧的“正常”状态。</p>
            </StatusCard>
          </div>
        </section>

        <section className="section" aria-labelledby="data-heading">
          <div className="section-heading">
            <h2 id="data-heading">数据状态</h2>
            <p>摘要来自真实的数据检查；没有数据时显示空状态，不展示假价格或假收益。</p>
          </div>
          {snapshot.dataError ? (
            <div className="empty-state">
              <p className="empty-title">暂时无法读取数据状态</p>
              <p>{snapshot.dataError}</p>
            </div>
          ) : data ? (
            <div className="status-grid">
              <StatusCard title="数据准备程度" tone={dataTone} label={dataLabel}>
                <p className="muted">{data.detail ?? "状态来自后端数据检查结果。"}</p>
                <dl className="details">
                  <div>
                    <dt>数据来源</dt>
                    <dd>{data.source.source_code}</dd>
                  </div>
                  <div>
                    <dt>凭据</dt>
                    <dd>{data.source.credentials_present ? "已配置" : "未配置"}</dd>
                  </div>
                  <div>
                    <dt>数据版本</dt>
                    <dd>{data.data_versions.length}</dd>
                  </div>
                  <div>
                    <dt>股票池快照</dt>
                    <dd>{data.pool_snapshots.length}</dd>
                  </div>
                </dl>
              </StatusCard>

              <StatusCard title="覆盖范围" tone="neutral" label={`${data.record_count ?? 0} 条`}>
                {Object.keys(data.coverage).length === 0 ? (
                  <p className="muted">尚未导入任何数据。</p>
                ) : (
                  <dl className="details">
                    {Object.entries(data.coverage).map(([key, entry]) => (
                      <div key={key}>
                        <dt>{DATA_COVERAGE_LABELS[key] ?? key}</dt>
                        <dd>
                          {entry.count} 条
                          {entry.start ? ` · ${entry.start} ~ ${entry.end ?? "…"}` : ""}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </StatusCard>

              <StatusCard
                title="最近导入与质量"
                tone={data.quality.by_severity.high > 0 ? "warn" : "neutral"}
                label={`${data.quality.total} 个问题`}
              >
                {data.last_import ? (
                  <dl className="details">
                    <div>
                      <dt>批次状态</dt>
                      <dd>
                        {data.last_import.status}
                        {data.last_import.error_class ? `（${data.last_import.error_class}）` : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>完成时间</dt>
                      <dd>
                        {data.last_import.finished_at
                          ? formatDateTime(data.last_import.finished_at)
                          : "未完成"}
                      </dd>
                    </div>
                    <div>
                      <dt>分页成功 / 失败</dt>
                      <dd>
                        {data.last_import.pages_ok} / {data.last_import.pages_failed}
                      </dd>
                    </div>
                    <div>
                      <dt>高严重度问题</dt>
                      <dd>{data.quality.by_severity.high}</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="muted">还没有导入记录。</p>
                )}
              </StatusCard>
            </div>
          ) : (
            <div className="empty-state">
              <p className="empty-title">正在读取数据状态…</p>
            </div>
          )}
        </section>

        <section className="section" aria-labelledby="features-heading">
          <div className="section-heading">
            <h2 id="features-heading">功能准备状态</h2>
            <p>未实现的模块只显示状态，不展示数值或占位曲线。</p>
          </div>
          <div className="list-card">
            {system ? (
              FEATURES.map((feature) => {
                const meta = FEATURE_STATUS[system[feature.key].status] ?? UNKNOWN_FEATURE;
                return (
                  <div className="feature-row" key={feature.key}>
                    <div>
                      <h3>{feature.label}</h3>
                      <p>{feature.detail}</p>
                    </div>
                    <StatusChip tone={meta.tone}>{meta.label}</StatusChip>
                  </div>
                );
              })
            ) : (
              <div className="feature-row">
                <div>
                  <h3>功能状态</h3>
                  <p>{snapshot.systemError ?? "正在读取功能状态…"}</p>
                </div>
                <StatusChip tone="neutral">不可用</StatusChip>
              </div>
            )}
          </div>
        </section>

        <PortfolioPanel />

        <section className="section" aria-labelledby="limits-heading">
          <div className="section-heading">
            <h2 id="limits-heading">本地运行说明</h2>
          </div>
          {system ? (
            <ul className="limitations">
              {system.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">状态接口不可用，暂时无法显示运行限制。</p>
          )}
        </section>
      </main>

      <footer className="page-footer">
        <span>阶段 3 · 本地模拟核心</span>
        <span>{snapshot.health ? `后端 v${snapshot.health.version}` : "后端未连接"}</span>
        <span>仅用于本机开发与验证</span>
      </footer>
    </div>
  );
}
