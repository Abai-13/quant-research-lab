import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { AgentConfigResponse, AgentDisclosure, AgentDraft, AgentEvent, AgentLimits, AgentTask } from "./agentTypes";
import { cancelAgentTask, createAgentTask, deleteAgentTask, getAgentConfig, getAgentEvents, getAgentTask, getAgentTasks, getDisclosure, resumeAgentTask, saveAgentConfig, testAgentConfig } from "./agentClient";
import type { PracticeView, OrderPreview } from "./types";
import { fetchPracticePlan, fetchSession, placeOrder, previewOrder } from "./client";
import { formatBps, formatCents, formatDateShort, formatShares, isRunning } from "./format";
import { Notice } from "./ui";
import "../agent-research.css";

const DEFAULT_LIMITS: AgentLimits = { rounds: 8, tool_calls: 20, timeout_seconds: 120, tokens: 4096 };
const HARD_LIMITS: AgentLimits = { rounds: 8, tool_calls: 20, timeout_seconds: 120, tokens: 8192 };
const ACTIVE = new Set(["queued", "running"]);
const STATUS: Record<string, string> = { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
const KIND: Record<string, string> = { task_started: "任务开始", model_started: "模型调用", tool_started: "工具开始", tool_completed: "工具完成", task_completed: "任务完成", task_failed: "任务失败", task_cancelled: "任务取消", task_interrupted: "任务中断" };
type SessionContext = Pick<PracticeView, "practice_id" | "revision" | "revealed_date" | "source"> & { name: string; status: string };

function fmt(value: number | null | undefined, unit = "") { return value === null || value === undefined ? "未知" : `${value.toLocaleString()}${unit}`; }
function stringify(value: unknown) { if (value === null || value === undefined) return "—"; if (typeof value === "string") return value; try { return JSON.stringify(value); } catch { return "不可展示"; } }
function evidenceValue(key: string, value: unknown) {
  if (value === null || value === undefined) return "未知";
  if (typeof value === "number" && key.endsWith("_cents")) return `${formatCents(value)} 元`;
  if (typeof value === "number" && key.endsWith("_bps")) return formatBps(value);
  if (typeof value === "number" && key.includes("shares")) return `${formatShares(value)} 股`;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string" && (key === "as_of" || key.endsWith("_date"))) return formatDateShort(value);
  return stringify(value);
}
function newKey() { return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`; }
function isTradePayload(value: Record<string, unknown>) { return typeof value.ts_code === "string" && (value.direction === "buy" || value.direction === "sell") && Number.isInteger(value.planned_shares); }
function contextValue(context: Record<string, unknown> | null | undefined, key: string): unknown { return context?.[key]; }
function previewMessages(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      if (typeof record.detail === "string") return record.detail;
      if (typeof record.message === "string") return record.message;
      if (typeof record.label === "string") return record.label;
    }
    return stringify(item);
  }).filter(Boolean);
}
function firstFiniteNumber(...values: unknown[]): number | null {
  return values.find((value): value is number => typeof value === "number" && Number.isFinite(value)) ?? null;
}
function firstNonEmptyString(...values: unknown[]): string | null {
  return values.find((value): value is string => typeof value === "string" && value.trim() !== "") ?? null;
}

export function AgentPanel({ session = null, planId = null, planVersion = null, onPracticeRefresh, onSetupDraft, onStatus, onGoControl, settingsOnly = false, goalDraft = null }: { session?: SessionContext | null; planId?: string | null; planVersion?: number | null; onPracticeRefresh?: () => void; onSetupDraft?: (payload: Record<string, unknown>) => void; onStatus?: (status: string) => void; onGoControl?: () => void; settingsOnly?: boolean; goalDraft?: { text: string; requestId: string } | null }) {
  const practiceId = session?.practice_id ?? null;
  const scope = practiceId ? "simulation" : "setup";
  const sessionContextKey = session ? [session.practice_id, session.revision, session.revealed_date ?? "", session.source.version_id ?? "", planId ?? "", planVersion ?? ""].join(":") : `setup:${planId ?? "none"}:${planVersion ?? "none"}`;
  const [configState, setConfigState] = useState<AgentConfigResponse | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [draftConfig, setDraftConfig] = useState({ name: "", protocol: "chat_completions", base_url: "", model: "", allow_loopback_http: false, trusted_private_https: false });
  const [limits, setLimits] = useState<AgentLimits>(DEFAULT_LIMITS);
  const [apiKey, setApiKey] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [connectionCheck, setConnectionCheck] = useState<{ configVersion: string; label: string } | null>(null);
  const [taskList, setTaskList] = useState<AgentTask[]>([]);
  const [selected, setSelected] = useState<AgentTask | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [nextSeq, setNextSeq] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [goal, setGoal] = useState("");
  const goalRef = useRef(goal);
  const [preservedGoal, setPreservedGoal] = useState<{ requestId: string; text: string } | null>(null);
  const [requestLimits, setRequestLimits] = useState<AgentLimits>(DEFAULT_LIMITS);
  const [disclosure, setDisclosure] = useState<AgentDisclosure | null>(null);
  const [disclosureContextKey, setDisclosureContextKey] = useState<string | null>(null);
  const [resumeDisclosure, setResumeDisclosure] = useState<AgentDisclosure | null>(null);
  const [resumeConsented, setResumeConsented] = useState(false);
  const [consented, setConsented] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [tradeReview, setTradeReview] = useState<{ draft: AgentDraft; preview: OrderPreview } | null>(null);
  const inFlight = useRef(false);
  const cancelKeys = useRef(new Map<string, string>());
  const resumeKeys = useRef(new Map<string, string>());
  const submissionIntent = useRef<{ signature: string; key: string } | null>(null);
  const handledGoalRequest = useRef<string | null>(null);
  const disclosureRequestSeq = useRef(0);
  const taskListRequestSeq = useRef(0);
  const taskSelectionSeq = useRef(0);
  const selectedTaskId = useRef<string | null>(null);
  const taskScopeId = practiceId ?? planId;
  const taskScopeRef = useRef(taskScopeId);
  const sessionContextKeyRef = useRef(sessionContextKey);
  useLayoutEffect(() => { sessionContextKeyRef.current = sessionContextKey; }, [sessionContextKey]);
  useLayoutEffect(() => {
    taskScopeRef.current = taskScopeId;
    taskListRequestSeq.current += 1;
    taskSelectionSeq.current += 1;
    selectedTaskId.current = null;
  }, [taskScopeId]);
  goalRef.current = goal;

  function updateGoal(value: string) {
    goalRef.current = value;
    setGoal(value);
  }

  useEffect(() => {
    if (!goalDraft?.requestId || handledGoalRequest.current === goalDraft.requestId) return;
    handledGoalRequest.current = goalDraft.requestId;
    disclosureRequestSeq.current += 1;
    updateGoal(goalDraft.text);
    setPreservedGoal({ requestId: goalDraft.requestId, text: goalDraft.text });
    setDisclosure(null);
    setDisclosureContextKey(null);
    setConsented(false);
    setBusy(current => current === "disclosure" ? null : current);
    setPanelError(null);
    submissionIntent.current = null;
  }, [goalDraft?.requestId]);

  useEffect(() => {
    disclosureRequestSeq.current += 1;
    setDisclosure(null);
    setDisclosureContextKey(null);
    setConsented(false);
    setBusy(current => current === "disclosure" ? null : current);
    submissionIntent.current = null;
  }, [sessionContextKey]);

  useEffect(() => {
    disclosureRequestSeq.current += 1;
    setDisclosure(null);
    setDisclosureContextKey(null);
    setConsented(false);
    setBusy(current => current === "disclosure" ? null : current);
    submissionIntent.current = null;
  }, [goal]);

  const refreshConfig = useCallback(async () => {
    try {
      const state = await getAgentConfig(); setConfigState(state); setConfigError(null);
      setLimits({ ...DEFAULT_LIMITS, ...(state.config?.limits ?? state.limits) });
      setRequestLimits({ ...DEFAULT_LIMITS, ...(state.config?.limits ?? state.limits) });
      if (state.config) setDraftConfig({ name: state.config.name, protocol: state.config.protocol, base_url: state.config.base_url, model: state.config.model, allow_loopback_http: state.config.allow_loopback_http, trusted_private_https: state.config.trusted_private_https });
    } catch (error) { setConfigError(error instanceof Error ? error.message : "读取模型配置失败。"); }
  }, []);
  const refreshTasks = useCallback(async () => {
    const requestSeq = ++taskListRequestSeq.current;
    try {
      const result = await getAgentTasks(practiceId, planId, scope);
      const scopedTasks = result.tasks.filter(task => practiceId ? task.practice_id === practiceId : planId ? task.plan_id === planId : true);
      if (requestSeq === taskListRequestSeq.current && taskScopeRef.current === taskScopeId) { setTaskList(scopedTasks); setListError(null); }
    } catch (error) {
      if (requestSeq === taskListRequestSeq.current && taskScopeRef.current === taskScopeId) setListError(error instanceof Error ? error.message : "读取任务历史失败。");
    }
  }, [practiceId, planId, scope, taskScopeId]);
  useEffect(() => { void refreshConfig(); }, [refreshConfig]);
  useEffect(() => { selectTask(null); setTaskList([]); setDisclosure(null); setConsented(false); if (!settingsOnly) void refreshTasks(); }, [refreshTasks, settingsOnly]);

  useEffect(() => {
    if (!selected || !ACTIVE.has(selected.status)) return;
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    let live = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const [task, eventPage] = await Promise.all([getAgentTask(selected.task_id), getAgentEvents(selected.task_id, nextSeq)]);
          if (!live || !isCurrentTask(selectionSeq, selected.task_id, selectedScope)) return;
          setSelected(task);
          setTaskList(current => current.map(item => item.task_id === task.task_id ? task : item));
          appendEventPage(eventPage);
          if (!ACTIVE.has(task.status)) void refreshTasks();
        } catch (error) { if (live && isCurrentTask(selectionSeq, selected.task_id, selectedScope)) setPanelError(error instanceof Error ? error.message : "任务状态读取暂不可用。"); }
      })();
    }, 1800);
    return () => { live = false; window.clearInterval(timer); };
  }, [selected?.task_id, selected?.status, nextSeq, refreshTasks]);

  const activeTask = useMemo(() => taskList.find(item => ACTIVE.has(item.status)) ?? null, [taskList]);
  useEffect(() => {
    onStatus?.(configError ? "配置状态不可用" : !configState?.configured ? "未配置模型" : activeTask ? STATUS[activeTask.status] ?? activeTask.status : "无运行中任务");
  }, [activeTask?.status, configError, configState?.configured, onStatus]);
  function selectTask(task: AgentTask | null) {
    taskSelectionSeq.current += 1;
    selectedTaskId.current = task?.task_id ?? null;
    setSelected(task); setEvents([]); setNextSeq(0); setResumeDisclosure(null); setResumeConsented(false); setTradeReview(null); setPanelError(null);
  }
  function isCurrentTask(selectionSeq: number, taskId: string, selectedScope: string | null) {
    return selectionSeq === taskSelectionSeq.current && selectedTaskId.current === taskId && taskScopeRef.current === selectedScope;
  }
  function appendEventPage(page: { events: AgentEvent[]; next_seq: number }) {
    setEvents(current => {
      const seen = new Set(current.map(item => item.seq));
      return [...current, ...page.events.filter(item => !seen.has(item.seq))].sort((a, b) => a.seq - b.seq);
    });
    setNextSeq(current => Math.max(current, page.next_seq));
  }
  function openTask(task: AgentTask) {
    selectTask(task);
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    void (async () => {
      try { const page = await getAgentEvents(task.task_id, 0); if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) appendEventPage(page); }
      catch { /* task result remains readable */ }
    })();
  }
  function cancelKeyFor(taskId: string) { let key = cancelKeys.current.get(taskId); if (!key) { key = newKey(); cancelKeys.current.set(taskId, key); } return key; }

  async function persistConfig(event: React.FormEvent) {
    event.preventDefault(); setSettingsBusy(true); setSettingsMessage(null); setConfigError(null);
    try {
      const body: Record<string, unknown> = { ...draftConfig, limits, ...(apiKey ? { api_key: apiKey } : {}), ...(configState?.config?.config_version ? { expected_config_version: configState.config.config_version } : {}) };
      const saved = await saveAgentConfig(body); setConfigState(saved); setConnectionCheck(null); setSettingsMessage("配置已保存；保存状态不代表服务已验证。已有任务继续使用原配置版本。");
      disclosureRequestSeq.current += 1; setDisclosure(null); setDisclosureContextKey(null); setConsented(false); setResumeDisclosure(null); setResumeConsented(false);
      setLimits({ ...DEFAULT_LIMITS, ...(saved.config?.limits ?? saved.limits) });
    } catch (error) { setConfigError(error instanceof Error ? error.message : "保存配置失败。"); }
    finally { setApiKey(""); setSettingsBusy(false); }
  }
  async function runConnectionTest() {
    if (!configState?.config || !window.confirm("连接检测会向已配置服务发送一次最小工具能力请求，可能产生费用。继续吗？")) return;
    const configVersion = configState.config.config_version;
    setSettingsBusy(true); setSettingsMessage(null); setConnectionCheck({ configVersion, label: "正在检测…" });
    try { const result = await testAgentConfig(configVersion); setConnectionCheck({ configVersion, label: `最近检测：${result.reachable ? "服务可达" : "服务不可达"}；工具调用：${result.tool_supported ? "支持" : "未确认/不支持"}${result.reason ? `；原因：${result.reason}` : ""}` }); }
    catch (error) { const detail = error instanceof Error ? error.message : "连接检测失败。"; setConnectionCheck({ configVersion, label: `最近检测失败：${detail}` }); setConfigError(detail); }
    finally { setSettingsBusy(false); }
  }
  async function loadDisclosure() {
    if (!configState?.configured || activeTask) return;
    const requestSeq = ++disclosureRequestSeq.current;
    const requestedContextKey = sessionContextKey;
    const requestedRevision = session?.revision ?? null;
    setBusy("disclosure"); setPanelError(null); setDisclosure(null); setConsented(false);
    setDisclosureContextKey(null);
    try {
      const next = await getDisclosure(practiceId, requestedRevision, planId, planVersion);
      if (requestSeq === disclosureRequestSeq.current && sessionContextKeyRef.current === requestedContextKey) { setDisclosure(next); setDisclosureContextKey(requestedContextKey); setRequestLimits({ ...next.limits }); }
    } catch (error) { if (requestSeq === disclosureRequestSeq.current && sessionContextKeyRef.current === requestedContextKey) setPanelError(error instanceof Error ? error.message : "数据披露摘要获取失败。"); }
    finally { if (requestSeq === disclosureRequestSeq.current) setBusy(null); }
  }
  async function loadResumeDisclosure(task: AgentTask) {
    if (!task.resumable) return;
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    setBusy("resume-disclosure"); setPanelError(null); setResumeDisclosure(null); setResumeConsented(false);
    const taskRevision = typeof task.context.revision === "number" ? task.context.revision : null;
    try { const next = await getDisclosure(task.practice_id, taskRevision, task.plan_id ?? null, task.expected_plan_version ?? null); if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setResumeDisclosure(next); }
    catch (error) { if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setPanelError(error instanceof Error ? error.message : "恢复披露摘要获取失败。"); }
    finally { setBusy(null); }
  }
  async function submitResume(task: AgentTask) {
    if (!task.resumable || !task.operation_id || task.generation === undefined || !resumeDisclosure || !resumeConsented || inFlight.current) return;
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    const taskRevision = typeof task.context.revision === "number" ? task.context.revision : null;
    inFlight.current = true; setBusy("resume"); setPanelError(null);
    try {
      const current = await getAgentTask(task.task_id);
      if (!current.resumable || current.status !== "interrupted" || current.generation !== task.generation || current.operation_id !== task.operation_id) throw new Error("任务状态已变化；未重新提交恢复请求，请刷新并重新核对披露摘要。");
      let currentRevision = taskRevision;
      if (current.practice_id) {
        const latestSession = await fetchSession(current.practice_id);
        currentRevision = latestSession.revision;
        if (taskRevision !== null && latestSession.revision !== taskRevision) throw new Error("模拟修订已变化；请刷新任务并重新读取披露摘要，不会自动恢复旧上下文。");
      }
      if (current.plan_id) {
        const latestPlan = await fetchPracticePlan(current.plan_id);
        if (latestPlan.version !== current.expected_plan_version) throw new Error("方案版本已变化；请刷新任务并重新读取披露摘要，不会自动恢复旧上下文。");
      }
      const checkedDisclosure = await getDisclosure(current.practice_id, currentRevision, current.plan_id ?? null, current.expected_plan_version ?? null);
      if (checkedDisclosure.consent_digest !== resumeDisclosure.consent_digest) throw new Error("外发摘要已变化；请重新查看并同意最新摘要后再恢复。");
      if (!resumeKeys.current.has(current.task_id)) resumeKeys.current.set(current.task_id, newKey());
      const resumed = await resumeAgentTask(current.operation_id!, { consent: true, expected_generation: current.generation!, config_version: checkedDisclosure.config_version, consent_digest: checkedDisclosure.consent_digest, idempotency_key: resumeKeys.current.get(current.task_id)! });
      resumeKeys.current.delete(current.task_id);
      if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) openTask(resumed); await refreshTasks();
    }
    catch (error) { if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setPanelError(error instanceof Error ? error.message : "恢复任务失败；请重新读取状态与披露摘要。"); }
    finally { inFlight.current = false; setBusy(null); }
  }
  async function submitGoal(event: React.FormEvent) {
    event.preventDefault();
    if (!configState?.configured || !goal.trim() || inFlight.current || activeTask) return;
    if (!disclosure || disclosureContextKey !== sessionContextKey) {
      await loadDisclosure();
      return;
    }
    if (!consented) return;
    const submittedGoal = goal.trim();
    const submittedContextKey = sessionContextKey;
    const selectionSeq = taskSelectionSeq.current;
    inFlight.current = true; setBusy("create"); setPanelError(null);
    try {
      const selectedRevision = typeof selected?.context.revision === "number" ? selected.context.revision : null;
      const oldFingerprint = contextValue(selected?.context, "context_fingerprint");
      const currentFingerprint = contextValue(disclosure.context, "context_fingerprint");
      const sameFingerprint = typeof oldFingerprint === "string" && typeof currentFingerprint === "string" && oldFingerprint === currentFingerprint;
      const disclosurePractice = contextValue(disclosure.context, "practice_id") ?? contextValue(disclosure.context, "practice");
      const disclosureRevision = contextValue(disclosure.context, "revision");
      const samePractice = selected?.practice_id === practiceId && selectedRevision === (session?.revision ?? null) && (disclosurePractice === undefined || disclosurePractice === practiceId) && (disclosureRevision === undefined || disclosureRevision === (session?.revision ?? null));
      const samePlan = practiceId ? (selected?.plan_id ?? null) === planId : planId !== null && selected?.plan_id === planId;
      const parent = selected?.status === "completed" && !selected.stale && sameFingerprint && samePractice && samePlan ? selected.task_id : null;
      const body = { practice_id: practiceId, expected_revision: session?.revision ?? null, ...(planId ? { plan_id: planId, expected_plan_version: planVersion } : {}), ...(parent ? { parent_task_id: parent } : {}), goal: submittedGoal, config_version: disclosure.config_version, consent: true as const, consent_digest: disclosure.consent_digest, limits: requestLimits };
      const signature = JSON.stringify(body);
      if (!submissionIntent.current || submissionIntent.current.signature !== signature) {
        submissionIntent.current = { signature, key: newKey() };
      }
      // Keep the same key after an ambiguous network failure so a retry replays
      // the server's original task instead of starting a second paid request.
      const task = await createAgentTask({ ...body, idempotency_key: submissionIntent.current.key });
      submissionIntent.current = null;
      if (sessionContextKeyRef.current === submittedContextKey) {
        if (goalRef.current === submittedGoal) updateGoal("");
        setDisclosure(null); setDisclosureContextKey(null); setConsented(false);
        if (selectionSeq === taskSelectionSeq.current) openTask(task);
        await refreshTasks();
      }
    } catch (error) { if (sessionContextKeyRef.current === submittedContextKey) setPanelError(error instanceof Error ? error.message : "任务提交失败。"); }
    finally { inFlight.current = false; setBusy(null); }
  }
  async function stopTask(task: AgentTask) {
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    setBusy("cancel"); setPanelError(null);
    try { const stopped = await cancelAgentTask(task.task_id, cancelKeyFor(task.task_id)); if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setSelected(stopped); await refreshTasks(); }
    catch (error) { if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setPanelError(error instanceof Error ? error.message : "停止任务失败。"); }
    finally { setBusy(null); }
  }
  async function removeTask(task: AgentTask) {
    const selectionSeq = taskSelectionSeq.current;
    const selectedScope = taskScopeRef.current;
    setBusy("delete"); setPanelError(null);
    try { let current = task; if (ACTIVE.has(current.status)) { current = await cancelAgentTask(current.task_id, cancelKeyFor(current.task_id)); if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setSelected(current); }
      if (ACTIVE.has(current.status)) throw new Error("取消请求已送达，任务仍在运行；终态后可再次删除。");
      await deleteAgentTask(task.task_id); if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) selectTask(null); await refreshTasks();
    } catch (error) { if (isCurrentTask(selectionSeq, task.task_id, selectedScope)) setPanelError(error instanceof Error ? error.message : "删除任务失败。"); }
    finally { setBusy(null); }
  }
  async function prepareTrade(draft: AgentDraft) {
    const selectionSeq = taskSelectionSeq.current;
    const contextKey = sessionContextKey;
    setBusy("draft"); setPanelError(null); setTradeReview(null);
    try {
      if (!session || selected?.scope !== "simulation" || selected.practice_id !== session.practice_id || draft.practice_id !== session.practice_id || draft.context_fingerprint !== selected.context.context_fingerprint || !isTradePayload(draft.payload)) throw new Error("草稿绑定或字段无效，请重新试算。");
      const current = await fetchSession(session.practice_id);
      if (current.revision !== draft.expected_revision || current.revision !== session.revision || current.revealed_date !== draft.revealed_date || !isRunning(current.status)) throw new Error("模拟日期或版本已变化，草稿过期。请重新试算后再确认。");
      const input = { ts_code: String(draft.payload.ts_code), direction: String(draft.payload.direction), planned_shares: Number(draft.payload.planned_shares) };
      const preview = await previewOrder(session.practice_id, input); if (selectionSeq === taskSelectionSeq.current && contextKey === sessionContextKeyRef.current) setTradeReview({ draft, preview });
    } catch (error) { if (selectionSeq === taskSelectionSeq.current && contextKey === sessionContextKeyRef.current) setPanelError(error instanceof Error ? error.message : "草稿试算失败。"); }
    finally { setBusy(null); }
  }
  async function submitTrade() {
    if (!session || !tradeReview || busy) return;
    setBusy("order"); setPanelError(null);
    try {
      const { draft } = tradeReview; const latest = await fetchSession(session.practice_id);
      if (latest.revision !== draft.expected_revision || latest.revealed_date !== draft.revealed_date || !isRunning(latest.status)) throw new Error("模拟已变化，订单未提交。请重新试算。");
      const key = `agent-trade:${draft.draft_id}`;
      const updated = await placeOrder(session.practice_id, { ts_code: String(draft.payload.ts_code), direction: String(draft.payload.direction), planned_shares: Number(draft.payload.planned_shares), idempotency_key: key, expected_revision: draft.expected_revision ?? latest.revision });
      setTradeReview(null); onPracticeRefresh?.(); setPanelError(null); setSettingsMessage(`订单草稿已提交；模拟修订 ${updated.revision}。`);
    } catch (error) { setPanelError(error instanceof Error ? error.message : "订单未提交。"); }
    finally { setBusy(null); }
  }

  const settingsForm = <form className="agent-settings-form" onSubmit={persistConfig}>
    <label>配置名称<input value={draftConfig.name} onChange={e => setDraftConfig({ ...draftConfig, name: e.target.value })} required /></label>
    <label>协议<select value={draftConfig.protocol} onChange={e => setDraftConfig({ ...draftConfig, protocol: e.target.value })}><option value="chat_completions">Chat Completions（工具调用）</option><option value="unsupported">其他协议（当前不支持）</option></select></label>
    <label>服务 API URL（填写兼容的 Chat Completions 根地址）<input type="url" value={draftConfig.base_url} onChange={e => setDraftConfig({ ...draftConfig, base_url: e.target.value })} placeholder="https://…" required /></label>
    <label>模型名称（使用服务商提供的 model 标识）<input value={draftConfig.model} onChange={e => setDraftConfig({ ...draftConfig, model: e.target.value })} required /></label>
    <label>API Key（仅本次内存输入）<input type="password" autoComplete="new-password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configState?.config?.key_configured ? `已配置 ${configState.config.key_masked ?? ""}；留空保留` : "输入服务密钥"} /></label>
    <div className="agent-limit-grid">{(["rounds", "tool_calls", "timeout_seconds", "tokens"] as const).map(key => <label key={key}>{({ rounds: "模型轮次", tool_calls: "工具次数", timeout_seconds: "时限（秒）", tokens: "Token预算" })[key]}<input type="number" min="1" max={HARD_LIMITS[key]} value={limits[key]} onChange={e => setLimits({ ...limits, [key]: Math.min(HARD_LIMITS[key], Math.max(1, Number(e.target.value) || 1)) })} /></label>)}</div>
    <label className="agent-check"><input type="checkbox" checked={draftConfig.allow_loopback_http} onChange={e => setDraftConfig({ ...draftConfig, allow_loopback_http: e.target.checked })} />允许本机回环 HTTP</label>
    <label className="agent-check"><input type="checkbox" checked={draftConfig.trusted_private_https} onChange={e => setDraftConfig({ ...draftConfig, trusted_private_https: e.target.checked })} />信任私有 HTTPS 目标</label>
    {configState?.config ? <p className="agent-muted">当前密钥：{configState.config.key_configured ? `已配置 ${configState.config.key_masked ?? ""}` : "未配置"} · 配置保存与连通性检测分开；当前任务使用的版本不会随设置更改。</p> : null}
    <p className="agent-connection-state" role="status">{configState?.config && connectionCheck?.configVersion === configState.config.config_version ? connectionCheck.label : configState?.configured ? "尚未检测当前配置。保存或更改目标后需重新检测。" : "尚未配置模型目标。手动模拟仍可继续。"}</p>
    <div className="agent-actions"><button className="practice-primary" type="submit" disabled={settingsBusy}>{settingsBusy ? "保存中…" : "保存配置"}</button><button className="refresh-button" type="button" onClick={() => void runConnectionTest()} disabled={!configState?.configured || settingsBusy}>检测连接与工具能力</button></div>
    {settingsMessage ? <p className="agent-success" role="status">{settingsMessage}</p> : null}
  </form>;

  if (settingsOnly) return <section className="agent-panel agent-settings-only" aria-labelledby="agent-settings-heading">
    <header className="agent-panel-header"><div><p className="eyebrow">控制室 · 模型连接</p><h2 id="agent-settings-heading">Agent 模型设置</h2><p className="agent-muted">保存目标配置与本地密钥状态；连接检测会向已配置服务发送一次请求，并可能收费。</p></div><span className={`agent-config-state ${configState?.configured ? "is-ready" : ""}`}>{configState?.configured ? `已配置 · ${configState.config?.name}` : configError ? "配置不可用" : "尚未配置模型"}</span></header>
    {configError ? <Notice tone="warn">{configError}</Notice> : null}
    <div className="agent-settings"><h3>本地模型服务</h3>{settingsForm}</div>
  </section>;

  return <section className="agent-panel" aria-labelledby="agent-heading">
    <header className="agent-panel-header"><div><p className="eyebrow">Agent 任务台 · {scope === "setup" ? "新模拟条件" : "当前历史模拟"}</p><h2 id="agent-heading">把目标交给 Agent 查证与整理</h2><p className="agent-muted">工具进度、证据、结论与待确认草稿集中显示。Agent 不会自动下单、推进日期或创建模拟。</p></div><div className="agent-header-actions"><span className={`agent-config-state ${configState?.configured ? "is-ready" : ""}`}>{configState?.configured ? `已配置 · ${configState.config?.name}` : configError ? "配置不可用" : "尚未配置模型"}</span><button type="button" className="refresh-button" onClick={onGoControl}>控制室设置</button></div></header>
    {configError ? <Notice tone="warn">{configError}。无模型时仍可直接使用历史模拟。</Notice> : null}
    {session ? <p className="agent-context-line">正在为「{session.name}」查证，范围截至 {formatDateShort(session.revealed_date)}；Agent 只整理建议，不会自动下单或推进。</p> : <p className="agent-context-line">{planId ? `正在协助编辑已保存方案 v${planVersion ?? "未知"}` : "先保存方案即可把后续提问接在同一上下文中。"}；发送范围以你确认的披露摘要为准。</p>}
     {!configState?.configured ? <div className="agent-empty"><strong>尚未连接模型</strong><span>仍可手动设置方案、预览订单和推进练习。</span>{preservedGoal ? <label className="practice-field"><span>保留的问题</span><textarea value={goal} onChange={event => updateGoal(event.target.value)} maxLength={1200} rows={3} /></label> : null}<button className="refresh-button" type="button" onClick={onGoControl}>去控制室连接模型</button></div> : null}
    {configState?.configured ? <form className="agent-task-form" onSubmit={submitGoal}>
      <label htmlFor="agent-goal">本次目标</label><textarea id="agent-goal" value={goal} onChange={e => updateGoal(e.target.value)} maxLength={1200} rows={3} placeholder={scope === "setup" ? "例如：整理一个面向消费行业的历史模拟条件草稿" : "例如：核对当前持仓在已揭晓区间的主要风险，并引用可查证依据"} disabled={Boolean(activeTask)} />
      <div className="agent-suggestions" aria-label="目标建议"><span>建议问题（仅填入输入框）</span>{(scope === "setup" ? ["整理一个可检验的历史模拟条件", "列出开始研究前还缺少的资料"] : ["核对当前持仓在已揭晓区间的风险", "查找支持或反驳当前研究问题的证据"]).map(suggestion => <button key={suggestion} type="button" className="agent-suggestion" onClick={() => updateGoal(suggestion)} disabled={Boolean(activeTask)}>{suggestion}</button>)}</div>
      <div className="agent-form-footer"><span>{disclosure && disclosureContextKey === sessionContextKey ? "发送前请确认本次披露内容。" : "提交前会自动读取最新披露摘要，不会直接发送。"}</span><button type="submit" className="practice-primary" disabled={!goal.trim() || Boolean(activeTask || busy)}>{busy === "disclosure" ? "读取披露摘要…" : busy === "create" ? "提交中…" : disclosure && disclosureContextKey === sessionContextKey ? (consented ? "同意并发送" : "请先确认披露") : "继续并查看披露"}</button></div>
      {disclosure && disclosureContextKey === sessionContextKey ? <div className="agent-disclosure"><h3>即将发送给 {disclosure.target.name}</h3><p>{disclosure.target.model} · {disclosure.target.base_url}</p><p>用途：{disclosure.purpose} · 可能产生服务商费用：{disclosure.possible_cost ? "是" : "否（按服务端说明）"}</p><p><strong>会发送：</strong>{disclosure.data_categories.slice(0, 3).join("、") || "服务端未列出"}{disclosure.data_categories.length > 3 ? ` 等 ${disclosure.data_categories.length} 类` : ""}</p><details><summary>查看数据类别与任务上限</summary><ul>{disclosure.data_categories.map(item => <li key={item}>{item}</li>)}</ul><p>{disclosure.limits.rounds} 轮 · {disclosure.limits.tool_calls} 次工具 · {disclosure.limits.timeout_seconds} 秒 · {disclosure.limits.tokens} Token</p></details><label className="agent-consent"><input type="checkbox" checked={consented} onChange={e => setConsented(e.target.checked)} />我已阅读并同意本次发送内容</label></div> : null}
    </form> : null}
    {panelError ? <Notice tone="error">{panelError}</Notice> : null}
    <div className="agent-columns"><div className="agent-history"><div className="agent-subhead"><h3>对话记录</h3><button className="link-button" type="button" onClick={() => void refreshTasks()}>刷新</button></div>{listError ? <p className="agent-muted">{listError}</p> : taskList.length === 0 ? <p className="agent-muted">当前上下文暂无对话。</p> : <ul>{taskList.map(task => <li key={task.task_id}><button type="button" className={selected?.task_id === task.task_id ? "agent-task-link selected" : "agent-task-link"} onClick={() => openTask(task)}><span>你 · {new Date(task.created_at).toLocaleString()}</span><strong>{task.goal}</strong>{task.result?.summary ? <span>Agent · {task.result.summary}</span> : <span>{STATUS[task.status] ?? task.status}</span>}</button><button type="button" className="link-button" aria-label={`删除任务 ${task.goal}`} onClick={() => void removeTask(task)} disabled={Boolean(busy)}>删除</button></li>)}</ul>}</div>
    {selected ? <article className="agent-task-detail"><header><div><span className={`agent-status status-${selected.status}`}>{STATUS[selected.status] ?? selected.status}</span><h3>{selected.goal}</h3><p className="agent-muted">{new Date(selected.created_at).toLocaleString()}{selected.parent_task_id ? " · 接续此前对话" : " · 新对话"}</p><details className="agent-technical-context"><summary>查看任务上下文</summary><p>{selected.practice_id ? `练习 ${selected.practice_id} · revision ${selected.context.revision ?? "未知"} · as_of ${selected.context.as_of ?? "未知"}` : `方案 ${selected.plan_id ?? "未绑定"} v${selected.expected_plan_version ?? "未知"}`} · task {selected.task_id} · parent {selected.parent_task_id ?? "无"}</p></details></div><div className="agent-actions">{ACTIVE.has(selected.status) ? <button type="button" className="refresh-button" onClick={() => void stopTask(selected)} disabled={Boolean(busy)}>停止当前分析</button> : null}<button type="button" className="link-button" onClick={() => void removeTask(selected)} disabled={Boolean(busy)}>删除记录</button></div></header>
      {selected.stale ? <Notice tone="warn">绑定模拟版本已过期；此结果仅供历史查看，草稿不能提交。</Notice> : null}
      <details className="agent-usage-details"><summary>查看任务用量与成本</summary><div className="agent-usage"><span>输入 Token：{fmt(selected.usage.input_tokens)}</span><span>输出 Token：{fmt(selected.usage.output_tokens)}</span><span>合计：{fmt(selected.usage.total_tokens)}</span><span>模型调用：{selected.usage.model_calls}</span><span>工具调用：{selected.usage.tool_calls}</span><span>用时：{fmt(selected.usage.elapsed_ms, " ms")}</span><span>费用估算：{selected.usage.estimated_cost === null ? "未知" : String(selected.usage.estimated_cost)}</span></div></details>
      {selected.status === "failed" ? <Notice tone="warn" title="这次分析没有完成"><p>可以修改问题后重新尝试；如果再次失败，请检查控制室中的模型连接与工具调用能力。任务不会自动执行任何订单。</p>{selected.stop_reason ? <details><summary>查看服务端停止原因</summary><code>{selected.stop_reason}</code></details> : null}</Notice> : null}
      {selected.status === "cancelled" ? <Notice tone="neutral" title="分析已停止"><p>没有自动执行订单。可修改问题后重新开始分析。</p>{selected.stop_reason ? <details><summary>查看停止原因</summary><code>{selected.stop_reason}</code></details> : null}</Notice> : null}
      {selected.status === "interrupted" && selected.stop_reason ? <details className="agent-stop-reason"><summary>查看中断原因</summary><code>{selected.stop_reason}</code></details> : null}
      {selected.status === "interrupted" && selected.resumable ? <div className="agent-disclosure"><h4>任务可恢复</h4><p>恢复状态由服务端校验。继续前需重新读取并确认本次外发内容，旧授权不会沿用。</p>{!resumeDisclosure ? <button type="button" className="refresh-button" onClick={() => void loadResumeDisclosure(selected)} disabled={Boolean(busy)}>重新生成恢复披露摘要</button> : <><dl><div><dt>提供方 / 目标</dt><dd>{resumeDisclosure.target.name} · {resumeDisclosure.target.base_url} · {resumeDisclosure.target.model}</dd></div><div><dt>用途</dt><dd>{resumeDisclosure.purpose}</dd></div><div><dt>可能收费</dt><dd>{resumeDisclosure.possible_cost ? "是，取决于服务商" : "服务端标记为否"}</dd></div><div><dt>剩余预算 / 限制</dt><dd>{resumeDisclosure.limits.rounds} 轮 · {resumeDisclosure.limits.tool_calls} 次工具 · {resumeDisclosure.limits.timeout_seconds} 秒 · {resumeDisclosure.limits.tokens} Token</dd></div></dl><strong>本次数据类别</strong><ul>{resumeDisclosure.data_categories.map(item => <li key={item}>{item}</li>)}</ul><label className="agent-consent"><input type="checkbox" checked={resumeConsented} onChange={e => setResumeConsented(e.target.checked)}/>我已重新阅读披露摘要并同意继续发送任务所需的最小数据。</label><button className="practice-primary" type="button" onClick={() => void submitResume(selected)} disabled={!resumeConsented || Boolean(busy) || !selected.operation_id || selected.generation === undefined}>同意并恢复此任务</button></>}</div> : null}
      {selected.status === "interrupted" && selected.resumable === false ? <Notice tone="warn" title="此分析无法继续"><p>服务端未允许恢复。可以修改问题后重新开始，或检查控制室中的模型连接和任务限制；不会自动执行订单。</p>{selected.stop_reason ? <details><summary>查看服务端原因</summary><code>{selected.stop_reason}</code></details> : null}</Notice> : null}
      <details className="agent-steps"><summary>工具步骤与状态</summary>{events.length ? <ol>{events.map(item => <li key={item.seq}><span>{KIND[item.kind] ?? item.kind}</span>{item.tool_name ? <code>{item.tool_name}</code> : null}<p>{item.summary}</p>{item.reason ? <small>停止或失败说明：{item.reason}</small> : null}{item.evidence_ids.length ? <details><summary>查看引用 ID（{item.evidence_ids.length}）</summary><ul>{item.evidence_ids.map(id => <li key={id}><code>{id}</code></li>)}</ul></details> : null}</li>)}</ol> : <p className="agent-muted">{ACTIVE.has(selected.status) ? "等待服务端事件…" : "没有事件记录。"}</p>}</details>
      {selected.result ? <div className="agent-result"><h4>结论</h4><p className="agent-result-text">{selected.result.summary}</p>{selected.result.limitations.length ? <div><h4>数据与结论限制</h4><ul>{selected.result.limitations.map((item, i) => <li key={`${i}-${item}`}>{item}</li>)}</ul></div> : null}
        {selected.result.evidence.length ? <div><h4>证据引用</h4><div className="agent-evidence-list">{selected.result.evidence.map((item, i) => { const data = item.data && typeof item.data === "object" && !Array.isArray(item.data) ? item.data as Record<string, unknown> : null; return <details key={item.evidence_id ?? i}><summary>{item.tool_name ?? "工具结果"} · {item.as_of ?? "日期未知"} · {item.evidence_id ?? `证据 ${i + 1}`}</summary><p>{item.summary ?? "核对证据摘要与固定版本引用。"}</p><dl><div><dt>版本</dt><dd>{item.version_id ?? stringify(item.version)}</dd></div>{data ? Object.entries(data).slice(0, 30).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{evidenceValue(key, value)}</dd></div>) : null}</dl>{!data && item.data !== undefined ? <pre>{stringify(item.data)}</pre> : null}</details>; })}</div></div> : null}
        {selected.result.drafts.length ? <div><h4>待确认草稿</h4>{selected.result.drafts.map(draft => {
          const draftPlanId = draft.plan_id ?? selected.plan_id;
          const draftPlanVersion = draft.expected_plan_version ?? selected.expected_plan_version;
          const draftContextMatches = typeof selected.context.context_fingerprint === "string" && draft.context_fingerprint === selected.context.context_fingerprint;
          const setupOwnerMatches = selected.scope === "setup" && draftContextMatches && Boolean(planId) && draftPlanId === planId && draftPlanVersion === planVersion && selected.plan_id === planId && selected.expected_plan_version === planVersion;
          const tradeOwnerMatches = selected.scope === "simulation" && draftContextMatches && Boolean(session) && selected.practice_id === session?.practice_id && draft.practice_id === session?.practice_id && (typeof selected.context.revision !== "number" || selected.context.revision === draft.expected_revision);
          return <article className="agent-draft" key={draft.draft_id}><strong>{draft.kind === "trade" ? "交易草稿" : "练习方案草稿"}</strong><p>来源：Agent 任务 · revision {draft.expected_revision ?? "未绑定"} · 已揭晓至 {draft.revealed_date ?? "无日期"} · 方案 v{draftPlanVersion ?? "未绑定"}</p>{draft.limitations.length ? <ul>{draft.limitations.map(item => <li key={item}>{item}</li>)}</ul> : null}<details><summary>查看草稿字段</summary><pre>{stringify(draft.payload)}</pre></details>{draft.kind === "setup" ? <button type="button" className="refresh-button" onClick={() => onSetupDraft?.(draft.payload)} disabled={!onSetupDraft || selected.stale || !setupOwnerMatches}>核对并更新当前方案</button> : <button type="button" className="refresh-button" onClick={() => void prepareTrade(draft)} disabled={!tradeOwnerMatches || selected.stale || busy === "draft" || !isTradePayload(draft.payload)}>核对并预览订单</button>}</article>;
        })}</div> : null}
      </div> : null}
      {tradeReview ? (() => {
        const preview = tradeReview.preview;
        const payload = tradeReview.draft.payload;
        const referencePrice = firstFiniteNumber(preview.price_cents, preview.estimated_price_cents, preview.known_price_cents);
        const estimatedAmount = firstFiniteNumber(preview.gross_cents, preview.estimated_gross_cents, preview.estimated_notional_cents);
        const estimatedFee = firstFiniteNumber(preview.fee_cents, preview.estimated_fee_cents);
        const earliestDate = firstNonEmptyString(preview.earliest_trade_date, preview.trade_date, preview.execution_date);
        const warnings = [...previewMessages(preview.restrictions), ...previewMessages(preview.warnings), ...previewMessages(preview.limitations)];
        return <div className="agent-trade-review"><h4>订单预览</h4><dl><div><dt>证券</dt><dd>{String(payload.ts_code)}</dd></div><div><dt>方向与数量</dt><dd>{payload.direction === "buy" ? "买入" : "卖出"} {formatShares(Number(payload.planned_shares))} 股</dd></div><div><dt>当前参考价格</dt><dd>{referencePrice === null || referencePrice === undefined ? "未知" : `${formatCents(referencePrice)} 元`}</dd></div><div><dt>预估金额</dt><dd>{estimatedAmount === null || estimatedAmount === undefined ? "未知" : `${formatCents(estimatedAmount)} 元`}</dd></div><div><dt>预估费用</dt><dd>{estimatedFee === null || estimatedFee === undefined ? "未知" : `${formatCents(estimatedFee)} 元`}</dd></div><div><dt>最早尝试成交日</dt><dd>{typeof earliestDate === "string" ? formatDateShort(earliestDate) : "服务端未提供"}</dd></div></dl><p>以上价格与金额为服务端预估；未成交前，未来实际成交价仍未知。确认只登记订单，不会自动推进模拟。</p>{warnings.length ? <ul className="practice-restrictions">{warnings.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul> : null}<details><summary>查看完整服务端预览</summary><pre>{stringify(preview)}</pre></details><div className="agent-actions"><button className="practice-primary" type="button" disabled={Boolean(busy) || selected.stale} onClick={() => void submitTrade()}>{busy === "order" ? "提交中…" : "确认提交订单"}</button><button type="button" className="link-button" onClick={() => setTradeReview(null)} disabled={Boolean(busy)}>取消</button></div></div>;
      })() : null}
      </article> : <div className="agent-task-detail agent-empty"><strong>选择任务查看步骤、结论和引用证据</strong><span>任务终态后停止轮询；历史任务可查看或删除。</span></div>}</div>
  </section>;
}
