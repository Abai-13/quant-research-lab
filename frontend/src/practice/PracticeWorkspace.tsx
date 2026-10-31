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
  PracticePlan,
  PlanPayload,
  PracticeSessionWorkspace,
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
  fetchPracticePlan,
  fetchSessionWorkspace,
  fetchSession,
  fetchSessions,
  finishPractice,
  isRevisionConflict,
  placeOrder,
  prepareDemo,
  previewOrder,
  restartPractice,
  savePracticePlan,
  updatePracticePlan,
  confirmPracticePlan,
} from "./client";
import { formatDate, isFinished } from "./format";
import { HomeView } from "./HomeView";
import { SessionView } from "./SessionView";
import { Notice, type Tone } from "./ui";
import { AgentPanel } from "./AgentPanel";

const LAST_SESSION_KEY = "practice-last-session";
const LAST_PLAN_KEY = "practice-last-plan";
export type PracticeSection = "home" | "room" | "records";

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

function readLastPlanId(): string | null { try { return window.localStorage.getItem(LAST_PLAN_KEY); } catch { return null; } }
function storeLastPlanId(id: string | null) { try { if (id) window.localStorage.setItem(LAST_PLAN_KEY, id); else window.localStorage.removeItem(LAST_PLAN_KEY); } catch { /* URL and server remain authoritative */ } }

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
function normalizePlanDate(value: string): string | null {
  const digits = /^\d{8}$/.test(value) ? value : /^\d{4}-\d{2}-\d{2}$/.test(value) ? value.replace(/-/g, "") : null;
  if (!digits) return null;
  const year = Number(digits.slice(0, 4));
  const month = Number(digits.slice(4, 6));
  const day = Number(digits.slice(6, 8));
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > 31) return null;
  const date = new Date(0);
  date.setUTCHours(0, 0, 0, 0);
  date.setUTCFullYear(year, month - 1, day);
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? digits : null;
}

export function PracticeWorkspace({ section = "home", onNavigate, onGoControl, setupDraft = null, onSetupDraftConsumed }: { section?: PracticeSection; onNavigate?: (section: PracticeSection) => void; onGoControl?: () => void; setupDraft?: Record<string, unknown> | null; onSetupDraftConsumed?: () => void }) {
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
  const [practicePlan, setPracticePlan] = useState<PracticePlan | null>(null);
  const [planBusy, setPlanBusy] = useState(() => Boolean(readLastPlanId()));
  const [workspace, setWorkspace] = useState<PracticeSessionWorkspace | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [agentStatus, setAgentStatus] = useState("正在读取 Agent 状态");
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [goalDraft, setGoalDraft] = useState<{ text: string; requestId: string } | null>(null);
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
  const confirmPlanKey = useRef<{ signature: string; key: string } | null>(null);
  const setupDraftKey = useRef<{ signature: string; key: string } | null>(null);
  const manualOrderNotice = useRef<{ practiceId: string; revision: number; revealedDate: string | null } | null>(null);
  const sessionRequestSeq = useRef(0);
  const workspaceRequestSeq = useRef(0);
  const decisionsRequestSeq = useRef(0);
  const reportRequestSeq = useRef(0);
  const marketRequestSeq = useRef(0);
  const planRequestSeq = useRef(0);
  const activeSessionId = useRef<string | null>(readPracticeIdFromUrl());

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
    const requestSeq = ++decisionsRequestSeq.current;
    try {
      const result = await fetchDecisions(practiceId);
      if (requestSeq === decisionsRequestSeq.current && activeSessionId.current === practiceId) setDecisions(result);
    } catch {
      if (requestSeq === decisionsRequestSeq.current && activeSessionId.current === practiceId) setDecisions([]);
    }
  }, []);

  const loadReport = useCallback(async (practiceId: string) => {
    const requestSeq = ++reportRequestSeq.current;
    setReportLoading(true);
    setReportError(null);
    try {
      const result = await fetchReport(practiceId);
      if (requestSeq === reportRequestSeq.current && activeSessionId.current === practiceId) setReport(result);
    } catch (error) {
      if (requestSeq === reportRequestSeq.current && activeSessionId.current === practiceId) setReportError(errorText(error));
    } finally {
      if (requestSeq === reportRequestSeq.current && activeSessionId.current === practiceId) setReportLoading(false);
    }
  }, []);

  const loadMarket = useCallback(async (practiceId: string, code: string) => {
    const requestSeq = ++marketRequestSeq.current;
    setMarketLoading(true);
    setMarketError(null);
    try {
      const result = await fetchMarket(practiceId, code);
      if (requestSeq === marketRequestSeq.current && activeSessionId.current === practiceId) setMarket(result);
    } catch (error) {
      if (requestSeq === marketRequestSeq.current && activeSessionId.current === practiceId) setMarketError(errorText(error));
    } finally {
      if (requestSeq === marketRequestSeq.current && activeSessionId.current === practiceId) setMarketLoading(false);
    }
  }, []);

  const applySession = useCallback((next: PracticeView, addHistoryEntry = false) => {
    const pendingOrderNotice = manualOrderNotice.current;
    if (pendingOrderNotice && (pendingOrderNotice.practiceId !== next.practice_id || pendingOrderNotice.revision !== next.revision || pendingOrderNotice.revealedDate !== next.revealed_date)) {
      manualOrderNotice.current = null;
      setNotice((current) => current?.text === "订单结果已从服务端更新；请查看订单列表中的成交状态和成交日期。" ? null : current);
    }
    activeSessionId.current = next.practice_id;
    decisionsRequestSeq.current += 1;
    reportRequestSeq.current += 1;
    marketRequestSeq.current += 1;
    setSession(next);
    setSessionId(next.practice_id);
    setLastSessionId(next.practice_id);
    storeLastSessionId(next.practice_id);
    writePracticeIdToUrl(next.practice_id, addHistoryEntry);
    const workspaceSeq = ++workspaceRequestSeq.current;
    setWorkspaceError(null);
    void fetchSessionWorkspace(next.practice_id).then((result) => {
      if (workspaceSeq !== workspaceRequestSeq.current || result.practice_id !== next.practice_id) return;
      if (result.revision !== next.revision) {
        setWorkspace(null);
        setWorkspaceError("工作区修订与当前练习状态不同；请刷新后再查看证券明细。");
        return;
      }
      setWorkspace(result);
    }).catch((error) => {
      if (workspaceSeq === workspaceRequestSeq.current) { setWorkspace(null); setWorkspaceError(errorText(error)); }
    });
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
    void loadDecisions(next.practice_id);
  }, [loadDecisions]);

  const loadSession = useCallback(
    async (practiceId: string, keepMarket = false, silent = false): Promise<boolean> => {
      activeSessionId.current = practiceId;
      if (!silent) {
        setSessionLoading(true);
      }
      const requestSeq = ++sessionRequestSeq.current;
      setSessionError(null);
      try {
        const next = await fetchSession(practiceId);
        if (requestSeq !== sessionRequestSeq.current) return false;
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
        if (requestSeq !== sessionRequestSeq.current) return false;
        setSessionError(errorText(error));
        return false;
      } finally {
        if (!silent && requestSeq === sessionRequestSeq.current) {
          setSessionLoading(false);
        }
      }
    },
    [applySession, loadDecisions, loadMarket, loadReport],
  );

  useEffect(() => {
    const requestSeq = ++planRequestSeq.current;
    void loadOptions();
    void loadSessions();
    const planId = readLastPlanId();
    if (planId) {
      void fetchPracticePlan(planId).then((loaded) => {
        if (requestSeq !== planRequestSeq.current) return;
        setPracticePlan(loaded);
        if (loaded.status === "confirmed" && loaded.practice_id && !readPracticeIdFromUrl()) {
          setSessionId(loaded.practice_id);
          writePracticeIdToUrl(loaded.practice_id);
          void loadSession(loaded.practice_id);
        }
      }).catch((error) => { if (requestSeq === planRequestSeq.current) setNotice({ tone: "warn", text: `恢复已保存方案失败：${errorText(error)}。不会自动另建方案。` }); }).finally(() => { if (requestSeq === planRequestSeq.current) setPlanBusy(false); });
    }
    return () => { planRequestSeq.current += 1; };
  }, [loadOptions, loadSessions, loadSession]);

  useEffect(() => {
    const initial = readPracticeIdFromUrl();
    if (initial) {
      setSessionId(initial);
      void loadSession(initial);
    }
  }, [loadSession]);

  useEffect(() => {
    const onPopState = () => {
      sessionRequestSeq.current += 1;
      workspaceRequestSeq.current += 1;
      const nextId = readPracticeIdFromUrl();
      activeSessionId.current = nextId;
      decisionsRequestSeq.current += 1; reportRequestSeq.current += 1; marketRequestSeq.current += 1;
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

  async function handleSavePlan(payload: PlanPayload, symbols: string[], goal: string, key: string) {
    setPlanBusy(true);
    setNotice(null);
    try {
      const saved = practicePlan?.status === "draft"
        ? await updatePracticePlan(practicePlan.plan_id, { expected_version: practicePlan.version, payload, symbols, goal, idempotency_key: key })
        : await savePracticePlan({ payload, symbols, goal, idempotency_key: key });
      setPracticePlan(saved);
      storeLastPlanId(saved.plan_id);
      confirmPlanKey.current = null;
      setNotice({ tone: saved.qualification.ready ? "ok" : "warn", text: saved.qualification.ready ? `方案已保存为 v${saved.version}，资格检查通过；尚未创建练习。` : `方案已保存为 v${saved.version}，请查看资格问题并修改后重新检查。` });
    } catch (error) {
      setNotice({ tone: "error", text: errorText(error) });
      if (error instanceof PracticeApiError && error.status === 409) setNotice({ tone: "error", text: "方案版本已变化；保留了当前输入，请刷新方案后人工核对再保存。" });
    } finally { setPlanBusy(false); }
  }

  async function handleConfirmPlan() {
    if (!practicePlan || practicePlan.status !== "draft" || !practicePlan.qualification.ready || planBusy) return;
    const signature = `${practicePlan.plan_id}:${practicePlan.version}`;
    if (confirmPlanKey.current?.signature !== signature) confirmPlanKey.current = { signature, key: newGoalRequestId() };
    setPlanBusy(true); setCreating(true); setNotice(null);
    try {
      const result = await confirmPracticePlan(practicePlan.plan_id, practicePlan.version, confirmPlanKey.current.key);
      setPracticePlan(result.plan); storeLastPlanId(result.plan.plan_id); confirmPlanKey.current = null;
      applySession(result.session, true); setReport(null); setDecisions(result.session.decisions); setMarket(null);
      onNavigate?.("room");
      setNotice({ tone: "ok", text: "已按服务端资格结果创建练习。" }); void loadSessions();
    } catch (error) {
      setNotice({ tone: "error", text: errorText(error) });
      if (error instanceof PracticeApiError && error.status === 409) setNotice({ tone: "error", text: "方案版本已变化；未覆盖当前表单，请重新读取方案并核对后确认。" });
    } finally { setPlanBusy(false); setCreating(false); }
  }

  async function handleSetupDraft(payload: Record<string, unknown>) {
    if (!practicePlan || practicePlan.status !== "draft") {
      setNotice({ tone: "warn", text: "请先保存当前方案，再应用 Agent 草稿；草稿不会另建一份隐藏方案。" });
      return;
    }
    const allowedSymbols = new Set(options?.symbols.map((item) => item.ts_code) ?? []);
    const base = practicePlan.payload;
    const next: PlanPayload = { ...base };
    let nextSymbols = practicePlan.symbols;
    if (Object.prototype.hasOwnProperty.call(payload, "name") && (typeof payload.name !== "string" || !payload.name.trim() || payload.name.trim().length > 80)) {
      setNotice({ tone: "warn", text: "草稿名称无效，原样保留；请手动修正后再保存。" }); return;
    }
    if (typeof payload.name === "string") next.name = payload.name.trim();
    for (const key of ["start_date", "end_date"] as const) {
      if (Object.prototype.hasOwnProperty.call(payload, key) && (typeof payload[key] !== "string" || !normalizePlanDate(payload[key] as string))) {
        setNotice({ tone: "warn", text: "草稿日期无效，未应用任何草稿字段；原方案保持不变。" }); return;
      }
      if (typeof payload[key] === "string") next[key] = normalizePlanDate(payload[key] as string)!;
    }
    const normalizedStart = normalizePlanDate(next.start_date);
    const normalizedEnd = normalizePlanDate(next.end_date);
    if (!normalizedStart || !normalizedEnd || normalizedStart > normalizedEnd) { setNotice({ tone: "warn", text: "草稿开始和结束日期无效，未应用；原方案保持不变。" }); return; }
    next.start_date = normalizedStart;
    next.end_date = normalizedEnd;
    const draftCash = payload.cash_cents ?? payload.initial_cash_cents;
    if ((Object.prototype.hasOwnProperty.call(payload, "cash_cents") || Object.prototype.hasOwnProperty.call(payload, "initial_cash_cents")) && (typeof draftCash !== "number" || !Number.isSafeInteger(draftCash) || draftCash < 0)) {
      setNotice({ tone: "warn", text: "草稿起始现金无效，未应用；原方案保持不变。" }); return;
    }
    if (typeof draftCash === "number") next.cash_cents = draftCash;
    if (Object.prototype.hasOwnProperty.call(payload, "positions")) {
      if (!Array.isArray(payload.positions) || !payload.positions.every((raw) => {
        if (!raw || typeof raw !== "object") return false;
        const position = raw as Record<string, unknown>;
        const acquireDate = position.acquire_date;
        const normalizedAcquireDate = acquireDate === undefined || acquireDate === null || acquireDate === "" ? next.start_date : typeof acquireDate === "string" ? normalizePlanDate(acquireDate) : null;
        return typeof position.ts_code === "string" && allowedSymbols.has(position.ts_code) && Number.isSafeInteger(position.shares) && Number(position.shares) > 0 && (position.total_cost_cents === undefined || position.total_cost_cents === null || (Number.isSafeInteger(position.total_cost_cents) && Number(position.total_cost_cents) >= 0)) && (position.sellable_shares === undefined || position.sellable_shares === null || (Number.isSafeInteger(position.sellable_shares) && Number(position.sellable_shares) >= 0 && Number(position.sellable_shares) <= Number(position.shares))) && normalizedAcquireDate !== null && normalizedAcquireDate <= next.start_date;
      })) { setNotice({ tone: "warn", text: "草稿持仓包含无效或当前版本不支持的值，未应用；原方案保持不变。" }); return; }
      const positionCodes = payload.positions.map((item) => (item as Record<string, unknown>).ts_code as string);
      if (new Set(positionCodes).size !== positionCodes.length) { setNotice({ tone: "warn", text: "草稿持仓包含重复证券，未应用；原方案保持不变。" }); return; }
      next.positions = payload.positions.map((item) => {
        const position = item as Record<string, unknown>;
        const shares = position.shares as number;
        const acquireDate = position.acquire_date === undefined || position.acquire_date === null || position.acquire_date === "" ? next.start_date : normalizePlanDate(position.acquire_date as string)!;
        return { ts_code: position.ts_code as string, shares, total_cost_cents: typeof position.total_cost_cents === "number" ? position.total_cost_cents : null, sellable_shares: typeof position.sellable_shares === "number" ? position.sellable_shares : shares, acquire_date: acquireDate };
      });
    }
    if (Object.prototype.hasOwnProperty.call(payload, "symbols")) {
      if (!Array.isArray(payload.symbols) || !payload.symbols.every((item) => typeof item === "string" && allowedSymbols.has(item)) || payload.symbols.length < 1 || payload.symbols.length > 20 || new Set(payload.symbols).size !== payload.symbols.length) {
        setNotice({ tone: "warn", text: "草稿关注证券无效或超出当前数据版本范围，未应用；原方案保持不变。" }); return;
      }
      nextSymbols = payload.symbols as string[];
    }
    if (nextSymbols !== practicePlan.symbols) next.settings = { ...base.settings, watchlist_symbols: nextSymbols };
    const signature = JSON.stringify({ plan_id: practicePlan.plan_id, version: practicePlan.version, next, symbols: nextSymbols, goal: practicePlan.goal });
    if (setupDraftKey.current?.signature !== signature) setupDraftKey.current = { signature, key: newGoalRequestId() };
    setPlanBusy(true); setNotice(null);
    try {
      const updated = await updatePracticePlan(practicePlan.plan_id, { expected_version: practicePlan.version, payload: next, symbols: nextSymbols, goal: practicePlan.goal, idempotency_key: setupDraftKey.current.key });
      setupDraftKey.current = null;
      setPracticePlan(updated); setNotice({ tone: "ok", text: `Agent 草稿已写入同一方案 v${updated.version}；请检查并再次确认资格，不会自动创建练习。` });
    } catch (error) { setNotice({ tone: "error", text: errorText(error) }); }
    finally { setPlanBusy(false); }
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
    activeSessionId.current = practiceId;
    setGoalDraft(null);
    setSessionId(practiceId);
    setSession(null);
    setSessionError(null);
    setNotice(null);
    setMarket(null);
    setReport(null);
    setDecisions([]);
    writePracticeIdToUrl(practiceId, true);
    onNavigate?.("room");
    void loadSession(practiceId);
  }

  function backToHome() {
    activeSessionId.current = null;
    sessionRequestSeq.current += 1;
    workspaceRequestSeq.current += 1;
    decisionsRequestSeq.current += 1; reportRequestSeq.current += 1; marketRequestSeq.current += 1;
    setWorkspace(null);
    setWorkspaceError(null);
    setGoalDraft(null);
    setSessionId(null);
    setSession(null);
    setSessionError(null);
    setNotice(null);
    setMarket(null);
    setReport(null);
    setDecisions([]);
    writePracticeIdToUrl(null, true);
    onNavigate?.("home");
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
      manualOrderNotice.current = { practiceId: next.practice_id, revision: next.revision, revealedDate: next.revealed_date };
      setNotice({
        tone: "ok",
        text: "订单结果已从服务端更新；请查看订单列表中的成交状态和成交日期。",
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
    window.setTimeout(() => {
      const panel = document.getElementById("session-agent-panel");
      panel?.scrollIntoView({ behavior: "smooth", block: "start" });
      panel?.querySelector<HTMLElement>("textarea")?.focus();
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

  if (section === "room" && !session) {
    return <div className="practice-home"><div className="empty-state"><p className="empty-title">练习室还没有打开一段练习</p><p>选择一条记录继续，或回首页准备数据并创建练习。</p><div className="practice-form-actions">{sessions[0] ? <button type="button" className="practice-primary" onClick={() => openSession(sessions[0].practice_id)}>继续最近练习</button> : null}<button type="button" className="refresh-button" onClick={() => onNavigate?.("home")}>回首页开始</button></div></div></div>;
  }

  if (session && section === "room") {
    return (
      <div className="practice-home">
        <SessionView
          key={session.practice_id}
          session={session}
          workspace={workspace?.practice_id === session.practice_id ? workspace : null}
          workspaceError={workspaceError}
      agentPanel={<div id="session-agent-panel"><AgentPanel session={session} planId={practicePlan?.practice_id === session.practice_id ? practicePlan.plan_id : null} planVersion={practicePlan?.practice_id === session.practice_id ? practicePlan.version : null} onGoControl={onGoControl} onStatus={setAgentStatus} onSetupDraft={(payload) => void handleSetupDraft(payload)} onPracticeRefresh={() => void loadSession(session.practice_id, true, true)} goalDraft={goalDraft} /></div>}
          agentStatus={agentStatus}
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
      section={section === "room" ? "home" : section}
      options={options}
      optionsError={optionsError}
      sessions={sessions}
      sessionsLoading={sessionsLoading}
      sessionsError={sessionsError}
      lastSessionId={lastSessionId}
      demo={demo}
      demoBusy={demoBusy}
      creating={creating}
      plan={practicePlan}
      planBusy={planBusy}
      onApplySetupDraft={(payload) => void handleSetupDraft(payload)}
      notice={notice}
      onDismissNotice={() => setNotice(null)}
      onCreate={handleCreate}
      onSavePlan={handleSavePlan}
      onConfirmPlan={handleConfirmPlan}
      onPrepareDemo={handlePrepareDemo}
      onOpen={openSession}
      onRestart={handleRestart}
      onRefreshSessions={() => void loadSessions()}
      onReloadOptions={() => void loadOptions()}
      onNavigate={onNavigate}
      onGoControl={onGoControl}
      setupDraft={setupDraft}
      onSetupDraftConsumed={onSetupDraftConsumed}
    />
  );
}
