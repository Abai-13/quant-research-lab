import { useCallback, useEffect, useState } from "react";
import { cancelDataJob, getDataJob, getDataPresets, prepareData, resumeDataJob } from "./client";
import type { DataPreset, OperationView, PreparedDataset } from "./types";
import { toInputDate } from "../practice/format";

const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const statusLabel: Record<string, string> = { queued: "排队中", running: "准备中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "请求失败，请稍后重试。"; }

export function DataPreparation({ onUseDataset }: { onUseDataset?: (dataset: PreparedDataset) => void }) {
  const [presets, setPresets] = useState<DataPreset[]>([]); const [presetId, setPresetId] = useState("");
  const [symbols, setSymbols] = useState("sh.600000, sz.000001"); const [startDate, setStartDate] = useState("2024-01-01"); const [endDate, setEndDate] = useState("2024-06-30");
  const [sourceCode, setSourceCode] = useState(""); const [refreshMode, setRefreshMode] = useState("use_cache"); const [operation, setOperation] = useState<OperationView | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const loadPresets = useCallback(async () => { setError(""); try { const result = await getDataPresets(); setPresets(result.presets); if (!presetId && result.presets[0]) { setPresetId(result.presets[0].preset_id); setStartDate(toInputDate(result.presets[0].start_date)); setEndDate(toInputDate(result.presets[0].end_date)); setSymbols(result.presets[0].symbols.join(", ")); setSourceCode(result.presets[0].source_code ?? ""); } } catch (e) { setError(errorMessage(e)); } }, [presetId]);
  useEffect(() => { void loadPresets(); }, [loadPresets]);
  useEffect(() => {
    if (!operation || terminal.has(operation.status)) return;
    let timer = 0; let stopped = false;
    const poll = async () => {
      if (stopped) return;
      try { const next = await getDataJob(operation.operation_id); if (!stopped) setOperation(next); }
      catch (e) { if (!stopped) setError(`${errorMessage(e)} 状态读取失败，可手动重试。`); }
      if (!stopped) timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000);
    };
    timer = window.setTimeout(poll, document.visibilityState === "hidden" ? 10000 : 2000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [operation?.operation_id, operation?.status, operation?.updated_at]);
  function choosePreset(id: string) { setPresetId(id); const p = presets.find(x => x.preset_id === id); if (!p) return; setStartDate(toInputDate(p.start_date)); setEndDate(toInputDate(p.end_date)); setSymbols(p.symbols.join(", ")); setSourceCode(p.source_code ?? ""); }
  async function start(synthetic = false) {
    setBusy(true); setError("");
    try { const selected = presets.find(p => p.preset_id === presetId); const request: Record<string, unknown> = { source_code: synthetic ? "synthetic" : sourceCode || selected?.source_code || undefined, refresh_mode: synthetic ? "use_cache" : refreshMode };
      if (selected) request.preset_id = selected.preset_id;
      request.symbols = symbols.split(/[，,\s]+/).map(x => x.trim().toLowerCase()).filter(Boolean); request.start_date = startDate; request.end_date = endDate;
      setOperation(await prepareData(request));
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  async function cancel() { if (!operation) return; setBusy(true); setError(""); try { setOperation(await cancelDataJob(operation.operation_id, operation.generation)); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  async function resume() { if (!operation?.resumable) return; setBusy(true); setError(""); try { setOperation(await resumeDataJob(operation.operation_id, operation.generation)); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  const operationResult = operation?.result && typeof operation.result === "object" ? operation.result as Record<string, unknown> : null;
  const preparedResult = operationResult?.prepared_dataset && typeof operationResult.prepared_dataset === "object" ? operationResult.prepared_dataset as PreparedDataset : operationResult && "data_version_id" in operationResult ? operationResult as unknown as PreparedDataset : null;
  const dataset = operation?.status === "completed" ? preparedResult : null;
  const eligibility = dataset?.eligibility;
  const eligibilityStatus = typeof eligibility === "string" ? eligibility.toLowerCase() : String((eligibility as Record<string, unknown> | undefined)?.status ?? "").toLowerCase();
  const explicitlyBlocked = eligibilityStatus === "blocked" || (typeof eligibility === "object" && Boolean(eligibility) && (eligibility as Record<string, unknown>).eligible === false);
  const restrictedAnalysis = eligibilityStatus === "restricted_analysis";
  const simulationReady = typeof eligibility === "string"
    ? ["simulation_ready", "ready", "eligible"].includes(eligibility.toLowerCase()) || (dataset?.synthetic === true && eligibility === "restricted_analysis")
    : Boolean(eligibility && typeof eligibility === "object" && ((eligibility as Record<string, unknown>).simulation_ready === true || (eligibility as Record<string, unknown>).eligible === true || ["simulation_ready", "ready", "eligible"].includes(String((eligibility as Record<string, unknown>).status ?? "").toLowerCase())));
  const canUseDataset = Boolean(dataset?.data_version_id && simulationReady) && !explicitlyBlocked;
  return <section className="section phase6-card" aria-labelledby="data-prepare-heading">
    <div className="section-heading"><div><p className="eyebrow">历史模拟 · 数据准备</p><h2 id="data-prepare-heading">准备历史数据</h2><p className="muted">首次无需填写 URL、上传文件、版本 ID 或 Token。准备失败不会自动切换为合成数据。</p></div><button className="refresh-button" type="button" onClick={() => void loadPresets()}>重新读取预设</button></div>
    {error ? <div className="phase6-error" role="alert"><p>{error}</p><button className="link-button" type="button" onClick={() => void loadPresets()}>重试读取</button></div> : null}
    {!presets.length ? <p className="muted">服务端尚未提供数据预设；请稍后重试或先使用下方明确标注的合成示例入口。</p> : <div className="phase6-grid">
      <label className="practice-field"><span>历史区间预设</span><select value={presetId} onChange={e => choosePreset(e.target.value)}>{presets.map(p => <option key={p.preset_id} value={p.preset_id}>{p.name}（{p.start_date}～{p.end_date}）</option>)}</select></label>
      <label className="practice-field"><span>数据来源（由服务端提供）</span><input value={sourceCode} onChange={e => setSourceCode(e.target.value)} placeholder="使用预设来源" /></label>
      <label className="practice-field"><span>开始日期</span><input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} /></label>
      <label className="practice-field"><span>结束日期</span><input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} /></label>
      <label className="practice-field phase6-wide"><span>证券范围（可编辑，最多范围由服务端校验）</span><input value={symbols} onChange={e => setSymbols(e.target.value)} /></label>
      <label className="practice-field"><span>缓存策略</span><select value={refreshMode} onChange={e => setRefreshMode(e.target.value)}><option value="use_cache">优先复用可用缓存</option><option value="force_refresh">明确刷新并准备新版本</option></select></label>
      <div className="phase6-actions"><button className="practice-primary" type="button" onClick={() => void start()} disabled={busy || !presetId || Boolean(operation && !terminal.has(operation.status))}>准备真实历史数据</button><button className="refresh-button" type="button" onClick={() => void start(true)} disabled={busy || Boolean(operation && !terminal.has(operation.status))}>准备合成示例</button></div>
    </div>}
    {!presets.length ? <div className="phase6-actions"><button className="refresh-button" type="button" onClick={() => void start(true)} disabled={busy || Boolean(operation && !terminal.has(operation.status))}>明确准备合成示例</button></div> : null}
    {operation ? <article className="phase6-operation" aria-live="polite"><div><strong>{statusLabel[operation.status] ?? operation.status}</strong><span>阶段：{operation.progress.phase} · 已完成 {operation.progress.completed_units}{operation.progress.total_units === null ? " 单元（总量未知）" : ` / ${operation.progress.total_units}`}</span></div>{operation.stop_reason ? <p>停止原因：{operation.stop_reason}</p> : null}{operation.checkpoint?.summary ? <p>检查点：{operation.checkpoint.summary}</p> : null}
      {!terminal.has(operation.status) ? <button className="link-button" type="button" onClick={() => void cancel()} disabled={busy}>取消准备</button> : null}
      {terminal.has(operation.status) && operation.resumable ? <button className="refresh-button" type="button" onClick={() => void resume()} disabled={busy}>恢复（服务端已校验可恢复）</button> : null}
      {operation.status === "interrupted" && !operation.resumable ? <p>此任务不能恢复：{operation.stop_reason || "服务端未标记 resumable。"}</p> : null}
      {dataset ? <div className="phase6-result"><h3>{dataset.synthetic ? "合成数据已准备" : "历史数据已准备"}</h3><p>来源 {dataset.source_code} · 版本由服务端管理 · {dataset.cache_hit ? "命中缓存" : "新准备"}</p><p>{dataset.start_date}～{dataset.end_date} · {dataset.symbols.join("、")}</p><p>质量：{JSON.stringify(dataset.quality)} · 资格：{typeof dataset.eligibility === "string" ? dataset.eligibility : JSON.stringify(dataset.eligibility)}</p><p>内容摘要：{dataset.content_digest ?? "不可用"}</p>{dataset.limitations?.length ? <ul>{dataset.limitations.map(x => <li key={x}>{x}</li>)}</ul> : null}{onUseDataset ? <><button className="practice-primary" type="button" onClick={() => onUseDataset(dataset)} disabled={!canUseDataset}>{restrictedAnalysis ? "以明确限制带入创建模拟" : "将此版本带入创建模拟"}</button>{restrictedAnalysis && canUseDataset ? <p className="muted">该版本只有受限分析资格；带入后仍须检查数据限制和模拟选项。</p> : null}{!canUseDataset ? <p className="phase6-error">服务端未提供可用于模拟的数据版本；该结果仅供查看资格与限制。</p> : null}</> : null}</div> : null}
    </article> : null}
    <p className="practice-field-hint">来源可用性、缓存与模拟资格以服务端结果为准。网络准备会向所选来源请求数据；合成数据仅用于离线演示并始终明确标记。</p>
  </section>;
}
