import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { getJson } from "./api";
import type { Health, Ready, SystemStatus } from "./types";

type Tone = "ok" | "warn" | "error" | "neutral";

interface Snapshot {
  health: Health | null;
  ready: Ready | null;
  system: SystemStatus | null;
  healthError: string | null;
  readyError: string | null;
  systemError: string | null;
  checkedAt: Date | null;
}

const EMPTY_SNAPSHOT: Snapshot = {
  health: null,
  ready: null,
  system: null,
  healthError: null,
  readyError: null,
  systemError: null,
  checkedAt: null,
};

const REFRESH_INTERVAL_MS = 10_000;

const DB_REASON_LABELS: Record<string, string> = {
  database_missing: "数据库尚未初始化",
  database_corrupt: "数据库文件损坏",
  schema_incompatible: "数据库 schema 不兼容",
  database_unavailable: "数据库当前不可访问",
};

const FEATURE_STATUS: Record<string, { label: string; tone: Tone }> = {
  not_connected: { label: "未接入", tone: "neutral" },
  not_initialized: { label: "未初始化", tone: "neutral" },
  not_implemented: { label: "未实现", tone: "neutral" },
  not_configured: { label: "未配置", tone: "neutral" },
};

const UNKNOWN_FEATURE = { label: "状态未知", tone: "neutral" as Tone };

const FEATURES: Array<{
  key: "market_data" | "account" | "strategy" | "scheduler" | "ai";
  label: string;
  detail: string;
}> = [
  { key: "market_data", label: "行情数据", detail: "真实行情与交易日历将在阶段 2 接入" },
  { key: "account", label: "模拟账户", detail: "阶段 1 不创建账户，也不展示净值或收益" },
  { key: "strategy", label: "策略与回测", detail: "尚未实现选股、调仓或回测逻辑" },
  { key: "scheduler", label: "定时任务", detail: "尚未实现每日更新或每周调仓调度" },
  { key: "ai", label: "AI 分析与周报", detail: "未配置模型，也不作为本地启动依赖" },
];

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
      const [healthResult, readyResult, systemResult] = await Promise.allSettled([
        getJson<Health>("/api/health", controller.signal),
        getJson<Ready>("/api/ready", controller.signal),
        getJson<SystemStatus>("/api/system/status", controller.signal),
      ]);

      if (controller.signal.aborted) {
        return;
      }

      const next: Snapshot = {
        health: null,
        ready: null,
        system: null,
        healthError: null,
        readyError: null,
        systemError: null,
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

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="eyebrow">本地模拟 · 阶段 1</p>
          <h1>AI 量化模拟交易系统</h1>
          <p className="subtitle">本地基础状态</p>
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

        <section className="section" aria-labelledby="account-heading">
          <div className="section-heading">
            <h2 id="account-heading">账户与收益</h2>
            <p>没有数据时不使用零值、示例持仓或虚构曲线填充页面。</p>
          </div>
          <div className="empty-state">
            <p className="empty-title">暂无账户数据</p>
            <p>
              阶段 1 只验证本地服务与数据库连接。模拟账户、持仓、成交和收益尚未实现，
              因此这里不展示任何数值。
            </p>
          </div>
        </section>

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
        <span>阶段 1 · 本地基础</span>
        <span>{snapshot.health ? `后端 v${snapshot.health.version}` : "后端未连接"}</span>
        <span>仅用于本机开发与验证</span>
      </footer>
    </div>
  );
}
