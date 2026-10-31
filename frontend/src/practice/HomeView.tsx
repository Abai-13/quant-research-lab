import { useState } from "react";

import type { CreatePracticeInput, DemoPreparation, PracticeOptions, PracticePlan, PracticeSummary, PlanPayload } from "./types";
import type { PracticeSection } from "./PracticeWorkspace";
import { formatDate, isFinished } from "./format";
import { NewPracticeForm } from "./NewPracticeForm";
import { EmptyState, Notice, StatusBadge, sourceLabel, type Tone } from "./ui";
import { AgentPanel } from "./AgentPanel";

function summaryMeta(session: PracticeSummary): string {
  return [formatDate(session.start_date), formatDate(session.end_date), session.revealed_date ? `已揭晓至 ${formatDate(session.revealed_date)}` : null].filter(Boolean).join(" · ");
}

export function HomeView({
  section, options, optionsError, sessions, sessionsLoading, sessionsError, lastSessionId, demo, demoBusy, creating,
  plan, planBusy, setupDraft, notice, onDismissNotice, onCreate, onSavePlan, onConfirmPlan,
  onApplySetupDraft, onSetupDraftConsumed, onPrepareDemo, onOpen, onRestart, onRefreshSessions, onReloadOptions, onNavigate, onGoControl,
}: {
  section: PracticeSection;
  options: PracticeOptions | null;
  optionsError: string | null;
  sessions: PracticeSummary[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  lastSessionId?: string | null;
  demo: DemoPreparation | null;
  demoBusy: boolean;
  creating: boolean;
  plan: PracticePlan | null;
  planBusy: boolean;
  setupDraft: Record<string, unknown> | null;
  notice: { tone: Tone; text: string } | null;
  onDismissNotice: () => void;
  onCreate: (input: CreatePracticeInput) => Promise<void>;
  onSavePlan: (payload: PlanPayload, symbols: string[], goal: string, key: string) => Promise<void>;
  onConfirmPlan: () => Promise<void>;
  onApplySetupDraft: (payload: Record<string, unknown>) => void;
  onSetupDraftConsumed?: () => void;
  onPrepareDemo: () => Promise<void>;
  onOpen: (practiceId: string) => void;
  onRestart: (practiceId: string) => Promise<void>;
  onRefreshSessions: () => void;
  onReloadOptions: () => void;
  onNavigate?: (section: PracticeSection) => void;
  onGoControl?: () => void;
}) {
  const [restartTarget, setRestartTarget] = useState<string | null>(null);
  const runningCount = sessions.filter((session) => !isFinished(session.status)).length;
  const lastSession = sessions.find((session) => session.practice_id === lastSessionId) ?? sessions[0] ?? null;

  return <div className="practice-home">
    {section === "home" ? <>
      <section className="practice-hero">
        <div className="practice-hero-copy">
          <p className="eyebrow">Agent 投资练习 · 历史数据模拟</p>
          <h1>先说目标，再检查方案</h1>
          <p className="practice-hero-lead">选择本地演示或可用数据，和 Agent 一起整理方案；资格检查通过后，由你确认创建。不会接入真实资金。</p>
          <div className="practice-hero-actions">
            <button type="button" className="practice-primary" onClick={() => void onPrepareDemo()} disabled={demoBusy || options?.demo_available === false}>{demoBusy ? "正在准备…" : "带我体验一次"}</button>
            {lastSession ? <button type="button" className="practice-secondary" onClick={() => onOpen(lastSession.practice_id)}>继续上次练习</button> : null}
            <button type="button" className="practice-secondary" onClick={() => onNavigate?.("records")}>查看练习记录 · {sessions.length}</button>
          </div>
          {demo ? <p className="practice-field-hint">演示范围已由服务端准备：{formatDate(demo.startDate)} 至 {formatDate(demo.endDate)}。准备不会创建练习。</p> : <p className="practice-field-hint">无模型也可手动完成方案、下单、推进和复盘。</p>}
        </div>
        <dl className="practice-hero-stats"><div><dt>练习记录</dt><dd>{sessions.length}</dd></div><div><dt>进行中</dt><dd>{runningCount}</dd></div><div><dt>已完成</dt><dd>{sessions.length - runningCount}</dd></div></dl>
      </section>
      {notice ? <Notice tone={notice.tone} onDismiss={onDismissNotice}>{notice.text}</Notice> : null}
      {optionsError ? <Notice tone="warn" title="数据选项暂不可用"><p>{optionsError}</p><button type="button" className="refresh-button" onClick={onReloadOptions}>重新读取数据选项</button></Notice> : null}
      {!options ? <Notice tone="neutral">正在读取真实数据范围与默认值。准备演示或后端选项不可用时，不会用假日期和证券替代。</Notice> : null}
      <div className="practice-entry-grid">
        <div className="practice-entry-plan">
          <NewPracticeForm options={options} demo={demo} demoBusy={demoBusy} creating={creating} plan={plan} planBusy={planBusy} setupDraft={setupDraft} onSetupDraftConsumed={onSetupDraftConsumed} onCreate={onCreate} onSavePlan={onSavePlan} onConfirmPlan={onConfirmPlan} onPrepareDemo={onPrepareDemo} />
        </div>
        <div className="practice-entry-agent"><AgentPanel planId={plan?.status === "draft" ? plan.plan_id : null} planVersion={plan?.status === "draft" ? plan.version : null} onSetupDraft={onApplySetupDraft} onGoControl={onGoControl} /></div>
      </div>
    </> : <>
      <header className="practice-session-header"><div><p className="eyebrow">练习记录</p><h1>你的练习</h1><p className="practice-hero-lead">打开记录可继续练习或查看真实复盘报告。</p></div><button type="button" className="refresh-button" onClick={onRefreshSessions} disabled={sessionsLoading}>{sessionsLoading ? "读取中…" : "刷新记录"}</button></header>
      {notice ? <Notice tone={notice.tone} onDismiss={onDismissNotice}>{notice.text}</Notice> : null}
      {sessionsError ? <Notice tone="error" title="读取练习记录失败">{sessionsError}</Notice> : null}
      {sessionsLoading && sessions.length === 0 ? <EmptyState title="正在读取练习记录…" /> : sessions.length === 0 ? <EmptyState title="还没有练习记录">返回首页，先准备本地演示或选择可用数据。</EmptyState> : <div className="practice-session-list">
        {sessions.map((session) => <article className="practice-session-row" key={session.practice_id}>
          <div className="practice-session-main"><div className="practice-session-title"><h2>{session.name}</h2><StatusBadge status={session.status} /></div><p className="muted">{summaryMeta(session)}</p><p className="muted">来源：{sourceLabel(session.source)}</p></div>
          <div className="practice-session-actions"><button type="button" className="practice-primary" onClick={() => onOpen(session.practice_id)}>{isFinished(session.status) ? "查看练习与复盘" : "继续练习"}</button><button type="button" className="link-button" disabled={Boolean(restartTarget)} onClick={() => setRestartTarget(session.practice_id)}>重新开始</button></div>
          {restartTarget === session.practice_id ? <div className="practice-confirm" role="group" aria-label="确认重新开始"><p>将依据该记录创建一个新的练习身份；这条记录与原报告会保留。</p><div className="practice-confirm-actions"><button type="button" className="practice-primary" onClick={() => { setRestartTarget(null); void onRestart(session.practice_id); }}>确认重新开始</button><button type="button" className="link-button" onClick={() => setRestartTarget(null)}>取消</button></div></div> : null}
        </article>)}
      </div>}
    </>}
  </div>;
}
