import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { cancelExperiment, clearCredential, createExperimentPlan, getExperiment, getResearchRecord, getResearchSourceContext, listCredentials, listExperiments, newIdempotencyKey, resumeExperiment, reviseResearchRecord, saveCredential, saveResearchRecord, searchResearch, startExperiment } from "./client";
import { fetchSession, fetchSessions } from "../practice/client";
import type { PracticeSummary, PracticeView } from "../practice/types";
import type { CredentialPublic, ExperimentPlan, OperationView, ResearchContextAvailability, ResearchRecord, ResearchSourceContext } from "./types";

function message(e: unknown) { return e instanceof Error ? e.message : "请求失败，请重试。"; }
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const statusLabel: Record<string, string> = { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
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
  const [experiment, setExperiment] = useState<(OperationView & { result?: Record<string, unknown> | null }) | null>(null); const [experimentList, setExperimentList] = useState<OperationView[]>([]);
  const [practiceList, setPracticeList] = useState<PracticeSummary[]>([]); const [practice, setPractice] = useState<PracticeView | null>(null); const [practiceId, setPracticeId] = useState(""); const [sourceContext, setSourceContext] = useState<ResearchContextAvailability | null>(null); const [loadingContext, setLoadingContext] = useState(false); const [timeScopedSearch, setTimeScopedSearch] = useState(false);
  const [plan, setPlan] = useState<ExperimentPlan | null>(null); const [trainingDates, setTrainingDates] = useState(""); const [candidates, setCandidates] = useState("5"); const [baseline, setBaseline] = useState("10"); const [budgetSeconds, setBudgetSeconds] = useState("180");
  const load = useCallback(async () => { setError(""); try { if (timeScopedSearch && sourceContext?.availability !== "available") throw new Error("先选择可用的练习来源上下文，才能按该模拟时点筛选研究记录。"); const visibility = timeScopedSearch && sourceContext?.availability === "available" ? { practiceId: sourceContext.source_context.practice_id, asOf: sourceContext.source_context.revealed_date } : undefined; const result = await searchResearch(query, visibility); setRecords(result.records); } catch (e) { setError(message(e)); } }, [query, sourceContext, timeScopedSearch]);
  const loadExperiments = useCallback(async () => { try { setExperimentList((await listExperiments()).experiments); } catch (e) { setError(message(e)); } }, []);
  const loadPractices = useCallback(async () => { setError(""); try { const result = await fetchSessions(100, 0); setPracticeList(result.sessions); if (!practiceId && result.sessions[0]) setPracticeId(result.sessions[0].practice_id); } catch (e) { setError(message(e)); } }, [practiceId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadExperiments(); }, [loadExperiments]);
  useEffect(() => { void loadPractices(); }, [loadPractices]);
  useEffect(() => { if (!experiment || terminal.has(experiment.status)) return; let timer = 0; let stopped = false; const poll = async () => { try { const next = await getExperiment(experiment.operation_id); if (!stopped) setExperiment(next); } catch (e) { if (!stopped) setError(message(e)); } if (!stopped) timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000); }; timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000); return () => { stopped = true; window.clearTimeout(timer); }; }, [experiment?.operation_id, experiment?.status, experiment?.updated_at]);
  async function refreshSourceContext(id = practiceId) {
    if (!id) { setPractice(null); setSourceContext(null); return; }
    setLoadingContext(true); setError(""); setNotice(""); setPlan(null);
    try {
      const nextPractice = await fetchSession(id);
      setPractice(nextPractice);
      const nextContext = await getResearchSourceContext(nextPractice.practice_id, nextPractice.revision);
      setSourceContext(nextContext);
      setTrainingDates(nextContext.availability === "available" && nextContext.source_context.source_code === "synthetic" ? syntheticTrainingDays(nextContext.source_context, nextPractice.end_date) : "");
    } catch (e) { setPractice(null); setSourceContext(null); setError(message(e)); }
    finally { setLoadingContext(false); }
  }
  useEffect(() => { if (practiceId) void refreshSourceContext(practiceId); else { setPractice(null); setSourceContext(null); } }, [practiceId]);
  async function openRecord(id: string) { setError(""); try { setSelected(await getResearchRecord(id)); } catch (e) { setError(message(e)); } }
  async function saveRecord(e: FormEvent) { e.preventDefault(); setBusy(true); setError(""); try { let refs: unknown; try { refs = JSON.parse(sourceRefsJson); } catch { throw new Error("来源引用必须是合法 JSON 数组；没有引用时填写 []。"); } if (!Array.isArray(refs)) throw new Error("来源引用必须是 JSON 数组。"); const evidenceDate = evidenceAsOf.replaceAll("-", ""); const record = await saveResearchRecord({ kind: "question", goal, hypothesis, disconfirming_condition: disconfirming, scope, evidence_as_of: evidenceDate, source_refs: refs, symbols: [], limitations: [] }); setNotice("研究记录已保存；只有登记的真实引用会作为证据展示。"); setGoal(""); setHypothesis(""); setDisconfirming(""); setSourceRefsJson("[]"); await load(); setSelected(record); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function revise(e: FormEvent) { e.preventDefault(); if (!selected) return; setBusy(true); setError(""); try { await reviseResearchRecord(selected.record_id, { ...(selected.body ?? {}), kind: selected.kind, goal: selected.goal, hypothesis: selected.hypothesis, disconfirming_condition: selected.disconfirming_condition, scope: selected.scope, evidence_as_of: selected.evidence_as_of, source_refs: selected.source_refs ?? [], revision_note: revisionText }); setRevisionText(""); setSelected(await getResearchRecord(selected.record_id)); await load(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function selectExperiment(id: string) { setError(""); try { setExperiment(await getExperiment(id)); } catch (e) { setError(message(e)); } }
  async function freezePlan(e: FormEvent) {
    e.preventDefault();
    if (!selected || sourceContext?.availability !== "available" || !practice) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const dates = trainingDates.split(/[，,\s]+/).filter(Boolean);
      if (!dates.length || dates.some(date => !/^\d{8}$/.test(date)) || dates.some((date, index) => index > 0 && date <= dates[index - 1])) throw new Error("训练交易日必须是升序、唯一的 YYYYMMDD 列表；只提交源数据版本中的交易日。");
      if (dates[0] <= sourceContext.source_context.revealed_date) throw new Error("训练日期必须晚于当前练习的已揭晓日期。");
      const candidateValues = candidates.split(/[，,\s]+/).filter(Boolean).map(Number);
      const baselineValue = Number(baseline); const seconds = Number(budgetSeconds);
      if (candidateValues.some(value => !Number.isInteger(value)) || !Number.isInteger(baselineValue) || !Number.isInteger(seconds)) throw new Error("候选参数、基线和预算必须为整数。");
      const frozen = await createExperimentPlan({
        record_id: selected.record_id, revision_id: selected.revision_id,
        engine_id: "practice_deterministic_v1",
        source_context: sourceContext.source_context,
        training_dates: dates, evaluation_dates: undefined, warmup_dates: [],
        parameter_name: "rebalance_every_n_days", candidates: candidateValues,
        baseline_value: baselineValue, primary_metric: "net_return_bps",
        selection_rule: "highest_net_return_bps", risk_constraints: {},
        budget: { wall_seconds: seconds, max_bytes: 100 * 1024 * 1024 },
      });
      setPlan(frozen); setNotice("服务端已冻结方案。请核对源上下文、训练/评估日期与预算，再明确启动隔离实验。");
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  async function launch() {
    if (!plan || !sourceContext || sourceContext.availability !== "available") return;
    setBusy(true); setError("");
    try { const operation = await startExperiment(plan.plan_id, plan.plan_revision); setExperiment(operation); setNotice("隔离实验已显式提交；不会修改原练习。正在读取服务端 Operation 与结果。"); await loadExperiments(); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  async function stop() { if (!experiment) return; setBusy(true); try { setExperiment(await cancelExperiment(experiment.operation_id, experiment.generation)); await loadExperiments(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  async function resume() { if (!experiment?.resumable) return; setBusy(true); try { setExperiment(await resumeExperiment(experiment.operation_id, experiment.generation)); await loadExperiments(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <div className="phase6-columns">
    <section className="phase6-card">
      <h2>研究记录与检索</h2>
      {error ? <p className="phase6-error" role="alert">{error}</p> : null}{notice ? <p className="agent-success" role="status">{notice}</p> : null}
      <label className="practice-field"><span>选择已有历史模拟（练习 ID 与 revision 来自服务端）</span><select value={practiceId} onChange={e => setPracticeId(e.target.value)}><option value="">选择模拟</option>{practiceList.map(item => <option key={item.practice_id} value={item.practice_id}>{item.name} · {item.practice_id} · {item.status}</option>)}</select></label>
      {practice ? <p className="practice-field-hint">当前练习：{practice.practice_id} · revision {practice.revision} · 已揭晓至 {practice.revealed_date ?? "未知"}</p> : null}
      <button className="refresh-button" type="button" onClick={() => void refreshSourceContext()} disabled={!practiceId || loadingContext}>{loadingContext ? "读取来源上下文…" : "刷新练习修订与研究来源上下文"}</button>
      {sourceContext?.availability === "available" ? <details><summary>服务端核验的完整十字段 source_context</summary><pre className="phase6-json">{JSON.stringify(sourceContext.source_context, null, 2)}</pre><p className="muted">计划提交时会原样发送以上对象；指纹完全由服务端计算，页面不推导或改写。</p></details> : null}
      {sourceContext?.availability === "supplementation_required" ? <div className="phase6-error" role="status"><strong>该练习需要补录后才能研究。</strong><p>原因：{sourceContext.reason}</p><p>需补录字段：{sourceContext.missing_fields.map(field => missingContextLabel[field] ?? field).join("、") || "服务端未列明"}</p></div> : null}
      <form className="phase6-inline" onSubmit={e => { e.preventDefault(); void load(); }}><input value={query} onChange={e => setQuery(e.target.value)} placeholder="检索问题、证据摘要"/><button className="refresh-button" disabled={timeScopedSearch && sourceContext?.availability !== "available"}>搜索</button></form>
      <label className="practice-field"><span><input type="checkbox" checked={timeScopedSearch} onChange={e => setTimeScopedSearch(e.target.checked)}/>仅显示该练习揭晓时点可见的记录</span></label>
      <p className="muted">{timeScopedSearch && sourceContext?.availability === "available" ? `按 practice_id=${sourceContext.source_context.practice_id} 与 as_of=${sourceContext.source_context.revealed_date} 过滤；未来证据和修订不会显示。` : "当前为全局检索。勾选时点过滤后，仅返回当时已存在且 evidence_as_of 不晚于练习揭晓日的引用。"}</p>
      {records.length ? <ul className="phase6-list">{records.map(r => <li key={r.record_id}><button className="link-button" type="button" onClick={() => void openRecord(r.record_id)}>{r.goal}</button><span>{r.record_id} · 修订 {r.revision_id} · {r.created_at}</span></li>)}</ul> : <p className="muted">没有可显示的研究记录，或该时点下没有满足证据时间条件的记录。</p>}
      <details><summary>保存新问题 / 假设</summary><form className="phase6-form" onSubmit={e => void saveRecord(e)}><label className="practice-field"><span>研究问题</span><input required value={goal} onChange={e => setGoal(e.target.value)}/></label><label className="practice-field"><span>假设</span><textarea required value={hypothesis} onChange={e => setHypothesis(e.target.value)}/></label><label className="practice-field"><span>反证条件</span><textarea required value={disconfirming} onChange={e => setDisconfirming(e.target.value)}/></label><label className="practice-field"><span>范围</span><select value={scope} onChange={e => setScope(e.target.value)}><option value="global">全局研究</option><option value="simulation">指定模拟（引用需含 owner_id）</option></select></label><label className="practice-field"><span>证据截至日期（YYYYMMDD）</span><input required inputMode="numeric" pattern="[0-9]{8}" value={evidenceAsOf} onChange={e => setEvidenceAsOf(e.target.value)}/></label><label className="practice-field"><span>来源引用 JSON 数组（仅填真实引用；无引用填 []）</span><textarea value={sourceRefsJson} onChange={e => setSourceRefsJson(e.target.value)}/></label><button className="practice-primary" disabled={busy}>保存研究记录</button></form></details>
      {selected ? <article className="phase6-detail"><h3>{selected.goal}</h3><dl><Pair label="记录 / 当前修订">{[selected.record_id, selected.revision_id]}</Pair><Pair label="修订历史">{`${JSON.stringify(selected.revision_history ?? [])}（共 ${selected.revision_history_total ?? selected.revision_history?.length ?? 0} 条）`}</Pair><Pair label="范围">{selected.scope ?? "未知"}</Pair><Pair label="假设">{selected.hypothesis ?? "未记录"}</Pair><Pair label="反证条件">{selected.disconfirming_condition ?? "未记录"}</Pair><Pair label="引用">{selected.source_refs ?? []}</Pair><Pair label="证据截至">{selected.evidence_as_of ?? "未知"}</Pair><Pair label="尝试与版本">{selected.attempts ?? selected.versions ?? []}</Pair><Pair label="限制">{selected.limitations ?? []}</Pair></dl>
        <form className="phase6-form" onSubmit={e => void revise(e)}><label className="practice-field"><span>追加修订说明</span><textarea value={revisionText} onChange={e => setRevisionText(e.target.value)} required/></label><button className="refresh-button" disabled={busy}>追加修订</button></form>
        <details open><summary>冻结并启动隔离实验</summary>
          {sourceContext?.availability !== "available" ? <p className="muted">先选取服务端标记为 available 的模拟来源上下文；缺字段时需补录，页面不会猜指纹。</p> : <form className="phase6-form" onSubmit={e => void freezePlan(e)}>
            <label className="practice-field"><span>训练交易日（YYYYMMDD，逗号分隔）</span><textarea required value={trainingDates} onChange={e => setTrainingDates(e.target.value)}/></label>
            {sourceContext.source_context.source_code === "synthetic" ? <p className="practice-field-hint">这是受限合成练习；预填日期仅按服务端标记的周一至周五合成日历生成。它不表示真实市场交易资格，也可由你核对后修改。</p> : <p className="practice-field-hint">真实来源请从该版本的有效交易日中逐项填写；页面不推算日历。评估区间由服务端从同一版本派生120个后续交易日，数据不足时会拒绝冻结。</p>}
            <div className="phase6-grid"><label className="practice-field"><span>参数候选（5/10/20）</span><input value={candidates} onChange={e => setCandidates(e.target.value)}/></label><label className="practice-field"><span>冻结基线</span><select value={baseline} onChange={e => setBaseline(e.target.value)}><option value="5">5个交易日</option><option value="10">10个交易日</option><option value="20">20个交易日</option></select></label><label className="practice-field"><span>墙钟预算秒数</span><input type="number" min="1" max="300" value={budgetSeconds} onChange={e => setBudgetSeconds(e.target.value)}/></label></div>
            <button className="practice-primary" disabled={busy || !selected}>校验并冻结方案</button>
          </form>}
          {plan ? <div className="phase6-plan"><h4>服务端冻结方案 · {plan.plan_id} / revision {plan.plan_revision}</h4><dl><Pair label="状态与摘要">{[plan.status, plan.plan_digest]}</Pair><Pair label="来源 / 练习修订">{plan.source_context ? `${String(plan.source_context.source_code)} · ${String(plan.source_context.practice_id)} · revision ${String(plan.source_context.revision)}` : "服务端未返回"}</Pair><Pair label="数据版本">{plan.source_context?.data_version_id ?? "服务端未返回"}</Pair><Pair label="训练 / 评估区间">{`训练 ${dateRange(plan.training_dates)}；评估 ${dateRange(plan.evaluation_dates)}`}</Pair><Pair label="参数候选 / 冻结基线">{`${plan.parameter_name ?? "参数"}：${(plan.candidates ?? []).join("、")} / ${plan.baseline_value ?? "无"}`}</Pair><Pair label="指标 / 选择规则">{[plan.primary_metric, plan.selection_rule, plan.risk_constraints]}</Pair><Pair label="冻结预算摘要">{plan.budget ? `墙钟 ${String(plan.budget.wall_seconds)} 秒；磁盘上限 ${String(plan.budget.max_bytes)} bytes；逻辑步数 ${String(plan.budget.estimated_logical_steps)} / ${String(plan.budget.max_logical_steps)}` : "服务端未返回"}</Pair><Pair label="限制">{plan.limitations ?? []}</Pair></dl><details><summary>完整服务端冻结 DTO（含十字段上下文与全部日期）</summary><pre className="phase6-json">{JSON.stringify(plan, null, 2)}</pre></details><p>启动仅创建本地隔离实验；不调用模型或行情供应商，也不改变原练习。</p><button className="practice-primary" type="button" disabled={busy || Boolean(experiment && !terminal.has(experiment.status))} onClick={() => { if (window.confirm("明确启动此已冻结方案的本地隔离实验？")) void launch(); }}>明确启动实验</button></div> : null}
        </details>
      </article> : null}
    </section>
    <section className="phase6-card"><h2>实验 Operation、attempt 与结果</h2><button className="refresh-button" type="button" onClick={() => void loadExperiments()}>刷新全部实验</button>{experimentList.length ? <ul className="phase6-list">{experimentList.map(item => <li key={item.operation_id}><button className="link-button" type="button" onClick={() => void selectExperiment(item.operation_id)}>{item.operation_id} · {statusLabel[item.status] ?? item.status} · attempt {item.attempt}</button><span>generation {item.generation} · {item.updated_at}</span></li>)}</ul> : <p className="muted">没有实验记录，或研究服务尚未就绪。</p>}{!experiment ? <p className="muted">选择一条实验记录查看服务端 Operation、attempt、停止状态和最终结果。</p> : <><strong>{statusLabel[experiment.status] ?? experiment.status}</strong><p>attempt {experiment.attempt} · generation {experiment.generation} · {experiment.progress.phase} · {experiment.progress.completed_units}{experiment.progress.total_units === null ? " / 未知" : ` / ${experiment.progress.total_units}`}</p>{experiment.stop_reason ? <p>停止原因：{experiment.stop_reason}</p> : null}{experiment.resumable ? <button className="refresh-button" type="button" onClick={() => void resume()} disabled={busy}>恢复实验 · 服务端允许 resumable</button> : null}{experiment.status === "interrupted" && !experiment.resumable ? <p className="muted">服务端没有标记可恢复，不能续跑。</p> : null}{!terminal.has(experiment.status) ? <button className="link-button" type="button" onClick={() => void stop()} disabled={busy}>取消实验</button> : null}<h3>服务端完整响应与结果</h3><pre className="phase6-json">{JSON.stringify(experiment.result ?? experiment, null, 2)}</pre></>}</section>
  </div>;
}

function CredentialsPanel() {
  const [items, setItems] = useState<CredentialPublic[]>([]); const [secrets, setSecrets] = useState<Record<string, string>>({}); const [modes, setModes] = useState<Record<string, "persistent" | "process">>({}); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState("");
  const load = useCallback(async () => { setError(""); try { setItems((await listCredentials()).credentials); } catch (e) { setError(message(e)); } }, []); useEffect(() => { void load(); }, [load]);
  async function save(item: CredentialPublic) { const secret = secrets[item.slot] ?? ""; if (!secret) return; const provider = item.slot === "market_data:tushare" ? "tushare" : item.provider; if (!provider || provider === "unknown") { setError(`${item.slot} 缺少服务端 provider 标识，暂时不能保存。`); return; } if (!item.target_fingerprint) { setError(`${item.slot} 缺少服务端目标指纹；先在服务端设置目标，再保存凭据。`); return; } setBusy(item.slot); setError(""); try { await saveCredential(item.slot, { secret, provider, target_fingerprint: item.target_fingerprint, storage_mode: modes[item.slot] ?? item.storage_mode ?? "persistent", expected_generation: item.credential_generation, idempotency_key: newIdempotencyKey() }); setNotice(`${item.slot} 已保存并绑定到当前服务端目标；输入框已清空。`); await load(); } catch (e) { setError(message(e)); } finally { setSecrets(current => ({ ...current, [item.slot]: "" })); setBusy(""); } }
  async function clear(item: CredentialPublic) { setBusy(item.slot); setError(""); try { await clearCredential(item.slot, item.credential_generation); setSecrets(current => ({ ...current, [item.slot]: "" })); setNotice(`${item.slot} 已请求清除。状态以服务端返回为准。`); await load(); } catch (e) { setError(message(e)); } finally { setBusy(""); } }
  return <section className="phase6-card"><div className="section-heading"><div><h2>模型与行情凭据</h2><p className="muted">模型凭据用于明确提交的 Agent 研究任务；行情凭据用于数据服务。凭据只发送到本地后端，不写入浏览器持久存储或 URL。关闭浏览器不代表后端进程退出，也不代表供应商撤销了 Key。</p></div><button className="refresh-button" onClick={() => void load()}>刷新状态</button></div>{error ? <p className="phase6-error" role="alert">{error}</p> : null}{notice ? <p className="agent-success" role="status">{notice}</p> : null}{!items.length ? <p className="muted">凭据服务未就绪，当前无法读取受保护存储状态。</p> : items.map(item => <article className="phase6-credential" key={item.slot}><div><h3>{item.slot}</h3><p>{item.provider ?? "提供方未知"} · {item.state} · {item.origin === "external_environment" ? "外部环境配置" : item.origin === "managed" ? "由本应用管理" : "无凭据"}{item.masked ? ` · ${item.masked}` : ""}</p><p>保存方式：{item.storage_mode === "persistent" ? "受保护的持久保存" : item.storage_mode === "process" ? "仅当前后端进程内存" : "尚未配置"} · generation {item.credential_generation}</p>{item.state === "cleared" ? <p role="status">凭据已清除，必须重新填写 Key 才能恢复调用；清除状态不会回退到 .env。</p> : null}{item.reason ? <p>{item.reason === "target_mismatch" ? "现存凭据绑定到旧目标；重新保存时会绑定到当前服务端显示的目标。" : item.reason}</p> : null}{item.slot === "model" && item.state === "missing" ? <p role="status">模型目标指纹尚未由服务端配置；此状态下不能安全绑定新 Key。通过模型设置完成目标配置后刷新。</p> : null}</div><div className="phase6-credential-form"><label className="practice-field"><span>新的 Key（保存后清空）</span><input type="password" autoComplete="new-password" value={secrets[item.slot] ?? ""} onChange={e => setSecrets(current => ({ ...current, [item.slot]: e.target.value }))}/></label><label className="practice-field"><span>保存方式</span><select value={modes[item.slot] ?? item.storage_mode ?? "persistent"} onChange={e => setModes(current => ({ ...current, [item.slot]: e.target.value as "persistent" | "process" }))}><option value="persistent">受保护持久保存</option><option value="process">仅当前后端进程</option></select></label><div className="phase6-actions"><button className="practice-primary" type="button" onClick={() => void save(item)} disabled={busy === item.slot || !(secrets[item.slot] ?? "") || !item.target_fingerprint}>保存并清空输入</button><button className="link-button" type="button" onClick={() => void clear(item)} disabled={busy === item.slot || item.state === "cleared"}>清除凭据</button></div></div></article>)}<p className="practice-field-hint">清除后不应回退到环境变量。重新启用模型任务时，系统会重新显示外发数据摘要、预算并要求本次授权。</p></section>;
}

export function ResearchSettings() { const [tab, setTab] = useState<"research" | "credentials">("research"); return <div className="practice-home phase6-page"><section className="practice-hero"><div><p className="eyebrow">阶段 6 · 研究与运行凭据</p><h1>检索证据，冻结方案，再明确启动实验</h1><p className="practice-hero-lead">研究实验使用隔离空间，不会自动影响历史模拟的订单或结果。</p></div></section><div className="phase6-tabs"><button className={tab === "research" ? "practice-primary" : "refresh-button"} onClick={() => setTab("research")}>研究与实验</button><button className={tab === "credentials" ? "practice-primary" : "refresh-button"} onClick={() => setTab("credentials")}>凭据设置</button></div>{tab === "research" ? <ResearchPanel/> : <CredentialsPanel/>}</div>; }
