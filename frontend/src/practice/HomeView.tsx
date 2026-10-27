import { useState } from "react";

import type {
  CreatePracticeInput,
  DemoPreparation,
  PracticeOptions,
  PracticeSummary,
} from "./types";
import { formatDate, isFinished } from "./format";
import { NewPracticeForm } from "./NewPracticeForm";
import { EmptyState, Notice, SectionHeading, StatusBadge, sourceLabel, type Tone } from "./ui";
import { AgentPanel } from "./AgentPanel";
import { DataPreparation } from "../phase6/DataPreparation";
import type { PreparedDataset } from "../phase6/types";

function summaryMeta(session: PracticeSummary): string {
  const parts = [formatDate(session.start_date), formatDate(session.end_date)];
  if (session.revealed_date) {
    parts.push(`已揭晓至 ${formatDate(session.revealed_date)}`);
  }
  return parts.join(" · ");
}

export function HomeView({
  options,
  optionsError,
  sessions,
  sessionsLoading,
  sessionsError,
  demo,
  demoBusy,
  creating,
  busy,
  lastSessionId,
  notice,
  onDismissNotice,
  onCreate,
  onPrepareDemo,
  onOpen,
  onRestart,
  onRefreshSessions,
  onReloadOptions,
}: {
  options: PracticeOptions | null;
  optionsError: string | null;
  sessions: PracticeSummary[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  demo: DemoPreparation | null;
  demoBusy: boolean;
  creating: boolean;
  busy: boolean;
  lastSessionId: string | null;
  notice: { tone: Tone; text: string } | null;
  onDismissNotice: () => void;
  onCreate: (input: CreatePracticeInput) => Promise<void>;
  onPrepareDemo: () => Promise<void>;
  onOpen: (practiceId: string) => void;
  onRestart: (practiceId: string) => Promise<void>;
  onRefreshSessions: () => void;
  onReloadOptions: () => void;
}) {
  const [restartTarget, setRestartTarget] = useState<PracticeSummary | null>(null);
  const [setupDraft, setSetupDraft] = useState<Record<string, unknown> | null>(null);
  const [setupDraftOrigin, setSetupDraftOrigin] = useState<string | null>(null);
  const [dataPreparationOpen, setDataPreparationOpen] = useState(false);
  const runningCount = sessions.filter((session) => !isFinished(session.status)).length;
  const lastSession =
    sessions.find((session) => session.practice_id === lastSessionId) ?? sessions[0] ?? null;
  const isResume = lastSession !== null && lastSession.practice_id === lastSessionId;

  function openDataPreparation() {
    setDataPreparationOpen(true);
    window.setTimeout(() => {
      const summary = document.getElementById("data-prepare-summary");
      summary?.scrollIntoView({ behavior: "smooth", block: "start" });
      summary?.focus();
    }, 0);
  }

  return (
    <div className="practice-home">
      <section className="practice-hero">
        <div className="practice-hero-copy">
          <p className="eyebrow">历史模拟工作台</p>
          <h1>开始一段历史模拟，或继续上次进度</h1>
          <p className="practice-hero-lead">
            第一次使用可以先体验合成示例，也可以准备服务端支持的历史数据。创建后按当时可见行情操作并逐日揭晓；不接入真实资金。
          </p>
          <div className="practice-hero-actions">
            {lastSession ? (
              <button
                type="button"
                className="practice-primary"
                disabled={busy}
                onClick={() => onOpen(lastSession.practice_id)}
              >
                {isResume ? "继续上次模拟" : "继续最近模拟"}
              </button>
            ) : null}
            <a className="practice-secondary-link" href="#new-practice-heading">
              创建模拟
            </a>
          </div>
        </div>
        <dl className="practice-hero-stats">
          <div>
            <dt>模拟记录</dt>
            <dd>{sessions.length}</dd>
          </div>
          <div>
            <dt>进行中</dt>
            <dd>{runningCount}</dd>
          </div>
          <div>
            <dt>已完成</dt>
            <dd>{sessions.length - runningCount}</dd>
          </div>
        </dl>
      </section>

      <section className="practice-start-choices" aria-label="选择开始方式">
        <a href="#new-practice-heading">
          <span className="eyebrow">第一次体验</span>
          <strong>体验合成示例</strong>
          <span>使用明确标记的离线样本，不需要模型或服务商凭据。</span>
        </a>
        <button type="button" onClick={openDataPreparation}>
          <span className="eyebrow">已有数据来源</span>
          <strong>准备历史数据</strong>
          <span>先查看支持来源、日期范围与资格，再由你选择是否准备。</span>
        </button>
      </section>

      {notice ? (
        <Notice tone={notice.tone} onDismiss={onDismissNotice}>
          {notice.text}
        </Notice>
      ) : null}

      {optionsError ? (
        <Notice tone="warn" title="数据选项暂不可用">
          <p>{optionsError}</p>
          <div className="practice-notice-actions">
            <button type="button" className="refresh-button" onClick={onReloadOptions}>
              重新读取数据选项
            </button>
          </div>
        </Notice>
      ) : null}

      <NewPracticeForm
        options={options}
        demo={demo}
        demoBusy={demoBusy}
        creating={creating}
        onCreate={onCreate}
        onPrepareDemo={onPrepareDemo}
        setupDraft={setupDraft}
      />

      {setupDraftOrigin ? <Notice tone="neutral" title={`${setupDraftOrigin}已填入创建表单`}>
        <p>请核对日期、资金、数据版本和持仓，再手动确认创建。</p>
      </Notice> : null}

      <details
        className="practice-data-preparation"
        id="data-prepare-section"
        open={dataPreparationOpen}
        onToggle={(event) => setDataPreparationOpen(event.currentTarget.open)}
      >
        <summary id="data-prepare-summary">高级数据准备 <span>选择服务端支持的来源、区间并查看数据资格</span></summary>
        <p className="practice-field-hint practice-data-preparation-note">
          这里调用独立的数据准备流程，生成的是数据集样本，不等同于上方的内置模拟演示。合成样本也可能受资格或行情范围限制；能否创建和成交以服务端结果及模拟规则为准。
        </p>
        <DataPreparation onUseDataset={(dataset: PreparedDataset) => {
          const weekday = (value: string, direction: 1 | -1) => {
            const date = new Date(`${value}T00:00:00Z`);
            while (date.getUTCDay() === 0 || date.getUTCDay() === 6) date.setUTCDate(date.getUTCDate() + direction);
            return date.toISOString().slice(0, 10);
          };
          const startDate = dataset.synthetic ? weekday(dataset.start_date, 1) : dataset.start_date;
          const endDate = dataset.synthetic ? weekday(dataset.end_date, -1) : dataset.end_date;
          setSetupDraft({ name: dataset.synthetic ? "合成示例模拟" : "历史数据模拟", start_date: startDate, end_date: endDate, source_code: dataset.source_code, data_version_id: dataset.data_version_id });
          const sourceName = options?.sources.find((source) => source.code === dataset.source_code)?.label;
          setSetupDraftOrigin(`${dataset.synthetic ? "Stage 6 受限合成数据样本" : "历史数据集"}${sourceName ? ` · ${sourceName}` : ""}`);
          onReloadOptions();
          window.setTimeout(() => document.getElementById("new-practice-heading")?.scrollIntoView({ behavior: "smooth", block: "center" }), 0);
        }} />
      </details>
      <AgentPanel onSetupDraft={(payload) => {
        setSetupDraft(payload);
        setSetupDraftOrigin("Agent 条件草稿");
        window.setTimeout(() => document.getElementById("new-practice-heading")?.scrollIntoView({ behavior: "smooth", block: "center" }), 0);
      }} />

      <section className="practice-steps" aria-label="历史模拟流程">
        <div>
          <span className="practice-step-number">1</span>
          <p>创建模拟并设定起点</p>
        </div>
        <div>
          <span className="practice-step-number">2</span>
          <p>查看已揭晓行情并下单</p>
        </div>
        <div>
          <span className="practice-step-number">3</span>
          <p>逐日揭晓、对照三路曲线</p>
        </div>
        <div>
          <span className="practice-step-number">4</span>
          <p>结束并查看模拟报告</p>
        </div>
      </section>

      <section className="section" aria-labelledby="history-heading">
        <SectionHeading
          id="history-heading"
          title="历史模拟"
          actions={
            <button type="button" className="refresh-button" onClick={onRefreshSessions} disabled={sessionsLoading}>
              {sessionsLoading ? "读取中…" : "刷新列表"}
            </button>
          }
        >
          每段模拟都有独立身份；重开会创建新的模拟，旧记录与模拟报告继续保留。
        </SectionHeading>

        {sessionsError ? (
          <Notice tone="error" title="读取历史模拟失败">
            {sessionsError}
          </Notice>
        ) : null}

        {sessionsLoading && sessions.length === 0 ? (
          <div className="empty-state">
            <p className="empty-title">正在读取模拟记录…</p>
          </div>
        ) : sessions.length === 0 ? (
          <EmptyState title="还没有历史模拟">
            用上方表单创建第一段历史模拟。没有数据时可先准备合成示例，不会写入真实数据。
          </EmptyState>
        ) : (
          <div className="practice-session-list">
            {sessions.map((session) => (
              <article className="practice-session-row" key={session.practice_id}>
                <div className="practice-session-main">
                  <div className="practice-session-title">
                    <h3>{session.name}</h3>
                    <StatusBadge status={session.status} />
                    {session.source?.synthetic ? (
                      <span className="chip chip-warn">
                        <span className="chip-dot" aria-hidden="true" />
                        合成示例
                      </span>
                    ) : null}
                  </div>
                  <p className="muted">{summaryMeta(session)}</p>
                  <p className="muted">来源：{sourceLabel(session.source)}</p>
                </div>
                <div className="practice-session-actions">
                  <button
                    type="button"
                    className="refresh-button"
                    disabled={busy}
                    onClick={() => onOpen(session.practice_id)}
                  >
                    {isFinished(session.status) ? "查看模拟报告" : "继续模拟"}
                  </button>
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => setRestartTarget(session)}
                  >
                    重新创建
                  </button>
                </div>
                {restartTarget?.practice_id === session.practice_id ? (
                  <div className="practice-confirm" role="group" aria-label="确认重新创建模拟">
                    <p>
                      将基于同一段设置创建一个新的模拟身份，旧记录与报告会保留。确认继续吗？
                    </p>
                    <div className="practice-confirm-actions">
                      <button
                        type="button"
                        className="practice-primary"
                        disabled={busy}
                        onClick={() => {
                          const target = restartTarget;
                          setRestartTarget(null);
                          if (target) {
                            void onRestart(target.practice_id);
                          }
                        }}
                      >
                        确认重新创建
                      </button>
                      <button type="button" className="link-button" onClick={() => setRestartTarget(null)}>
                        取消
                      </button>
                    </div>
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

    </div>
  );
}
