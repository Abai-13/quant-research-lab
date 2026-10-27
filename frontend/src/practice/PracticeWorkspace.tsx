import { useCallback, useEffect, useRef, useState } from "react";

import type {
  CreatePracticeInput,
  DemoPreparation,
  MarketSeries,
  OrderPreview,
  PracticeDecision,
  PracticeOptions,
  PracticeReport,
  PracticeSummary,
  PracticeView,
} from "./types";
import {
  PracticeApiError,
  advancePractice,
  createDecision,
  createPractice,
  fetchDecisions,
  fetchMarket,
  fetchOptions,
  fetchReport,
  fetchSession,
  fetchSessions,
  finishPractice,
  isRevisionConflict,
  placeOrder,
  prepareDemo,
  previewOrder,
  restartPractice,
} from "./client";
import { formatDate, isFinished } from "./format";
import { HomeView } from "./HomeView";
import { SessionView } from "./SessionView";
import { Notice, type Tone } from "./ui";
import { AgentPanel } from "./AgentPanel";

const LAST_SESSION_KEY = "practice-last-session";

function readPracticeIdFromUrl(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  const value = new URLSearchParams(window.location.search).get("practice");
  return value && value.trim() !== "" ? value.trim() : null;
}

function writePracticeIdToUrl(practiceId: string | null, addHistoryEntry = false): void {
  if (typeof window === "undefined") {
    return;
  }
  const url = new URL(window.location.href);
  const currentId = url.searchParams.get("practice");
  if (currentId === practiceId) return;
  if (practiceId) {
    url.searchParams.set("practice", practiceId);
  } else {
    url.searchParams.delete("practice");
  }
  if (addHistoryEntry) window.history.pushState({}, "", url);
  else window.history.replaceState({}, "", url);
}

function readLastSessionId(): string | null {
  try {
    return window.localStorage.getItem(LAST_SESSION_KEY);
  } catch {
    return null;
  }
}

function storeLastSessionId(practiceId: string | null): void {
  try {
    if (practiceId) {
      window.localStorage.setItem(LAST_SESSION_KEY, practiceId);
    } else {
      window.localStorage.removeItem(LAST_SESSION_KEY);
    }
  } catch {
    // 本地存储不可用时仅依赖 URL 恢复。
  }
}

function errorText(error: unknown): string {
  if (error instanceof PracticeApiError || error instanceof Error) {
    return error.message;
  }
  return "发生未知错误，请稍后重试。";
}

function summaryFromSession(session: PracticeView): PracticeSummary {
  return {
    practice_id: session.practice_id,
    name: session.name,
    status: session.status,
    start_date: session.start_date,
    end_date: session.end_date,
    revealed_date: session.revealed_date,
    next_trade_date: session.next_trade_date,
    source: session.source,
  };
}

function newGoalRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function PracticeWorkspace() {
  const [options, setOptions] = useState<PracticeOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<PracticeSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [demo, setDemo] = useState<DemoPreparation | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(() => readPracticeIdFromUrl());
  const [session, setSession] = useState<PracticeView | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [goalDraft, setGoalDraft] = useState<{ text: string; requestId: string } | null>(null);
  const [agentPanelOpen, setAgentPanelOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: Tone; text: string } | null>(null);
  const [market, setMarket] = useState<MarketSeries | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [report, setReport] = useState<PracticeReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<PracticeDecision[]>([]);
  const [lastSessionId, setLastSessionId] = useState<string | null>(() => readLastSessionId());
  const advanceRef = useRef<{ key: string; revision: number } | null>(null);
  const finishRef = useRef<{ key: string; revision: number } | null>(null);
  const restartRef = useRef<{ key: string; practiceId: string } | null>(null);

  const loadOptions = useCallback(async () => {
    setOptionsError(null);
    try {
      const result = await fetchOptions();
      setOptions(result);
    } catch (error) {
      setOptionsError(errorText(error));
    }
  }, []);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const result = await fetchSessions();
      setSessions(result.sessions);
    } catch (error) {
      setSessionsError(errorText(error));
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const loadDecisions = useCallback(async (practiceId: string) => {
    try {
      const result = await fetchDecisions(practiceId);
      setDecisions(result);
    } catch {
      setDecisions([]);
    }
  }, []);

  const loadReport = useCallback(async (practiceId: string) => {
    setReportLoading(true);
    setReportError(null);
    try {
      const result = await fetchReport(practiceId);
      setReport(result);
    } catch (error) {
      setReportError(errorText(error));
    } finally {
      setReportLoading(false);
    }
  }, []);

  const loadMarket = useCallback(async (practiceId: string, code: string) => {
    setMarketLoading(true);
    setMarketError(null);
    try {
      const result = await fetchMarket(practiceId, code);
      setMarket(result);
    } catch (error) {
      setMarketError(errorText(error));
    } finally {
      setMarketLoading(false);
    }
  }, []);

  const applySession = useCallback((next: PracticeView, addHistoryEntry = false) => {
    setSession(next);
    setSessionId(next.practice_id);
    setLastSessionId(next.practice_id);
    storeLastSessionId(next.practice_id);
    writePracticeIdToUrl(next.practice_id, addHistoryEntry);
    setSessions((current) => {
      const summary = summaryFromSession(next);
      const existing = current.findIndex((item) => item.practice_id === next.practice_id);
      if (existing === -1) {
        return [summary, ...current];
      }
      const copy = [...current];
      copy[existing] = summary;
      return copy;
    });
  }, []);

  const loadSession = useCallback(
    async (practiceId: string, keepMarket = false, silent = false): Promise<boolean> => {
      if (!silent) {
        setSessionLoading(true);
      }
      setSessionError(null);
      try {
        const next = await fetchSession(practiceId);
        applySession(next);
        void loadDecisions(practiceId);
        if (isFinished(next.status)) {
          void loadReport(practiceId);
        } else {
          setReport(null);
          setReportError(null);
        }
        if (!keepMarket) {
          const code =
            next.user_state.positions[0]?.ts_code ?? next.user_state.orders[0]?.ts_code ?? null;
          if (code) {
            void loadMarket(practiceId, code);
          } else {
            setMarket(null);
          }
        }
        return true;
      } catch (error) {
        setSessionError(errorText(error));
        return false;
      } finally {
        if (!silent) {
          setSessionLoading(false);
        }
      }
    },
    [applySession, loadDecisions, loadMarket, loadReport],
  );

  useEffect(() => {
    void loadOptions();
    void loadSessions();
  }, [loadOptions, loadSessions]);

  useEffect(() => {
    const initial = readPracticeIdFromUrl();
    if (initial) {
      setSessionId(initial);
      void loadSession(initial);
    }
  }, [loadSession]);

  useEffect(() => {
    const onPopState = () => {
      const nextId = readPracticeIdFromUrl();
      setSessionId(nextId);
      setSession(null);
      setSessionError(null);
      setReport(null);
      setMarket(null);
      setDecisions([]);
      if (nextId) void loadSession(nextId);
      else void loadSessions();
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [loadSession, loadSessions]);

  async function handleCreate(input: CreatePracticeInput) {
    setCreating(true);
    setNotice(null);
    setGoalDraft(null);
    setAgentPanelOpen(false);
    try {
      const created = await createPractice(input);
      applySession(created, true);
      setReport(null);
      setDecisions([]);
      setMarket(null);
      setNotice({ tone: "ok", text: "模拟已创建。请查看已揭晓行情，自主下单后揭晓下一交易日。" });
      void loadSessions();
    } catch (error) {
      setNotice({ tone: "error", text: errorText(error) });
      throw error;
    } finally {
      setCreating(false);
    }
  }

  async function handlePrepareDemo() {
    setDemoBusy(true);
    setNotice(null);
    try {
      const prepared = await prepareDemo();
      setDemo(prepared);
      if (prepared.options) {
        setOptions(prepared.options);
      }
    } catch (error) {
      setNotice({ tone: "error", text: errorText(error) });
    } finally {
      setDemoBusy(false);
    }
  }

  function openSession(practiceId: string) {
    setGoalDraft(null);
    setAgentPanelOpen(false);
    setSessionId(practiceId);
    setSession(null);
    setSessionError(null);
    setNotice(null);
    setMarket(null);
    setReport(null);
    setDecisions([]);
    writePracticeIdToUrl(practiceId, true);
    void loadSession(practiceId);
  }

  function backToHome() {
    setGoalDraft(null);
    setAgentPanelOpen(false);
    setSessionId(null);
    setSession(null);
    setSessionError(null);
    setNotice(null);
    setMarket(null);
    setReport(null);
    setDecisions([]);
    writePracticeIdToUrl(null, true);
    void loadSessions();
  }

  async function handleRefresh() {
    if (!session) {
      return;
    }
    setBusy("refresh");
    setNotice(null);
    const ok = await loadSession(session.practice_id, true, true);
    if (!ok) {
      setNotice({ tone: "error", text: "刷新失败，当前显示的可能是上一次读取的进度。" });
    }
    setBusy(null);
  }

  async function handlePreview(input: {
    ts_code: string;
    direction: string;
    planned_shares: number;
  }): Promise<OrderPreview> {
    if (!session) {
      throw new Error("模拟尚未加载完成。");
    }
    return previewOrder(session.practice_id, input);
  }

  async function handleSubmitOrder(input: {
    ts_code: string;
    direction: string;
    planned_shares: number;
    key: string;
  }) {
    if (!session) {
      throw new Error("模拟尚未加载完成。");
    }
    setBusy("order");
    setNotice(null);
    try {
      const next = await placeOrder(session.practice_id, {
        ts_code: input.ts_code,
        direction: input.direction,
        planned_shares: input.planned_shares,
        idempotency_key: input.key,
        expected_revision: session.revision,
      });
      applySession(next);
      setNotice({
        tone: "ok",
        text: "订单已登记，将在下一可执行日按当时价格尝试成交；尚未推进日期。",
      });
      void loadSessions();
    } catch (error) {
      if (isRevisionConflict(error)) {
        setNotice({ tone: "error", text: "页面状态已过期，已重新读取最新进度；请确认后重试。" });
        void loadSession(session.practice_id, true, true);
      } else {
        setNotice({ tone: "error", text: errorText(error) });
      }
      throw error;
    } finally {
      setBusy(null);
    }
  }

  async function handleAdvance() {
    if (!session) {
      return;
    }
    if (advanceRef.current?.revision !== session.revision) {
      advanceRef.current = { key: crypto.randomUUID(), revision: session.revision };
    }
    const key = advanceRef.current.key;
    setBusy("advance");
    setNotice(null);
    try {
      const next = await advancePractice(session.practice_id, key, session.revision);
      applySession(next);
      advanceRef.current = null;
      setNotice({
        tone: "ok",
        text: `已揭晓至 ${formatDate(next.revealed_date)}。可以继续下单或再次揭晓下一交易日。`,
      });
      void loadSessions();
      if (market) {
        void loadMarket(next.practice_id, market.code);
      }
    } catch (error) {
      if (isRevisionConflict(error)) {
        advanceRef.current = null;
        setNotice({ tone: "error", text: "页面状态已过期，已重新读取最新进度；请确认后重试。" });
        void loadSession(session.practice_id, true, true);
      } else {
        setNotice({ tone: "error", text: errorText(error) });
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleFinish() {
    if (!session) {
      return;
    }
    if (finishRef.current?.revision !== session.revision) {
      finishRef.current = { key: crypto.randomUUID(), revision: session.revision };
    }
    const key = finishRef.current.key;
    setBusy("finish");
    setNotice(null);
    try {
      const result = await finishPractice(session.practice_id, key, session.revision);
      applySession(result.session);
      finishRef.current = null;
      if (result.report) {
        setReport(result.report);
        setReportError(null);
      } else {
        void loadReport(result.session.practice_id);
      }
      setNotice({ tone: "ok", text: "模拟已结束，复盘报告已生成；旧记录保持可读。" });
      void loadSessions();
    } catch (error) {
      if (isRevisionConflict(error)) {
        finishRef.current = null;
        setNotice({ tone: "error", text: "页面状态已过期，已重新读取最新进度；请确认后重试。" });
        void loadSession(session.practice_id, true, true);
      } else {
        setNotice({ tone: "error", text: errorText(error) });
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleRestart(targetId: string) {
    setGoalDraft(null);
    setAgentPanelOpen(false);
    if (restartRef.current?.practiceId !== targetId) {
      restartRef.current = { key: crypto.randomUUID(), practiceId: targetId };
    }
    const key = restartRef.current.key;
    setBusy("restart");
    setNotice(null);
    try {
      const created = await restartPractice(targetId, key);
      restartRef.current = null;
      applySession(created, true);
      setReport(null);
      setReportError(null);
      setDecisions([]);
      setMarket(null);
      setNotice({ tone: "ok", text: "已创建全新的模拟身份，旧模拟与报告仍然保留。" });
      void loadSessions();
      void loadDecisions(created.practice_id);
      const code =
        created.user_state.positions[0]?.ts_code ?? created.user_state.orders[0]?.ts_code ?? null;
      if (code) {
        void loadMarket(created.practice_id, code);
      }
    } catch (error) {
      setNotice({ tone: "error", text: errorText(error) });
    } finally {
      setBusy(null);
    }
  }

  async function handleAddDecision(input: {
    judgment: string;
    evidence: string;
    exit_condition: string;
    decision_date?: string;
    key: string;
  }) {
    if (!session) {
      throw new Error("模拟尚未加载完成。");
    }
    setBusy("decision");
    try {
      await createDecision(session.practice_id, {
        judgment: input.judgment,
        evidence: input.evidence,
        exit_condition: input.exit_condition,
        decision_date: input.decision_date,
        idempotency_key: input.key,
      });
      await loadDecisions(session.practice_id);
      const refreshed = await fetchSession(session.practice_id);
      applySession(refreshed);
    } catch (error) {
      if (isRevisionConflict(error)) {
        setNotice({ tone: "error", text: "页面状态已过期，已重新读取最新进度；请确认后重试。" });
        void loadSession(session.practice_id, true, true);
      }
      throw error;
    } finally {
      setBusy(null);
    }
  }

  function handleOpenMarket(code: string) {
    if (!session) {
      return;
    }
    void loadMarket(session.practice_id, code);
  }

  function handleSendResearchQuestion(question: string) {
    const text = question.trim();
    if (!text) return;
    setGoalDraft({ text, requestId: newGoalRequestId() });
    setAgentPanelOpen(true);
    window.setTimeout(() => {
      const panel = document.getElementById("practice-agent-panel");
      panel?.scrollIntoView({ behavior: "smooth", block: "start" });
      panel?.querySelector<HTMLElement>("summary")?.focus();
    }, 0);
  }

  if (sessionId && (sessionLoading || (!session && !sessionError))) {
    return (
      <div className="practice-home">
        <div className="empty-state">
          <p className="empty-title">正在从服务端恢复模拟…</p>
          <p>刷新页面时会按地址中的模拟标识重新读取进度，不依赖浏览器内存。</p>
        </div>
      </div>
    );
  }

  if (sessionId && sessionError && !session) {
    return (
      <div className="practice-home">
        <Notice tone="error" title="无法打开模拟">
          {sessionError}
        </Notice>
        <div className="practice-form-actions">
          <button type="button" className="refresh-button" onClick={() => void loadSession(sessionId)}>
            重试
          </button>
          <button type="button" className="link-button" onClick={backToHome}>
            返回模拟首页
          </button>
        </div>
      </div>
    );
  }

  if (session) {
    return (
      <div className="practice-home">
        <details
          className="practice-agent-drawer"
          id="practice-agent-panel"
          open={agentPanelOpen}
          onToggle={(event) => setAgentPanelOpen(event.currentTarget.open)}
        >
          <summary>Agent 查证与任务 <span>按需展开，查看真实步骤、依据和草稿</span></summary>
          <AgentPanel
            session={session}
            onPracticeRefresh={() => void loadSession(session.practice_id, true, true)}
            goalDraft={goalDraft}
          />
        </details>
        <SessionView
          key={session.practice_id}
          session={session}
          options={options}
          market={market}
          marketLoading={marketLoading}
          marketError={marketError}
          report={report}
          reportLoading={reportLoading}
          reportError={reportError}
          decisions={decisions}
          busy={busy}
          notice={notice}
          onDismissNotice={() => setNotice(null)}
          onBack={backToHome}
          onRefresh={() => void handleRefresh()}
          onOpenMarket={handleOpenMarket}
          onPreview={handlePreview}
          onSubmitOrder={handleSubmitOrder}
          onAdvance={handleAdvance}
          onFinish={handleFinish}
          onRestart={() => handleRestart(session.practice_id)}
          onAddDecision={handleAddDecision}
          onSendResearchQuestion={handleSendResearchQuestion}
          onRetryReport={() => void loadReport(session.practice_id)}
        />
      </div>
    );
  }

  return (
    <HomeView
      options={options}
      optionsError={optionsError}
      sessions={sessions}
      sessionsLoading={sessionsLoading}
      sessionsError={sessionsError}
      demo={demo}
      demoBusy={demoBusy}
      creating={creating}
      busy={Boolean(busy)}
      lastSessionId={lastSessionId}
      notice={notice}
      onDismissNotice={() => setNotice(null)}
      onCreate={handleCreate}
      onPrepareDemo={handlePrepareDemo}
      onOpen={openSession}
      onRestart={handleRestart}
      onRefreshSessions={() => void loadSessions()}
      onReloadOptions={() => void loadOptions()}
    />
  );
}
