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
  const runningCount = sessions.filter((session) => !isFinished(session.status)).length;
  const lastSession =
    sessions.find((session) => session.practice_id === lastSessionId) ?? sessions[0] ?? null;
  const isResume = lastSession !== null && lastSession.practice_id === lastSessionId;

  return (
    <div className="practice-home">
      <section className="practice-hero">
        <div className="practice-hero-copy">
          <p className="eyebrow">历史练习 · 阶段 4</p>
          <h1>用当时能看到的信息，练习一次完整决策</h1>
          <p className="practice-hero-lead">
            新建一段历史区间，查看当时行情，自主下单，再逐日揭晓结果。系统提供三条对照路径与一份可核对复盘，
            不保证任何算法胜出，也不接入真实资金。
          </p>
          <div className="practice-hero-actions">
            {lastSession ? (
              <button
                type="button"
                className="practice-primary"
                disabled={busy}
                onClick={() => onOpen(lastSession.practice_id)}
              >
                {isResume ? "继续上次练习" : "继续最近练习"}
              </button>
            ) : null}
            <a className="practice-secondary-link" href="#new-practice-heading">
              新建练习
            </a>
          </div>
        </div>
        <dl className="practice-hero-stats">
          <div>
            <dt>练习记录</dt>
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

      <section className="practice-steps" aria-label="练习流程">
        <div>
          <span className="practice-step-number">1</span>
          <p>新建练习并设定起点</p>
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
          <p>结束并复盘，随时重开</p>
        </div>
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
      />

      <section className="section" aria-labelledby="history-heading">
        <SectionHeading
          id="history-heading"
          title="报告历史"
          actions={
            <button type="button" className="refresh-button" onClick={onRefreshSessions} disabled={sessionsLoading}>
              {sessionsLoading ? "读取中…" : "刷新列表"}
            </button>
          }
        >
          每个练习都有独立身份和记录；重开会创建新的身份，旧练习与报告继续保留。
        </SectionHeading>

        {sessionsError ? (
          <Notice tone="error" title="读取练习列表失败">
            {sessionsError}
          </Notice>
        ) : null}

        {sessionsLoading && sessions.length === 0 ? (
          <div className="empty-state">
            <p className="empty-title">正在读取练习记录…</p>
          </div>
        ) : sessions.length === 0 ? (
          <EmptyState title="还没有练习记录">
            用上方表单创建第一个历史练习。没有数据时可先准备合成示例，不会写入真实数据。
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
                    {isFinished(session.status) ? "查看复盘" : "继续练习"}
                  </button>
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => setRestartTarget(session)}
                  >
                    重开
                  </button>
                </div>
                {restartTarget?.practice_id === session.practice_id ? (
                  <div className="practice-confirm" role="group" aria-label="确认重开练习">
                    <p>
                      将基于同一段设置创建一个全新的练习身份，旧练习和报告会保留。确认重开吗？
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
                        确认重开
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

      <section className="section practice-phase5-note" aria-label="后续阶段说明">
        <div>
          <h2>Agent 对话将在阶段 5 接入</h2>
          <p className="muted">
            本阶段所有建议与交易都由你确认；页面不展示模拟的聊天、工具进度或自动交易。
          </p>
        </div>
      </section>
    </div>
  );
}
