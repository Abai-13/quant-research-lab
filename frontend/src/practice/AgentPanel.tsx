import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AgentConfigResponse, AgentDisclosure, AgentDraft, AgentEvent, AgentLimits, AgentTask } from "./agentTypes";
import { cancelAgentTask, createAgentTask, deleteAgentTask, getAgentConfig, getAgentEvents, getAgentTask, getAgentTasks, getDisclosure, saveAgentConfig, testAgentConfig } from "./agentClient";
import type { PracticeView, OrderPreview } from "./types";
import { fetchSession, placeOrder, previewOrder } from "./client";
import { formatBps, formatCents, formatDateShort, formatShares, isRunning } from "./format";
import { Notice } from "./ui";

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

export function AgentPanel({ session = null, onPracticeRefresh, onSetupDraft }: { session?: SessionContext | null; onPracticeRefresh?: () => void; onSetupDraft?: (payload: Record<string, unknown>) => void }) {
  const practiceId = session?.practice_id ?? null;
  const scope = practiceId ? "simulation" : "setup";
  const [configState, setConfigState] = useState<AgentConfigResponse | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [draftConfig, setDraftConfig] = useState({ name: "", protocol: "chat_completions", base_url: "", model: "", allow_loopback_http: false, trusted_private_https: false });
  const [limits, setLimits] = useState<AgentLimits>(DEFAULT_LIMITS);
  const [apiKey, setApiKey] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [taskList, setTaskList] = useState<AgentTask[]>([]);
  const [selected, setSelected] = useState<AgentTask | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [nextSeq, setNextSeq] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [goal, setGoal] = useState("");
  const [requestLimits, setRequestLimits] = useState<AgentLimits>(DEFAULT_LIMITS);
  const [disclosure, setDisclosure] = useState<AgentDisclosure | null>(null);
  const [consented, setConsented] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [tradeReview, setTradeReview] = useState<{ draft: AgentDraft; preview: OrderPreview } | null>(null);
  const inFlight = useRef(false);
  const cancelKeys = useRef(new Map<string, string>());
  const submissionIntent = useRef<{ signature: string; key: string } | null>(null);

  const refreshConfig = useCallback(async () => {
    try {
      const state = await getAgentConfig(); setConfigState(state); setConfigError(null);
      setLimits({ ...DEFAULT_LIMITS, ...(state.config?.limits ?? state.limits) });
      setRequestLimits({ ...DEFAULT_LIMITS, ...(state.config?.limits ?? state.limits) });
      if (state.config) setDraftConfig({ name: state.config.name, protocol: state.config.protocol, base_url: state.config.base_url, model: state.config.model, allow_loopback_http: state.config.allow_loopback_http, trusted_private_https: state.config.trusted_private_https });
    } catch (error) { setConfigError(error instanceof Error ? error.message : "读取模型配置失败。"); }
  }, []);
  const refreshTasks = useCallback(async () => {
    try { const result = await getAgentTasks(practiceId); setTaskList(result.tasks); setListError(null); }
    catch (error) { setListError(error instanceof Error ? error.message : "读取任务历史失败。"); }
  }, [practiceId]);
  useEffect(() => { void refreshConfig(); }, [refreshConfig]);
  useEffect(() => { setSelected(null); setEvents([]); setNextSeq(0); setDisclosure(null); setConsented(false); setTradeReview(null); void refreshTasks(); }, [refreshTasks]);

  useEffect(() => {
    if (!selected || !ACTIVE.has(selected.status)) return;
    let live = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const [task, eventPage] = await Promise.all([getAgentTask(selected.task_id), getAgentEvents(selected.task_id, nextSeq)]);
          if (!live) return;
          setSelected(task);
          setTaskList(current => current.map(item => item.task_id === task.task_id ? task : item));
          if (eventPage.events.length) setEvents(current => {
            const seen = new Set(current.map(item => item.seq));
            return [...current, ...eventPage.events.filter(item => !seen.has(item.seq))].sort((a, b) => a.seq - b.seq);
          });
          setNextSeq(eventPage.next_seq);
          if (!ACTIVE.has(task.status)) void refreshTasks();
        } catch (error) { if (live) setPanelError(error instanceof Error ? error.message : "任务状态读取暂不可用。"); }
      })();
    }, 1800);
    return () => { live = false; window.clearInterval(timer); };
  }, [selected?.task_id, selected?.status, nextSeq, refreshTasks]);

  const activeTask = useMemo(() => taskList.find(item => ACTIVE.has(item.status)) ?? null, [taskList]);
  function openTask(task: AgentTask) { setSelected(task); setEvents([]); setNextSeq(0); void (async () => { try { const page = await getAgentEvents(task.task_id, 0); setEvents(page.events); setNextSeq(page.next_seq); } catch { /* task result remains readable */ } })(); }
  function cancelKeyFor(taskId: string) { let key = cancelKeys.current.get(taskId); if (!key) { key = newKey(); cancelKeys.current.set(taskId, key); } return key; }

  async function persistConfig(event: React.FormEvent) {
    event.preventDefault(); setSettingsBusy(true); setSettingsMessage(null); setConfigError(null);
    try {
      const body: Record<string, unknown> = { ...draftConfig, limits, ...(apiKey ? { api_key: apiKey } : {}), ...(configState?.config?.config_version ? { expected_config_version: configState.config.config_version } : {}) };
      const saved = await saveAgentConfig(body); setConfigState(saved); setSettingsMessage("配置已保存。已有任务继续使用原配置版本。");
      setLimits({ ...DEFAULT_LIMITS, ...(saved.config?.limits ?? saved.limits) });
    } catch (error) { setConfigError(error instanceof Error ? error.message : "保存配置失败。"); }
    finally { setApiKey(""); setSettingsBusy(false); }
  }
  async function runConnectionTest() {
    if (!configState?.config || !window.confirm("连接检测会向已配置服务发送一次最小工具能力请求，可能产生费用。继续吗？")) return;
    setSettingsBusy(true); setSettingsMessage(null);
    try { const result = await testAgentConfig(configState.config.config_version); setSettingsMessage(`连通性：${result.reachable ? "可达" : "不可达"}；工具调用：${result.tool_supported ? "支持" : "未确认/不支持"}${result.reason ? `；原因：${result.reason}` : ""}`); }
    catch (error) { setConfigError(error instanceof Error ? error.message : "连接检测失败。"); }
    finally { setSettingsBusy(false); }
  }
  async function loadDisclosure() {
    if (!configState?.configured || activeTask) return;
    setBusy("disclosure"); setPanelError(null); setDisclosure(null); setConsented(false);
    try { const next = await getDisclosure(practiceId, session?.revision ?? null); setDisclosure(next); setRequestLimits({ ...next.limits }); }
    catch (error) { setPanelError(error instanceof Error ? error.message : "数据披露摘要获取失败。"); }
    finally { setBusy(null); }
  }
  async function submitGoal(event: React.FormEvent) {
    event.preventDefault();
    if (!disclosure || !consented || !configState?.config || !goal.trim() || inFlight.current || activeTask) return;
    inFlight.current = true; setBusy("create"); setPanelError(null);
    try {
      const body = { practice_id: practiceId, expected_revision: session?.revision ?? null, goal: goal.trim(), config_version: disclosure.config_version, consent: true as const, consent_digest: disclosure.consent_digest, limits: requestLimits };
      const signature = JSON.stringify(body);
      if (!submissionIntent.current || submissionIntent.current.signature !== signature) {
        submissionIntent.current = { signature, key: newKey() };
      }
      // Keep the same key after an ambiguous network failure so a retry replays
      // the server's original task instead of starting a second paid request.
      const task = await createAgentTask({ ...body, idempotency_key: submissionIntent.current.key });
      submissionIntent.current = null;
      setGoal(""); setDisclosure(null); setConsented(false); setSelected(task); setEvents([]); setNextSeq(0); await refreshTasks();
    } catch (error) { setPanelError(error instanceof Error ? error.message : "任务提交失败。"); }
    finally { inFlight.current = false; setBusy(null); }
  }
  async function stopTask(task: AgentTask) {
    setBusy("cancel"); setPanelError(null);
    try { const stopped = await cancelAgentTask(task.task_id, cancelKeyFor(task.task_id)); setSelected(stopped); await refreshTasks(); }
    catch (error) { setPanelError(error instanceof Error ? error.message : "停止任务失败。"); }
    finally { setBusy(null); }
  }
  async function removeTask(task: AgentTask) {
    setBusy("delete"); setPanelError(null);
    try { let current = task; if (ACTIVE.has(current.status)) { current = await cancelAgentTask(current.task_id, cancelKeyFor(current.task_id)); setSelected(current); }
      if (ACTIVE.has(current.status)) throw new Error("取消请求已送达，任务仍在运行；终态后可再次删除。");
      await deleteAgentTask(task.task_id); if (selected?.task_id === task.task_id) setSelected(null); await refreshTasks();
    } catch (error) { setPanelError(error instanceof Error ? error.message : "删除任务失败。"); }
    finally { setBusy(null); }
  }
  async function prepareTrade(draft: AgentDraft) {
    setBusy("draft"); setPanelError(null); setTradeReview(null);
    try {
      if (!session || draft.practice_id !== session.practice_id || !isTradePayload(draft.payload)) throw new Error("草稿绑定或字段无效，请重新试算。");
      const current = await fetchSession(session.practice_id);
      if (current.revision !== draft.expected_revision || current.revision !== session.revision || current.revealed_date !== draft.revealed_date || !isRunning(current.status)) throw new Error("模拟日期或版本已变化，草稿过期。请重新试算后再确认。");
      const input = { ts_code: String(draft.payload.ts_code), direction: String(draft.payload.direction), planned_shares: Number(draft.payload.planned_shares) };
      const preview = await previewOrder(session.practice_id, input); setTradeReview({ draft, preview });
    } catch (error) { setPanelError(error instanceof Error ? error.message : "草稿试算失败。"); }
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
    <label>Base URL<input type="url" value={draftConfig.base_url} onChange={e => setDraftConfig({ ...draftConfig, base_url: e.target.value })} placeholder="https://…" required /></label>
    <label>模型名称<input value={draftConfig.model} onChange={e => setDraftConfig({ ...draftConfig, model: e.target.value })} required /></label>
    <label>API Key（仅本次内存输入）<input type="password" autoComplete="new-password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configState?.config?.key_configured ? `已配置 ${configState.config.key_masked ?? ""}；留空保留` : "输入服务密钥"} /></label>
    <div className="agent-limit-grid">{(["rounds", "tool_calls", "timeout_seconds", "tokens"] as const).map(key => <label key={key}>{({ rounds: "模型轮次", tool_calls: "工具次数", timeout_seconds: "时限（秒）", tokens: "Token预算" })[key]}<input type="number" min="1" max={HARD_LIMITS[key]} value={limits[key]} onChange={e => setLimits({ ...limits, [key]: Math.min(HARD_LIMITS[key], Math.max(1, Number(e.target.value) || 1)) })} /></label>)}</div>
    <label className="agent-check"><input type="checkbox" checked={draftConfig.allow_loopback_http} onChange={e => setDraftConfig({ ...draftConfig, allow_loopback_http: e.target.checked })} />允许本机回环 HTTP</label>
    <label className="agent-check"><input type="checkbox" checked={draftConfig.trusted_private_https} onChange={e => setDraftConfig({ ...draftConfig, trusted_private_https: e.target.checked })} />信任私有 HTTPS 目标</label>
    {configState?.config ? <p className="agent-muted">当前密钥：{configState.config.key_configured ? `已配置 ${configState.config.key_masked ?? ""}` : "未配置"} · 当前任务使用的版本不会随设置更改。</p> : null}
    <div className="agent-actions"><button className="practice-primary" type="submit" disabled={settingsBusy}>{settingsBusy ? "保存中…" : "保存配置"}</button><button className="refresh-button" type="button" onClick={() => void runConnectionTest()} disabled={!configState?.configured || settingsBusy}>检测连接与工具能力</button></div>
    {settingsMessage ? <p className="agent-success" role="status">{settingsMessage}</p> : null}
  </form>;

  return <section className="agent-panel" aria-labelledby="agent-heading">
    <header className="agent-panel-header"><div><p className="eyebrow">Agent 任务台 · {scope === "setup" ? "新模拟条件" : "当前历史模拟"}</p><h2 id="agent-heading">把目标交给 Agent 查证与整理</h2><p className="agent-muted">工具进度、证据、结论与待确认草稿集中显示。Agent 不会自动下单、推进日期或创建模拟。</p></div><div className="agent-header-actions"><span className={`agent-config-state ${configState?.configured ? "is-ready" : ""}`}>{configState?.configured ? `已配置 · ${configState.config?.name}` : configError ? "配置不可用" : "尚未配置模型"}</span><button type="button" className="refresh-button" onClick={() => { setSettingsOpen(value => !value); setSettingsMessage(null); }}>{settingsOpen ? "收起设置" : "模型设置"}</button></div></header>
    {settingsOpen ? <div className="agent-settings"><h3>本地模型服务</h3><p>支持 Chat Completions 工具调用。连接检测会产生一次显式请求，可能收费；应用密钥与开发 CLI 登录无关。</p>{settingsForm}</div> : null}
    {configError ? <Notice tone="warn">{configError}。无模型时仍可直接使用历史模拟。</Notice> : null}
    {session ? <p className="agent-context-line">当前模拟：{session.name} · 修订 {session.revision} · 已揭晓至 {session.revealed_date ?? "尚无"} · 数据源 {session.source.code}（{session.source.version_id || "版本未知"}）</p> : <p className="agent-context-line">新模拟草稿范围：仅整理创建条件；不会查询账户/行情，也不会代替你提交创建。</p>}
    {!configState?.configured ? <div className="agent-empty"><strong>配置后即可创建真实 Agent 任务</strong><span>先在模型设置中填写服务目标与密钥。没有模型也能继续手动模拟。</span><button className="refresh-button" type="button" onClick={() => setSettingsOpen(true)}>配置模型</button></div> : null}
    {configState?.configured ? <form className="agent-task-form" onSubmit={submitGoal}>
      <label htmlFor="agent-goal">本次目标</label><textarea id="agent-goal" value={goal} onChange={e => setGoal(e.target.value)} maxLength={1200} rows={3} placeholder={scope === "setup" ? "例如：整理一个面向消费行业的历史模拟条件草稿" : "例如：核对当前持仓在已揭晓区间的主要风险，并引用可查证依据"} disabled={Boolean(activeTask)} />
      <div className="agent-form-footer"><div><button type="button" className="refresh-button" onClick={() => void loadDisclosure()} disabled={Boolean(activeTask || busy || !goal.trim())}>{busy === "disclosure" ? "读取披露摘要…" : disclosure ? "刷新外发摘要" : "查看本次外发内容"}</button><span>仅读取摘要，不会发送模型请求。</span></div><button type="submit" className="practice-primary" disabled={!disclosure || !consented || !goal.trim() || Boolean(activeTask || busy)}>{busy === "create" ? "提交中…" : "同意并开始任务"}</button></div>
      {disclosure ? <div className="agent-disclosure"><h3>本次数据披露与上限</h3><dl><div><dt>提供方 / 目标</dt><dd>{disclosure.target.name} · {disclosure.target.base_url} · {disclosure.target.model}</dd></div><div><dt>用途</dt><dd>{disclosure.purpose}</dd></div><div><dt>可能收费</dt><dd>{disclosure.possible_cost ? "是，取决于服务商" : "服务端标记为否"}</dd></div><div><dt>任务限制</dt><dd>{disclosure.limits.rounds} 轮 · {disclosure.limits.tool_calls} 次工具 · {disclosure.limits.timeout_seconds} 秒 · {disclosure.limits.tokens} Token（可在配置中调低）</dd></div></dl><div><strong>将发送的数据类别</strong><ul>{disclosure.data_categories.map(item => <li key={item}>{item}</li>)}</ul></div><label className="agent-consent"><input type="checkbox" checked={consented} onChange={e => setConsented(e.target.checked)} />我已阅读上述目标、用途、数据类别、收费可能与任务上限，同意发送本次任务所需的最小数据。</label></div> : null}
    </form> : null}
    {panelError ? <Notice tone="error">{panelError}</Notice> : null}
    <div className="agent-columns"><div className="agent-history"><div className="agent-subhead"><h3>任务历史</h3><button className="link-button" type="button" onClick={() => void refreshTasks()}>刷新</button></div>{listError ? <p className="agent-muted">{listError}</p> : taskList.length === 0 ? <p className="agent-muted">当前模拟暂无 Agent 任务。</p> : <ul>{taskList.map(task => <li key={task.task_id}><button type="button" className={selected?.task_id === task.task_id ? "agent-task-link selected" : "agent-task-link"} onClick={() => openTask(task)}><strong>{task.goal}</strong><span>{STATUS[task.status] ?? task.status} · {new Date(task.created_at).toLocaleString()}</span></button><button type="button" className="link-button" aria-label={`删除任务 ${task.goal}`} onClick={() => void removeTask(task)} disabled={Boolean(busy)}>删除</button></li>)}</ul>}</div>
    {selected ? <article className="agent-task-detail"><header><div><span className={`agent-status status-${selected.status}`}>{STATUS[selected.status] ?? selected.status}</span><h3>{selected.goal}</h3></div><div className="agent-actions">{ACTIVE.has(selected.status) ? <button type="button" className="refresh-button" onClick={() => void stopTask(selected)} disabled={Boolean(busy)}>停止任务</button> : null}<button type="button" className="link-button" onClick={() => void removeTask(selected)} disabled={Boolean(busy)}>删除记录</button></div></header>
      {selected.stale ? <Notice tone="warn">绑定模拟版本已过期；此结果仅供历史查看，草稿不能提交。</Notice> : null}
      <div className="agent-usage"><span>输入 Token：{fmt(selected.usage.input_tokens)}</span><span>输出 Token：{fmt(selected.usage.output_tokens)}</span><span>合计：{fmt(selected.usage.total_tokens)}</span><span>模型调用：{selected.usage.model_calls}</span><span>工具调用：{selected.usage.tool_calls}</span><span>用时：{fmt(selected.usage.elapsed_ms, " ms")}</span><span>费用估算：{selected.usage.estimated_cost === null ? "未知" : String(selected.usage.estimated_cost)}</span></div>
      {selected.stop_reason ? <p className="agent-stop-reason">停止原因：{selected.stop_reason}</p> : null}
      <div className="agent-steps"><h4>工具步骤与状态</h4>{events.length ? <ol>{events.map(item => <li key={item.seq}><span>{KIND[item.kind] ?? item.kind}</span>{item.tool_name ? <code>{item.tool_name}</code> : null}<p>{item.summary}</p>{item.reason ? <small>{item.reason}</small> : null}{item.evidence_ids.length ? <small>证据：{item.evidence_ids.join("、")}</small> : null}</li>)}</ol> : <p className="agent-muted">{ACTIVE.has(selected.status) ? "等待服务端事件…" : "没有事件记录。"}</p>}</div>
      {selected.result ? <div className="agent-result"><h4>结论</h4><p className="agent-result-text">{selected.result.summary}</p>{selected.result.limitations.length ? <div><h4>数据与结论限制</h4><ul>{selected.result.limitations.map((item, i) => <li key={`${i}-${item}`}>{item}</li>)}</ul></div> : null}
        {selected.result.evidence.length ? <div><h4>证据引用</h4><div className="agent-evidence-list">{selected.result.evidence.map((item, i) => { const data = item.data && typeof item.data === "object" && !Array.isArray(item.data) ? item.data as Record<string, unknown> : null; return <details key={item.evidence_id ?? i}><summary>{item.tool_name ?? "工具结果"} · {item.as_of ?? "日期未知"} · {item.evidence_id ?? `证据 ${i + 1}`}</summary><p>{item.summary ?? "核对证据摘要与固定版本引用。"}</p><dl><div><dt>版本</dt><dd>{item.version_id ?? stringify(item.version)}</dd></div>{data ? Object.entries(data).slice(0, 30).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{evidenceValue(key, value)}</dd></div>) : null}</dl>{!data && item.data !== undefined ? <pre>{stringify(item.data)}</pre> : null}</details>; })}</div></div> : null}
        {selected.result.drafts.length ? <div><h4>待确认草稿</h4>{selected.result.drafts.map(draft => <article className="agent-draft" key={draft.draft_id}><strong>{draft.kind === "trade" ? "交易草稿" : "新模拟条件草稿"}</strong><pre>{stringify(draft.payload)}</pre>{draft.limitations.length ? <ul>{draft.limitations.map(item => <li key={item}>{item}</li>)}</ul> : null}{draft.kind === "setup" ? <button type="button" className="refresh-button" onClick={() => onSetupDraft?.(draft.payload)} disabled={!onSetupDraft || selected.stale}>填入创建模拟表单</button> : <button type="button" className="refresh-button" onClick={() => void prepareTrade(draft)} disabled={!session || selected.stale || busy === "draft" || !isTradePayload(draft.payload)}>核对并预览订单</button>}</article>)}</div> : null}
      </div> : null}
      {tradeReview ? <div className="agent-trade-review"><h4>既有订单接口预览</h4><p>{tradeReview.draft.payload.direction === "buy" ? "买入" : "卖出"} {String(tradeReview.draft.payload.ts_code)} · {String(tradeReview.draft.payload.planned_shares)} 股</p><p>模拟日 {session?.revealed_date ?? "未知"} · 修订 {session?.revision ?? "未知"}</p><pre>{stringify(tradeReview.preview)}</pre><p>请在确认前检查系统预览与限制。确认只提交这笔订单，不推进模拟。</p><div className="agent-actions"><button className="practice-primary" type="button" disabled={Boolean(busy) || selected.stale} onClick={() => void submitTrade()}>{busy === "order" ? "提交中…" : "确认提交订单"}</button><button type="button" className="link-button" onClick={() => setTradeReview(null)} disabled={Boolean(busy)}>取消</button></div></div> : null}
      </article> : <div className="agent-task-detail agent-empty"><strong>选择任务查看步骤、结论和引用证据</strong><span>任务终态后停止轮询；历史任务可查看或删除。</span></div>}</div>
  </section>;
}
