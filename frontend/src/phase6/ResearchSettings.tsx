import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { cancelExperiment, createExperimentPlan, getExperiment, getResearchRecord, getResearchSourceContext, listExperiments, newIdempotencyKey, resumeExperiment, reviseResearchRecord, saveResearchRecord, searchResearch, startExperiment } from "./client";
import { fetchSession, fetchSessions } from "../practice/client";
import type { PracticeSummary, PracticeView } from "../practice/types";
import type { ExperimentPlan, OperationView, ResearchContextAvailability, ResearchRecord, ResearchSourceContext } from "./types";
import { CredentialsSettings } from "./CredentialsSettings";
import "../agent-research.css";

function message(e: unknown) { return e instanceof Error ? e.message : "请求失败，请重试。"; }
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const statusLabel: Record<string, string> = { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
type ExperimentView = OperationView & {
  result?: Record<string, unknown> | null;
  attempts?: Array<{ attempt: number; generation: number; status: string; checkpoint: Record<string, unknown>; created_at: string }>;
};
function scopeLabel(scope?: string) { return scope === "simulation" ? "指定模拟" : scope === "global" ? "全局研究" : "范围未知"; }
function metricText(metrics: Record<string, unknown>, key: string, unit: "bps" | "cents" | "count") {
  const value = metrics[key];
  if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
  if (unit === "bps") return `${(value / 100).toFixed(2)}%`;
  if (unit === "cents") return `${(value / 100).toFixed(2)} 元`;
  return value === 0 ? "0 笔成交" : `${value} 笔`;
}
function ResearchExperimentSummary({ experiment }: { experiment: ExperimentView }) {
  const result = experiment.result ?? {};
  const training = (Array.isArray(result.training_candidates) ? result.training_candidates : Array.isArray(result.completed_training_candidates) ? result.completed_training_candidates : []) as Array<Record<string, unknown>>;
  const evaluation = result.evaluation && typeof result.evaluation === "object" ? result.evaluation as Record<string, unknown> : null;
  const metrics = (value: unknown) => value && typeof value === "object" ? value as Record<string, unknown> : null;
  return <div className="agent-research-summary">
    <section aria-label="训练结果">
      <h4>训练结果（用于选参）</h4>
      {training.length ? <ul>{training.map((candidate, index) => <li key={index}>候选 {String(candidate.candidate ?? "未知")}：净收益 {metricText(candidate, "return_bps", "bps")} · 最大回撤 {metricText(candidate, "drawdown_bps", "bps")} · 费用 {metricText(candidate, "cost_cents", "cents")} · {metricText(candidate, "trade_count", "count")}</li>)}</ul> : <p>没有已完成的训练候选结果。</p>}
    </section>
    <section aria-label="正式检验结果">
      <h4>正式检验结果</h4>
      {evaluation ? <ul>{(["baseline", "selected"] as const).map(key => { const item = metrics(evaluation[key]); return <li key={key}>{key === "baseline" ? "冻结基线" : "训练后选中方案"}：{item ? `净收益 ${metricText(item, "return_bps", "bps")} · 最大回撤 ${metricText(item, "drawdown_bps", "bps")} · 费用 ${metricText(item, "cost_cents", "cents")} · ${metricText(item, "trade_count", "count")}` : "服务端未提供该项结果"}</li>; })}</ul> : <p role="status">{experiment.status === "failed" ? "实验失败；没有产生正式检验结果。已完成的训练数据不代表实验通过。" : experiment.status === "cancelled" ? "实验已取消；没有正式检验结果。" : "尚未产生或服务端未提供正式检验结果。"}</p>}
      {evaluation ? <p>检验数据来自服务端正式评估字段；数值缺失保持“未知”。</p> : null}
    </section>
    {Array.isArray(result.limitations) && result.limitations.length ? <section><h4>结果限制</h4><ul>{result.limitations.map((item, index) => <li key={index}>{String(item)}</li>)}</ul></section> : null}
    <details className="agent-research-technical"><summary>查看原始服务端结果</summary><pre className="phase6-json">{JSON.stringify(result, null, 2)}</pre></details>
    <details className="agent-research-technical"><summary>查看每次 attempt（{experiment.attempts?.length ?? 0}）</summary>{experiment.attempts?.length ? <ul className="agent-research-attempts">{experiment.attempts.map(item => <li key={item.attempt}>Attempt {item.attempt} · {statusLabel[item.status] ?? item.status} · {item.created_at}<details><summary>检查点详情</summary><pre className="phase6-json">{JSON.stringify(item.checkpoint, null, 2)}</pre></details></li>)}</ul> : <p>服务端未返回 attempt 明细。</p>}</details>
  </div>;
}
function Pair({ label, children }: { label: string; children: unknown }) { return <div><dt>{label}</dt><dd>{typeof children === "string" ? children : JSON.stringify(children)}</dd></div>; }
function dateRange(dates?: string[]) { return dates?.length ? `${dates[0]}～${dates[dates.length - 1]}（${dates.length} 个交易日）` : "无"; }
function syntheticTrainingDays(context: ResearchSourceContext, endDate: string) {
  const parse = (value: string) => value.length === 8 ? new Date(`${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}T00:00:00Z`) : new Date(`${value}T00:00:00Z`);
  const day = parse(context.revealed_date); day.setUTCDate(day.getUTCDate() + 1);
  const end = parse(endDate); const result: string[] = [];
  while (day <= end && result.length < 5) {
    if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) result.push(`${day.getUTCFullYear()}${String(day.getUTCMonth() + 1).padStart(2, "0")}${String(day.getUTCDate()).padStart(2, "0")}`);
    day.setUTCDate(day.getUTCDate() + 1);
  }
  return result.join(", ");
}
const missingContextLabel: Record<string, string> = { practice_id: "练习 ID", revision: "练习修订", revealed_date: "已揭晓日期", data_version_id: "数据版本", source_code: "数据来源", source_instance_id: "来源实例指纹", settings_fingerprint: "模拟设置指纹", algorithm_fingerprint: "算法规则指纹", fee_fingerprint: "费用规则指纹", segment_fingerprint: "模拟分段指纹" };

function ResearchPanel() {
  const [query, setQuery] = useState(""); const [records, setRecords] = useState<ResearchRecord[]>([]); const [selected, setSelected] = useState<ResearchRecord | null>(null);
  const [goal, setGoal] = useState(""); const [hypothesis, setHypothesis] = useState(""); const [disconfirming, setDisconfirming] = useState(""); const [scope, setScope] = useState("global"); const [evidenceAsOf, setEvidenceAsOf] = useState(""); const [sourceRefsJson, setSourceRefsJson] = useState("[]"); const [revisionText, setRevisionText] = useState(""); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false);
  const [experiment, setExperiment] = useState<ExperimentView | null>(null); const [experimentList, setExperimentList] = useState<OperationView[]>([]);
  const [practiceList, setPracticeList] = useState<PracticeSummary[]>([]); const [practice, setPractice] = useState<PracticeView | null>(null); const [practiceId, setPracticeId] = useState(""); const [sourceContext, setSourceContext] = useState<ResearchContextAvailability | null>(null); const [loadingContext, setLoadingContext] = useState(false); const [timeScopedSearch, setTimeScopedSearch] = useState(false);
  const [plan, setPlan] = useState<ExperimentPlan | null>(null); const [trainingDates, setTrainingDates] = useState(""); const [candidates, setCandidates] = useState("5"); const [baseline, setBaseline] = useState("10"); const [budgetSeconds, setBudgetSeconds] = useState("180");
  const currentPracticeId = useRef(practiceId);
  const currentSourceContext = useRef(sourceContext);
  const contextRequestSeq = useRef(0);
  const searchRequestSeq = useRef(0);
  const recordRequestSeq = useRef(0);
  const experimentRequestSeq = useRef(0);
  const experimentMutationInFlight = useRef(false);
  // A read-only refresh or reopening a record does not start a new write.
  // Keep each unresolved request identity until that exact request succeeds.
  const planIntents = useRef(new Map<string, string>());
  const experimentIntents = useRef(new Map<string, { signature: string; key: string }>());
  currentPracticeId.current = practiceId;
  currentSourceContext.current = sourceContext;
  const hasCurrentSourceContext = Boolean(practiceId && practice?.practice_id === practiceId && sourceContext?.availability === "available" && sourceContext.source_context.practice_id === practiceId && sourceContext.source_context.revision === practice.revision);
  const recordScopeKey = JSON.stringify([timeScopedSearch, practiceId, practice?.revision, sourceContext?.availability === "available" ? sourceContext.source_context.revealed_date : null]);
  const recordScopeRef = useRef(recordScopeKey);
  recordScopeRef.current = recordScopeKey;
  function recordVisibility() {
    if (!timeScopedSearch) return undefined;
    if (!hasCurrentSourceContext || sourceContext?.availability !== "available") throw new Error("先读取当前模拟的来源上下文，才能查看该时点的研究详情。");
    return { practiceId, asOf: sourceContext.source_context.revealed_date };
  }
  const load = useCallback(async () => {
    const requestSeq = ++searchRequestSeq.current;
    const selectedPracticeId = practiceId;
    const contextAtStart = sourceContext;
    setError("");
    setRecords([]);
    const contextMatches = Boolean(selectedPracticeId && contextAtStart?.availability === "available" && contextAtStart.source_context.practice_id === selectedPracticeId && practice?.practice_id === selectedPracticeId && contextAtStart.source_context.revision === practice.revision);
    if (timeScopedSearch && !contextMatches) return;
    const visibility = timeScopedSearch && contextMatches && contextAtStart?.availability === "available" ? { practiceId: selectedPracticeId, asOf: contextAtStart.source_context.revealed_date } : undefined;
    try {
      const result = await searchResearch(query, visibility);
      const current = currentSourceContext.current;
      const stillSameScope = !visibility || Boolean(current?.availability === "available" && current.source_context.practice_id === selectedPracticeId && current.source_context.revealed_date === visibility.asOf && current.source_context.revision === practice?.revision);
      if (requestSeq === searchRequestSeq.current && currentPracticeId.current === selectedPracticeId && stillSameScope) setRecords(result.records);
    } catch (reason) {
      if (requestSeq === searchRequestSeq.current && currentPracticeId.current === selectedPracticeId) setError(message(reason));
    }
  }, [practice, practiceId, query, sourceContext, timeScopedSearch]);
  const loadExperiments = useCallback(async () => { try { setExperimentList((await listExperiments()).experiments); } catch (e) { setError(message(e)); } }, []);
  const loadPractices = useCallback(async () => { setError(""); try { const result = await fetchSessions(100, 0); setPracticeList(result.sessions); if (!currentPracticeId.current && result.sessions[0]) { currentPracticeId.current = result.sessions[0].practice_id; setPracticeId(result.sessions[0].practice_id); } } catch (e) { setError(message(e)); } }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadExperiments(); }, [loadExperiments]);
  useEffect(() => { void loadPractices(); }, [loadPractices]);
  useEffect(() => { if (!experiment || terminal.has(experiment.status)) return; const requestSeq = experimentRequestSeq.current; let timer = 0; let stopped = false; const poll = async () => { try { const next = await getExperiment(experiment.operation_id); if (!stopped && requestSeq === experimentRequestSeq.current) setExperiment(next); } catch (reason) { if (!stopped && requestSeq === experimentRequestSeq.current) setError(message(reason)); } if (!stopped && requestSeq === experimentRequestSeq.current) timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000); }; timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000); return () => { stopped = true; window.clearTimeout(timer); }; }, [experiment?.operation_id, experiment?.status, experiment?.updated_at]);
  async function refreshSourceContext(id = practiceId) {
    if (currentPracticeId.current !== id) return;
    const requestSeq = ++contextRequestSeq.current;
    searchRequestSeq.current += 1; recordRequestSeq.current += 1; experimentRequestSeq.current += 1;
    setPractice(null); setSourceContext(null); setLoadingContext(Boolean(id)); setTrainingDates(""); setRecords([]); setSelected(null); setPlan(null); setExperiment(null); setError(""); setNotice("");
    if (!id) { setLoadingContext(false); return; }
    try {
      const nextPractice = await fetchSession(id);
      if (requestSeq !== contextRequestSeq.current || currentPracticeId.current !== id) return;
      if (nextPractice.practice_id !== id) throw new Error("服务端返回的练习与当前选择不一致，请刷新后重试。");
      setPractice(nextPractice);
      const nextContext = await getResearchSourceContext(nextPractice.practice_id, nextPractice.revision);
      if (requestSeq !== contextRequestSeq.current || currentPracticeId.current !== id) return;
      if (nextContext.availability === "available" && nextContext.source_context.practice_id !== id) throw new Error("研究来源上下文与当前选择的练习不一致，已拒绝使用。");
      setSourceContext(nextContext);
      setTrainingDates(nextContext.availability === "available" && nextContext.source_context.source_code === "synthetic" ? syntheticTrainingDays(nextContext.source_context, nextPractice.end_date) : "");
    } catch (reason) {
      if (requestSeq === contextRequestSeq.current && currentPracticeId.current === id) { setPractice(null); setSourceContext(null); setError(message(reason)); }
    } finally {
      if (requestSeq === contextRequestSeq.current && currentPracticeId.current === id) setLoadingContext(false);
    }
  }
  function choosePractice(id: string) {
    if (currentPracticeId.current === id) return;
    currentPracticeId.current = id;
    contextRequestSeq.current += 1; searchRequestSeq.current += 1; recordRequestSeq.current += 1; experimentRequestSeq.current += 1;
    setPracticeId(id); setPractice(null); setSourceContext(null); setLoadingContext(Boolean(id)); setTrainingDates(""); setRecords([]); setSelected(null); setPlan(null); setExperiment(null); setError(""); setNotice("");
  }
  useEffect(() => { if (practiceId) void refreshSourceContext(practiceId); else { setPractice(null); setSourceContext(null); } }, [practiceId]);
  async function openRecord(id: string) {
    const requestSeq = ++recordRequestSeq.current;
    const requestedScope = recordScopeKey;
    setSelected(null); setPlan(null); setError("");
    try {
      const record = await getResearchRecord(id, recordVisibility());
      if (requestSeq === recordRequestSeq.current && requestedScope === recordScopeRef.current) setSelected(record);
    } catch (reason) { if (requestSeq === recordRequestSeq.current && requestedScope === recordScopeRef.current) setError(message(reason)); }
  }
  async function saveRecord(e: FormEvent) {
    e.preventDefault();
    const requestSeq = ++recordRequestSeq.current;
    const requestedScope = recordScopeKey;
    const stillCurrent = () => requestSeq === recordRequestSeq.current && requestedScope === recordScopeRef.current;
    setBusy(true); setError("");
    try {
      const visibility = recordVisibility();
      let refs: unknown;
      try { refs = JSON.parse(sourceRefsJson); } catch { throw new Error("来源引用必须是合法 JSON 数组；没有引用时填写 []。"); }
      if (!Array.isArray(refs)) throw new Error("来源引用必须是 JSON 数组。");
      const record = await saveResearchRecord({ kind: "question", goal, hypothesis, disconfirming_condition: disconfirming, scope, evidence_as_of: evidenceAsOf.replaceAll("-", ""), source_refs: refs, symbols: [], limitations: [] });
      if (!stillCurrent()) return;
      setNotice("研究记录已保存；只有登记的真实引用会作为证据展示。");
      setGoal(""); setHypothesis(""); setDisconfirming(""); setSourceRefsJson("[]");
      setSelected(null); setPlan(null);
      let visibleRecord: ResearchRecord | null = null;
      try { visibleRecord = await getResearchRecord(record.record_id, visibility); }
      catch (reason) {
        if (!visibility || !message(reason).includes("record_not_visible")) throw reason;
        if (stillCurrent()) setNotice("研究记录已保存，但不属于当前模拟时点可见的记录；可切换全局检索查看。");
      }
      if (!stillCurrent()) return;
      await load();
      if (stillCurrent()) { setSelected(visibleRecord); recordRequestSeq.current += 1; }
    } catch (reason) { if (stillCurrent()) setError(message(reason)); }
    finally { setBusy(false); }
  }
  async function revise(e: FormEvent) {
    e.preventDefault(); if (!selected) return;
    const requestSeq = ++recordRequestSeq.current;
    const requestedScope = recordScopeKey;
    const recordId = selected.record_id;
    const stillCurrent = () => requestSeq === recordRequestSeq.current && requestedScope === recordScopeRef.current;
    setBusy(true); setError("");
    try {
      const visibility = recordVisibility();
      await reviseResearchRecord(recordId, { ...(selected.body ?? {}), kind: selected.kind, goal: selected.goal, hypothesis: selected.hypothesis, disconfirming_condition: selected.disconfirming_condition, scope: selected.scope, evidence_as_of: selected.evidence_as_of, source_refs: selected.source_refs ?? [], revision_note: revisionText });
      if (!stillCurrent()) return;
      setRevisionText(""); setPlan(null);
      const updated = await getResearchRecord(recordId, visibility);
      if (!stillCurrent()) return;
      await load();
      if (stillCurrent()) {
        setSelected(updated); recordRequestSeq.current += 1;
        if (visibility) setNotice("修订已保存；当前详情仍仅显示该模拟时点可见的修订与历史。");
      }
    } catch (reason) { if (stillCurrent()) setError(message(reason)); }
    finally { setBusy(false); }
  }
  async function selectExperiment(id: string) { const requestSeq = ++experimentRequestSeq.current; setError(""); setExperiment(null); try { const next = await getExperiment(id); if (requestSeq === experimentRequestSeq.current) setExperiment(next); } catch (reason) { if (requestSeq === experimentRequestSeq.current) setError(message(reason)); } }
  async function freezePlan(e: FormEvent) {
    e.preventDefault();
    if (experimentMutationInFlight.current) return;
    if (!selected || !hasCurrentSourceContext || sourceContext?.availability !== "available" || !practice || practice.practice_id !== practiceId || sourceContext.source_context.practice_id !== practiceId || sourceContext.source_context.revision !== practice.revision) return;
    experimentMutationInFlight.current = true;
    setBusy(true); setError(""); setNotice("");
    const selectedPracticeId = practiceId;
    const contextSeq = contextRequestSeq.current;
    const recordSeq = recordRequestSeq.current;
    try {
      const dates = trainingDates.split(/[，,\s]+/).filter(Boolean);
      if (!dates.length || dates.some(date => !/^\d{8}$/.test(date)) || dates.some((date, index) => index > 0 && date <= dates[index - 1])) throw new Error("训练交易日必须是升序、唯一的 YYYYMMDD 列表；只提交源数据版本中的交易日。");
      if (dates[0] <= sourceContext.source_context.revealed_date) throw new Error("训练日期必须晚于当前练习的已揭晓日期。");
      const candidateValues = candidates.split(/[，,\s]+/).filter(Boolean).map(Number);
      const baselineValue = Number(baseline); const seconds = Number(budgetSeconds);
      if (candidateValues.some(value => !Number.isInteger(value)) || !Number.isInteger(baselineValue) || !Number.isInteger(seconds)) throw new Error("候选参数、基线和预算必须为整数。");
      const requestBody = {
        record_id: selected.record_id, revision_id: selected.revision_id,
        engine_id: "practice_deterministic_v1",
        source_context: sourceContext.source_context,
        training_dates: dates, evaluation_dates: undefined, warmup_dates: [],
        parameter_name: "rebalance_every_n_days", candidates: candidateValues,
        baseline_value: baselineValue, primary_metric: "net_return_bps",
        selection_rule: "highest_net_return_bps", risk_constraints: {},
        budget: { wall_seconds: seconds, max_bytes: 100 * 1024 * 1024 },
      };
      const signature = JSON.stringify(requestBody);
      let key = planIntents.current.get(signature);
      if (!key) { key = newIdempotencyKey(); planIntents.current.set(signature, key); }
      const frozen = await createExperimentPlan({ ...requestBody, idempotency_key: key });
      if (planIntents.current.get(signature) === key) planIntents.current.delete(signature);
      if (contextSeq === contextRequestSeq.current && recordSeq === recordRequestSeq.current && currentPracticeId.current === selectedPracticeId && selectedPracticeId === practiceId) {
        setPlan(frozen); setNotice("服务端已冻结方案。请核对源上下文、训练/评估日期与预算，再明确启动隔离实验。");
      } else {
        setNotice("服务端已冻结提交中的方案，但当前练习或研究记录已变化；未启动实验，请重新核对。");
      }
    } catch (reason) { if (contextSeq === contextRequestSeq.current && recordSeq === recordRequestSeq.current) setError(message(reason)); }
    finally { experimentMutationInFlight.current = false; setBusy(false); }
  }
  async function launch() {
    if (experimentMutationInFlight.current) return;
    if (!plan || !hasCurrentSourceContext || !sourceContext || sourceContext.availability !== "available" || !practice || practice.practice_id !== practiceId || sourceContext.source_context.practice_id !== practiceId || sourceContext.source_context.revision !== practice.revision || String(plan.source_context?.practice_id ?? "") !== practiceId || String(plan.source_context?.revision ?? "") !== String(sourceContext.source_context.revision)) return;
    experimentMutationInFlight.current = true;
    setBusy(true); setError("");
    const selectedPracticeId = practiceId;
    const contextSeq = contextRequestSeq.current;
    const planId = plan.plan_id;
    const planRevision = plan.plan_revision;
    const requestSeq = ++experimentRequestSeq.current;
    const signature = JSON.stringify([planId, planRevision]);
    const previous = experimentIntents.current.get(planId);
    const intent = previous?.signature === signature ? previous : { signature, key: newIdempotencyKey() };
    experimentIntents.current.set(planId, intent);
    try {
      const operation = await startExperiment(planId, planRevision, intent.key);
      if (experimentIntents.current.get(planId) === intent) experimentIntents.current.delete(planId);
      const current = currentSourceContext.current;
      if (requestSeq === experimentRequestSeq.current && contextSeq === contextRequestSeq.current && currentPracticeId.current === selectedPracticeId && selectedPracticeId === practiceId && current?.availability === "available" && current.source_context.practice_id === selectedPracticeId) {
        setExperiment(operation);
        setNotice("隔离实验已显式提交；不会修改原练习。正在读取服务端 Operation 与结果。");
      } else {
        setNotice("实验启动请求已确认，但当前练习选择已变化；请在实验列表中查看服务端状态。");
      }
      await loadExperiments();
    } catch (reason) { if (requestSeq === experimentRequestSeq.current && contextSeq === contextRequestSeq.current) setError(message(reason)); }
    finally { experimentMutationInFlight.current = false; setBusy(false); }
  }
  async function stop() {
    if (!experiment || experimentMutationInFlight.current) return;
    const requestSeq = ++experimentRequestSeq.current;
    experimentMutationInFlight.current = true; setBusy(true);
    try { const next = await cancelExperiment(experiment.operation_id, experiment.generation); if (requestSeq === experimentRequestSeq.current) setExperiment(next); await loadExperiments(); }
    catch (reason) { if (requestSeq === experimentRequestSeq.current) setError(message(reason)); }
    finally { experimentMutationInFlight.current = false; setBusy(false); }
  }
  async function resume() {
    if (!experiment?.resumable || experimentMutationInFlight.current) return;
    const requestSeq = ++experimentRequestSeq.current;
    experimentMutationInFlight.current = true; setBusy(true);
    try { const next = await resumeExperiment(experiment.operation_id, experiment.generation); if (requestSeq === experimentRequestSeq.current) setExperiment(next); await loadExperiments(); }
    catch (reason) { if (requestSeq === experimentRequestSeq.current) setError(message(reason)); }
    finally { experimentMutationInFlight.current = false; setBusy(false); }
  }
  return <div className="phase6-columns agent-research-columns">
    <section className="phase6-card">
      <h2>研究记录与检索</h2>
      {error ? <p className="phase6-error" role="alert">{error}</p> : null}{notice ? <p className="agent-success" role="status">{notice}</p> : null}
      <label className="practice-field"><span>选择已有历史模拟</span><select value={practiceId} onChange={e => choosePractice(e.target.value)}><option value="">选择模拟</option>{practiceList.map(item => <option key={item.practice_id} value={item.practice_id}>{item.name} · {item.status}</option>)}</select></label>
      {practice ? <div className="practice-field-hint">当前练习已揭晓至 {practice.revealed_date ?? "未知"} · 修订 {practice.revision}<details><summary>查看技术标识</summary><code>{practice.practice_id}</code></details></div> : null}
      <button className="refresh-button" type="button" onClick={() => void refreshSourceContext()} disabled={!practiceId || loadingContext}>{loadingContext ? "读取来源上下文…" : "刷新练习修订与研究来源上下文"}</button>
      {sourceContext?.availability === "available" ? <details><summary>服务端核验的完整十字段 source_context</summary><pre className="phase6-json">{JSON.stringify(sourceContext.source_context, null, 2)}</pre><p className="muted">计划提交时会原样发送以上对象；指纹完全由服务端计算，页面不推导或改写。</p></details> : null}
      {sourceContext?.availability === "supplementation_required" ? <div className="phase6-error" role="status"><strong>该练习需要补录后才能研究。</strong><p>原因：{sourceContext.reason}</p><p>需补录字段：{sourceContext.missing_fields.map(field => missingContextLabel[field] ?? field).join("、") || "服务端未列明"}</p></div> : null}
      <form className="phase6-inline" onSubmit={e => { e.preventDefault(); void load(); }}><input value={query} onChange={e => { searchRequestSeq.current += 1; setRecords([]); setQuery(e.target.value); }} placeholder="检索问题、证据摘要"/><button className="refresh-button" disabled={timeScopedSearch && !hasCurrentSourceContext}>搜索</button></form>
      <label className="practice-field"><span><input type="checkbox" checked={timeScopedSearch} onChange={e => { searchRequestSeq.current += 1; recordRequestSeq.current += 1; setRecords([]); setSelected(null); setPlan(null); setTimeScopedSearch(e.target.checked); }}/>仅显示该练习揭晓时点可见的记录</span></label>
      <p className="muted">{timeScopedSearch && sourceContext?.availability === "available" ? `仅展示不晚于所选模拟揭晓日（${sourceContext.source_context.revealed_date}）的记录；未来证据和修订不进入此结果。` : "当前为全局检索。勾选时点过滤后，仅返回当时已存在且证据日期不晚于练习揭晓日的引用。"}</p>
      {records.length ? <ul className="phase6-list">{records.map(r => <li key={r.record_id}><button className="link-button" type="button" onClick={() => void openRecord(r.record_id)}>{r.goal}</button><span>{scopeLabel(r.scope)} · 证据截至 {r.evidence_as_of ?? "未知"} · {r.created_at}</span></li>)}</ul> : <p className="muted">没有可显示的研究记录，或该时点下没有满足证据时间条件的记录。</p>}
      <details><summary>保存新问题 / 假设</summary><form className="phase6-form" onSubmit={e => void saveRecord(e)}><label className="practice-field"><span>研究问题</span><input required value={goal} onChange={e => setGoal(e.target.value)}/></label><label className="practice-field"><span>假设</span><textarea required value={hypothesis} onChange={e => setHypothesis(e.target.value)}/></label><label className="practice-field"><span>反证条件</span><textarea required value={disconfirming} onChange={e => setDisconfirming(e.target.value)}/></label><label className="practice-field"><span>范围</span><select value={scope} onChange={e => setScope(e.target.value)}><option value="global">全局研究</option><option value="simulation">指定模拟（引用需含 owner_id）</option></select></label><label className="practice-field"><span>证据截至日期（YYYYMMDD）</span><input required inputMode="numeric" pattern="[0-9]{8}" value={evidenceAsOf} onChange={e => setEvidenceAsOf(e.target.value)}/></label><label className="practice-field"><span>来源引用 JSON 数组（仅填真实引用；无引用填 []）</span><textarea value={sourceRefsJson} onChange={e => setSourceRefsJson(e.target.value)}/></label><button className="practice-primary" disabled={busy}>保存研究记录</button></form></details>
      {selected ? <article className="phase6-detail"><h3>{selected.goal}</h3><dl><Pair label="范围">{scopeLabel(selected.scope)}</Pair><Pair label="假设">{selected.hypothesis ?? "未记录"}</Pair><Pair label="反证条件">{selected.disconfirming_condition ?? "未记录"}</Pair><Pair label="证据引用">{selected.source_refs?.length ? `${selected.source_refs.length} 条已登记引用` : "无引用"}</Pair><Pair label="证据截至">{selected.evidence_as_of ?? "未知"}</Pair><Pair label="限制">{selected.limitations ?? []}</Pair></dl>
        <details><summary>查看技术版本、修订历史与引用</summary><dl><Pair label="记录 / 当前修订">{[selected.record_id, selected.revision_id]}</Pair><Pair label="修订历史">{`${JSON.stringify(selected.revision_history ?? [])}（共 ${selected.revision_history_total ?? selected.revision_history?.length ?? 0} 条）`}</Pair><Pair label="来源引用">{selected.source_refs ?? []}</Pair><Pair label="尝试与版本">{selected.attempts ?? selected.versions ?? []}</Pair></dl></details>
        <form className="phase6-form" onSubmit={e => void revise(e)}><label className="practice-field"><span>追加修订说明</span><textarea value={revisionText} onChange={e => setRevisionText(e.target.value)} required/></label><button className="refresh-button" disabled={busy}>追加修订</button></form>
        <details open><summary>冻结并启动隔离实验</summary>
          {sourceContext?.availability !== "available" ? <p className="muted">先选取服务端标记为 available 的模拟来源上下文；缺字段时需补录，页面不会猜指纹。</p> : <form className="phase6-form" onSubmit={e => void freezePlan(e)}>
            <label className="practice-field"><span>训练交易日（YYYYMMDD，逗号分隔）</span><textarea required value={trainingDates} disabled={busy} onChange={e => { setTrainingDates(e.target.value); }}/></label>
            {sourceContext.source_context.source_code === "synthetic" ? <p className="practice-field-hint">这是受限合成练习；预填日期仅按服务端标记的周一至周五合成日历生成。它不表示真实市场交易资格，也可由你核对后修改。</p> : <p className="practice-field-hint">真实来源请从该版本的有效交易日中逐项填写；页面不推算日历。评估区间由服务端从同一版本派生120个后续交易日，数据不足时会拒绝冻结。</p>}
            <div className="phase6-grid"><label className="practice-field"><span>参数候选（5/10/20）</span><input value={candidates} disabled={busy} onChange={e => { setCandidates(e.target.value); }}/></label><label className="practice-field"><span>冻结基线</span><select value={baseline} disabled={busy} onChange={e => { setBaseline(e.target.value); }}><option value="5">5个交易日</option><option value="10">10个交易日</option><option value="20">20个交易日</option></select></label><label className="practice-field"><span>墙钟预算秒数</span><input type="number" min="1" max="300" value={budgetSeconds} disabled={busy} onChange={e => { setBudgetSeconds(e.target.value); }}/></label></div>
            <button className="practice-primary" disabled={busy || !selected}>校验并冻结方案</button>
          </form>}
          {plan ? <div className="phase6-plan"><h4>服务端已冻结方案 · revision {plan.plan_revision}</h4><dl><Pair label="状态">{plan.status}</Pair><Pair label="来源 / 练习修订">{plan.source_context ? `${String(plan.source_context.source_code)} · revision ${String(plan.source_context.revision)}` : "服务端未返回"}</Pair><Pair label="数据版本">{plan.source_context?.data_version_id ? "已绑定服务端数据版本" : "未知"}</Pair><Pair label="训练 / 评估区间">{`训练 ${dateRange(plan.training_dates)}；评估 ${dateRange(plan.evaluation_dates)}`}</Pair><Pair label="参数候选 / 冻结基线">{`${plan.parameter_name ?? "参数"}：${(plan.candidates ?? []).join("、")} / ${plan.baseline_value ?? "无"}`}</Pair><Pair label="指标 / 选择规则">{[plan.primary_metric, plan.selection_rule, plan.risk_constraints]}</Pair><Pair label="冻结预算摘要">{plan.budget ? `墙钟 ${String(plan.budget.wall_seconds)} 秒；磁盘上限 ${String(plan.budget.max_bytes)} bytes；逻辑步数 ${String(plan.budget.estimated_logical_steps)} / ${String(plan.budget.max_logical_steps)}` : "服务端未返回"}</Pair><Pair label="限制">{plan.limitations ?? []}</Pair></dl><details><summary>查看方案编号、摘要与完整冻结上下文</summary><pre className="phase6-json">{JSON.stringify(plan, null, 2)}</pre></details><p>启动仅创建本地隔离实验；不调用模型或行情供应商，也不改变原练习。</p><button className="practice-primary" type="button" disabled={busy || Boolean(experiment && !terminal.has(experiment.status))} onClick={() => { if (window.confirm("明确启动此已冻结方案的本地隔离实验？")) void launch(); }}>明确启动实验</button></div> : null}
        </details>
      </article> : null}
    </section>
    <section className="phase6-card agent-experiment-panel">
      <h2>实验状态与结果</h2>
      <button className="refresh-button" type="button" onClick={() => void loadExperiments()}>刷新全部实验</button>
      {experimentList.length ? <ul className="phase6-list">{experimentList.map(item => <li key={item.operation_id}>
        <button className="link-button" type="button" onClick={() => void selectExperiment(item.operation_id)}>{statusLabel[item.status] ?? item.status} · attempt {item.attempt} · 更新于 {item.updated_at}</button>
        <details><summary>技术标识</summary><code>{item.operation_id}</code></details>
      </li>)}</ul> : <p className="muted">没有实验记录，或研究服务尚未就绪。</p>}
      {!experiment ? <p className="muted">选择一条实验记录查看服务端状态、停止原因和结果。</p> : <>
        <p className={`agent-experiment-status status-${experiment.status}`}><strong>{statusLabel[experiment.status] ?? experiment.status}</strong>{experiment.status === "completed" ? " · 已结束" : ""}</p>
        <p>当前阶段：{experiment.progress.phase} · 已完成 {experiment.progress.completed_units}{experiment.progress.total_units === null ? " 个单位，任务总量未知" : ` / ${experiment.progress.total_units} 个单位`}</p>
        {experiment.status === "failed" ? <p role="status">实验失败。即使有训练结果，也没有因此完成正式检验。</p> : null}
        {experiment.stop_reason ? <p>停止原因：{experiment.stop_reason}</p> : null}
        {experiment.resumable ? <button className="refresh-button" type="button" onClick={() => void resume()} disabled={busy}>恢复实验 · 服务端允许恢复</button> : null}
        {experiment.status === "interrupted" && !experiment.resumable ? <p className="muted">服务端没有标记可恢复，不能续跑。</p> : null}
        {!terminal.has(experiment.status) ? <button className="link-button" type="button" onClick={() => void stop()} disabled={busy}>取消实验</button> : null}
        {experiment.result ? <ResearchExperimentSummary experiment={experiment}/> : <p className="muted">尚未产生结果；费用与交易数量不会推测。</p>}
        <details className="agent-research-technical"><summary>查看 Operation 标识与服务端完整响应</summary><pre className="phase6-json">{JSON.stringify(experiment, null, 2)}</pre></details>
      </>}
    </section>
  </div>;
}

export function ResearchSettings() { const [tab, setTab] = useState<"research" | "credentials">("research"); return <div className="practice-home phase6-page"><section className="practice-hero"><div><p className="eyebrow">阶段 6 · 研究与运行凭据</p><h1>检索证据，冻结方案，再明确启动实验</h1><p className="practice-hero-lead">研究实验使用隔离空间，不会自动影响历史模拟的订单或结果。</p></div></section><div className="phase6-tabs"><button className={tab === "research" ? "practice-primary" : "refresh-button"} onClick={() => setTab("research")}>研究与实验</button><button className={tab === "credentials" ? "practice-primary" : "refresh-button"} onClick={() => setTab("credentials")}>凭据设置</button></div>{tab === "research" ? <ResearchPanel/> : <CredentialsSettings/>}</div>; }
