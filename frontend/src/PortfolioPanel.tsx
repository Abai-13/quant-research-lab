import { useState } from "react";

import { getJson, postJson } from "./api";

interface DemoInfo {
  ok: boolean;
  account_id: string;
  source_code: string;
  version_id: string;
  pool_snapshot_id: string;
  as_of: string;
  parameter_bundle_id: string;
  fee_bundle_id: string;
  simulation_rule_bundle_id: string;
  simulation_as_of: string;
  simulation_next_trade_date: string;
  synthetic_demo: boolean;
}

interface ReportItem {
  ts_code: string;
  decision: string;
  quantity_shares?: number | null;
  restriction_code?: string | null;
  reason?: string | null;
}

interface RecommendationResult {
  run_id: string;
  report: {
    mode: string;
    restricted: boolean;
    restriction_detail?: string | null;
    items: ReportItem[];
    valid_until?: string | null;
  };
}

interface SimPosition {
  ts_code: string;
  shares: number;
  sellable_shares: number | null;
}

interface SimOrder {
  order_id: string;
  trade_date: string;
  ts_code: string;
  direction: string;
  planned_shares: number;
  filled_shares: number;
  remaining_shares: number;
  status: string;
  reason_code: string | null;
}

interface SimulationState {
  segment_id: string;
  revision: number;
  as_of: string;
  last_advanced_date: string | null;
  cash_cents: number;
  positions: SimPosition[];
  orders: SimOrder[];
  segment_status?: string;
}

const fieldStyle = { display: "grid", gap: 4, marginBottom: 8 } as const;

function moneyToCents(value: string, allowEmpty = false): number | null {
  if (allowEmpty && value === "") return null;
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value)) {
    throw new Error("请输入非负金额，最多两位小数；不接受负数或科学计数法。");
  }
  const [whole, fraction = ""] = value.split(".");
  const cents = BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2));
  if (cents > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("金额超出安全整数范围。");
  return Number(cents);
}

function nonnegativeShares(value: string): number {
  if (!/^(?:0|[1-9]\d*)$/.test(value)) throw new Error("数量必须为非负整数股数。");
  const shares = Number(value);
  if (!Number.isSafeInteger(shares)) throw new Error("数量超出安全整数范围。");
  return shares;
}

function positiveShares(value: string): number {
  const shares = nonnegativeShares(value);
  if (shares === 0) throw new Error("数量必须为正整数股数。");
  return shares;
}

function formatCents(cents: number): string {
  const whole = Math.trunc(cents / 100);
  const fraction = Math.abs(cents % 100).toString().padStart(2, "0");
  return `${whole}.${fraction}`;
}

function draftStorageKey(segmentId: string): string {
  return `simulation-order-draft:${segmentId}`;
}

function readOrderDraft(segmentId: string): Record<string, unknown> | null {
  const storageKey = draftStorageKey(segmentId);
  const current = window.sessionStorage.getItem(storageKey);
  if (!current) return null;
  try {
    const parsed: unknown = JSON.parse(current);
    return typeof parsed === "object" && parsed !== null ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function saveOrderDraft(segmentId: string, draft: Record<string, unknown>): void {
  window.sessionStorage.setItem(draftStorageKey(segmentId), JSON.stringify(draft));
}

function clearOrderDraft(segmentId: string): void {
  window.sessionStorage.removeItem(draftStorageKey(segmentId));
}

export function PortfolioPanel() {
  const [demo, setDemo] = useState<DemoInfo | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [accountId, setAccountId] = useState("my_account");
  const [displayName, setDisplayName] = useState("我的持仓");
  const [cash, setCash] = useState("100000.00");
  const [code, setCode] = useState("000001.SZ");
  const [shares, setShares] = useState("100");
  const [cost, setCost] = useState("1000.00");
  const [state, setState] = useState<{ cash_cents: number; positions: SimPosition[] } | null>(null);

  const [sourceCode, setSourceCode] = useState("tushare");
  const [versionId, setVersionId] = useState("");
  const [poolId, setPoolId] = useState("");
  const [asOf, setAsOf] = useState("");
  const [parameterBundle, setParameterBundle] = useState("");
  const [feeBundle, setFeeBundle] = useState("");
  const [mode, setMode] = useState<"incremental" | "independent">("incremental");
  const [recommendation, setRecommendation] = useState<RecommendationResult | null>(null);

  const [snapshotId, setSnapshotId] = useState("");
  const [segmentId, setSegmentId] = useState("");
  const [simulationRunDraft, setSimulationRunDraft] = useState<Record<string, unknown> | null>(null);
  const [ruleBundle, setRuleBundle] = useState("");
  const [tradeDate, setTradeDate] = useState("");
  const [orderDirection, setOrderDirection] = useState<"buy" | "sell">("buy");
  const [simState, setSimState] = useState<SimulationState | null>(null);

  function resetContext() {
    setDemo(null);
    setAccountId("my_account");
    setDisplayName("我的持仓");
    setState(null);
    setRecommendation(null);
    setSimState(null);
    setSnapshotId("");
    setSegmentId("");
    setSimulationRunDraft(null);
    setVersionId("");
    setPoolId("");
    setAsOf("");
    setParameterBundle("");
    setFeeBundle("");
    setRuleBundle("");
    setTradeDate("");
    setSourceCode("tushare");
  }

  async function startDemo() {
    resetContext();
    setBusy(true);
    setMessage(null);
    try {
      const response = await getJson<DemoInfo>("/api/portfolio/demo/bootstrap", new AbortController().signal);
      if (!response.ok || !response.body) throw new Error(`无法启动离线演示（HTTP ${response.status}）`);
      const info = response.body;
      setDemo(info);
      setAccountId(info.account_id);
      setSourceCode(info.source_code);
      setVersionId(info.version_id);
      setPoolId(info.pool_snapshot_id);
      setAsOf(info.as_of);
      setParameterBundle(info.parameter_bundle_id);
      setFeeBundle(info.fee_bundle_id);
      setRuleBundle(info.simulation_rule_bundle_id);
      setTradeDate(info.simulation_next_trade_date);
      const stateResponse = await getJson<{ state: { cash_cents: number; positions: SimPosition[] } }>(
        `/api/portfolio/accounts/${info.account_id}?demo=true`, new AbortController().signal,
      );
      setState(stateResponse.body?.state ?? null);
      const snapshots = await getJson<{ snapshots: Array<{ snapshot_id: string }> }>(
        `/api/portfolio/accounts/${info.account_id}/snapshots?demo=true`, new AbortController().signal,
      );
      setSnapshotId(snapshots.body?.snapshots?.[0]?.snapshot_id ?? "");
      setMessage("离线演示已启动：使用 bootstrap 返回的合成来源和独立数据库。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function createManualAccount() {
    setBusy(true);
    setMessage(null);
    try {
      const cashCents = moneyToCents(cash);
      if (cashCents === null) throw new Error("现金金额不能为空。");
      const shareCount = nonnegativeShares(shares);
      let openingPositions: Array<Record<string, unknown>> = [];
      if (shareCount > 0) {
        const normalizedCode = code.trim();
        if (!/^[0-9]{6}\.(SZ|SH|BJ)$/.test(normalizedCode)) {
          throw new Error("持仓证券代码必须为 6 位数字并带 .SZ、.SH 或 .BJ 后缀。");
        }
        const totalCostCents = moneyToCents(cost, true);
        openingPositions = [{
          ts_code: normalizedCode,
          shares: shareCount,
          total_cost_cents: totalCostCents,
          sellable_shares: shareCount,
          acquire_date: asOf || "20250301",
          locked: false,
        }];
      }
      const created = await postJson<{ ok: boolean }>("/api/portfolio/accounts", {
        account_id: accountId,
        display_name: displayName,
      });
      if (!created.ok) throw new Error(`创建账户失败：${JSON.stringify(created.body)}`);
      const opening = {
        event_type: "opening",
        business_date: asOf || "20250301",
        idempotency_key: `web-opening-${accountId}`,
        payload: {
          cash_cents: cashCents,
          positions: openingPositions,
        },
        expected_account_revision: 0,
      };
      const posted = await postJson(`/api/portfolio/accounts/${accountId}/events`, opening);
      if (!posted.ok) throw new Error(`期初保存失败：${JSON.stringify(posted.body)}`);
      const loaded = await getJson<{ state: { cash_cents: number; positions: SimPosition[] } }>(
        `/api/portfolio/accounts/${accountId}`, new AbortController().signal,
      );
      if (!loaded.ok || !loaded.body) throw new Error(`读取账户失败（HTTP ${loaded.status}）`);
      setState(loaded.body.state);
      setMessage("账户与期初持仓已保存。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function createRecommendation() {
    setBusy(true);
    setMessage(null);
    try {
      if (!sourceCode.trim() || !versionId.trim()) throw new Error("请显式填写数据来源和对应的数据版本。");
      const response = await postJson<RecommendationResult>("/api/portfolio/recommendation/runs", {
        demo: Boolean(demo), account_id: accountId, source_code: sourceCode,
        as_of: asOf, data_version_id: versionId, pool_snapshot_id: poolId || null,
        rule_bundle_id: parameterBundle, fee_bundle_id: feeBundle, mode,
      });
      if (!response.ok || !response.body) throw new Error(`生成建议失败：${JSON.stringify(response.body)}`);
      setRecommendation(response.body);
      setMessage(`建议已生成：${response.body.run_id}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function createSimulation() {
    setBusy(true);
    setMessage(null);
    try {
      if (!sourceCode.trim() || !versionId.trim()) throw new Error("请显式填写数据来源和对应的数据版本。");
      const draft = simulationRunDraft ?? {
        demo: Boolean(demo), source_account_id: accountId, base_account_snapshot_id: snapshotId,
        rule_bundle_id: ruleBundle, fee_bundle_id: feeBundle, source_code: sourceCode,
        data_version_id: versionId, as_of: demo ? demo.simulation_as_of : asOf,
        simulation_run_key: crypto.randomUUID(),
      };
      setSimulationRunDraft(draft);
      const response = await postJson<{ segment_id: string }>("/api/portfolio/simulation/segments", draft);
      if (!response.ok || !response.body) throw new Error(`创建模拟段失败：${JSON.stringify(response.body)}`);
      setSegmentId(response.body.segment_id);
      setSimulationRunDraft(null);
      await refreshSimulationFor(response.body.segment_id);
      setMessage(`模拟运行已创建：${response.body.segment_id}（运行键仅作幂等命名空间）。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function refreshSimulationFor(id: string) {
    const demoFlag = demo ? "true" : "false";
    const [segmentResponse, stateResponse] = await Promise.all([
      getJson<{ segment: { status: string }; orders: SimOrder[] }>(`/api/portfolio/simulation/segments/${id}?demo=${demoFlag}`, new AbortController().signal),
      getJson<{ state: SimulationState }>(`/api/portfolio/simulation/segments/${id}/state?demo=${demoFlag}`, new AbortController().signal),
    ]);
    if (!segmentResponse.ok || !segmentResponse.body || !stateResponse.ok || !stateResponse.body) {
      throw new Error("模拟状态读取失败，请检查当前数据库模式和运行 ID。");
    }
    setSimState({ ...stateResponse.body.state, orders: segmentResponse.body.orders, segment_status: segmentResponse.body.segment.status });
  }

  async function submitOrder() {
    if (!segmentId) { setMessage("请先创建模拟运行。"); return; }
    setBusy(true);
    setMessage(null);
    try {
      const savedDraft = readOrderDraft(segmentId);
      const draft = savedDraft ?? {
        demo: Boolean(demo), ts_code: code, direction: orderDirection,
        planned_shares: positiveShares(shares), trade_date: tradeDate,
        idempotency_key: crypto.randomUUID(),
      };
      if (!savedDraft) saveOrderDraft(segmentId, draft);
      const response = await postJson("/api/portfolio/simulation/segments/" + segmentId + "/orders", {
        ...draft,
      });
      if (!response.ok) throw new Error(`下单失败：${JSON.stringify(response.body)}`);
      clearOrderDraft(segmentId);
      await refreshSimulationFor(segmentId);
      setMessage("订单已提交；当前仅登记待处理订单，没有推进日期。实际账户未被修改。");
    } catch (error) {
      setMessage(`${error instanceof Error ? error.message : String(error)} 网络结果不明时请重试当前草稿；幂等键已保留。`);
    } finally {
      setBusy(false);
    }
  }

  async function advanceSimulation() {
    if (!segmentId) { setMessage("请先创建模拟运行。"); return; }
    setBusy(true);
    setMessage(null);
    try {
      const response = await postJson(`/api/portfolio/simulation/segments/${segmentId}/advance`, {
        demo: Boolean(demo), on_trade_date: tradeDate,
      });
      if (!response.ok) throw new Error(`推进失败：${JSON.stringify(response.body)}`);
      await refreshSimulationFor(segmentId);
      setMessage("已处理至指定交易日；即使没有待处理订单也可推进。实际账户未被修改。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function closeSimulation() {
    if (!segmentId) return;
    setBusy(true);
    setMessage(null);
    try {
      const response = await postJson(`/api/portfolio/simulation/segments/${segmentId}/close`, {
        demo: Boolean(demo), reason: "user_closed_from_portfolio_panel",
      });
      if (!response.ok) throw new Error(`结束模拟失败：${JSON.stringify(response.body)}`);
      await refreshSimulationFor(segmentId);
      setMessage("模拟运行已结束，后端将拒绝后续订单。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section" aria-labelledby="portfolio-heading">
      <div className="section-heading">
        <h2 id="portfolio-heading">我的持仓、组合建议与模拟</h2>
        <p>实际记录、建议与模拟成交相互隔离。模拟使用固定数据版本和已有交易守卫；受限与未知状态会保留并显示。</p>
      </div>
      {message ? <p role="status" className="muted">{message}</p> : null}
      {demo ? <p className="chip chip-warn"><span className="chip-dot" aria-hidden="true" />离线演示：合成数据与独立数据库</p> : null}

      <div className="status-grid">
        <article className="card">
          <div className="card-top"><h3>账户与期初</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>账户标识<input value={accountId} onChange={(event) => setAccountId(event.target.value)} /></label>
            <label style={fieldStyle}>显示名称<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
            <label style={fieldStyle}>现金（元）<input inputMode="decimal" value={cash} onChange={(event) => setCash(event.target.value)} /></label>
            <label style={fieldStyle}>证券代码<input value={code} onChange={(event) => setCode(event.target.value)} /></label>
            <label style={fieldStyle}>数量（股；0 股可空仓开始）<input inputMode="numeric" value={shares} onChange={(event) => setShares(event.target.value)} /></label>
            <label style={fieldStyle}>总成本（元，可空）<input inputMode="decimal" value={cost} onChange={(event) => setCost(event.target.value)} /></label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button type="button" onClick={startDemo} disabled={busy}>启动离线演示</button>
              <button type="button" onClick={() => { resetContext(); setMessage("已切换到个人模式；演示的账户、版本和模拟上下文已清除。"); }} disabled={busy}>切换个人模式</button>
              <button type="button" onClick={createManualAccount} disabled={busy || Boolean(demo)}>保存我的期初</button>
            </div>
            {state ? <p className="muted">现金 {formatCents(state.cash_cents)} 元；持仓 {state.positions.length} 项{state.positions.map((p) => ` · ${p.ts_code} ${p.shares}股`).join("")}</p> : null}
          </div>
        </article>

        <article className="card">
          <div className="card-top"><h3>组合建议</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>数据来源（个人模式显式填写）<input value={sourceCode} disabled={Boolean(demo)} onChange={(event) => setSourceCode(event.target.value)} /></label>
            <label style={fieldStyle}>数据版本<input value={versionId} onChange={(event) => setVersionId(event.target.value)} /></label>
            <label style={fieldStyle}>股票池快照<input value={poolId} onChange={(event) => setPoolId(event.target.value)} /></label>
            <label style={fieldStyle}>截至日 YYYYMMDD<input value={asOf} onChange={(event) => setAsOf(event.target.value)} /></label>
            <label style={fieldStyle}>参数规则组合<input value={parameterBundle} onChange={(event) => setParameterBundle(event.target.value)} /></label>
            <label style={fieldStyle}>费用组合<input value={feeBundle} onChange={(event) => setFeeBundle(event.target.value)} /></label>
            <label style={fieldStyle}>模式<select value={mode} onChange={(event) => setMode(event.target.value as "incremental" | "independent")}><option value="incremental">渐进调整（默认）</option><option value="independent">同资金独立推荐</option></select></label>
            <button type="button" onClick={createRecommendation} disabled={busy || !versionId || !sourceCode}>生成建议</button>
            {recommendation ? <div>
              <p className="muted">{recommendation.run_id} · {recommendation.report.mode} · {recommendation.report.restricted ? "受限" : "完整"}</p>
              {recommendation.report.restriction_detail ? <p className="muted">{recommendation.report.restriction_detail}</p> : null}
              <ul>{recommendation.report.items.slice(0, 20).map((item) => <li key={`${item.ts_code}-${item.decision}`}>{item.ts_code} · {item.decision}{item.quantity_shares != null ? ` · ${item.quantity_shares}股` : ""}{item.restriction_code ? ` · ${item.restriction_code}` : ""}{item.reason ? ` · ${item.reason}` : ""}</li>)}</ul>
            </div> : null}
          </div>
        </article>

        <article className="card">
          <div className="card-top"><h3>模拟运行</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>基线快照<input value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)} /></label>
            <label style={fieldStyle}>模拟规则组合<input value={ruleBundle} onChange={(event) => setRuleBundle(event.target.value)} /></label>
            <label style={fieldStyle}>处理日 YYYYMMDD<input value={tradeDate} onChange={(event) => setTradeDate(event.target.value)} /></label>
            <label style={fieldStyle}>订单方向<select value={orderDirection} onChange={(event) => setOrderDirection(event.target.value as "buy" | "sell")}><option value="buy">买入</option><option value="sell">卖出</option></select></label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button type="button" onClick={createSimulation} disabled={busy || !snapshotId || !ruleBundle}>新建独立模拟运行</button>
              <button type="button" onClick={submitOrder} disabled={busy || !segmentId || simState?.segment_status === "closed"}>提交模拟订单</button>
              <button type="button" onClick={advanceSimulation} disabled={busy || !segmentId || simState?.segment_status === "closed"}>推进至指定日</button>
              <button type="button" onClick={closeSimulation} disabled={busy || !segmentId || simState?.segment_status === "closed"}>结束模拟</button>
              <button type="button" onClick={() => { if (segmentId) clearOrderDraft(segmentId); setMessage("当前订单草稿已放弃；下一次提交会使用新的幂等键。"); }} disabled={busy || !segmentId}>新建订单草稿</button>
            </div>
            {segmentId ? <p className="muted">模拟段：{segmentId} · {simState?.segment_status === "closed" ? "已结束" : "进行中"} · 已推进至 {simState?.last_advanced_date ?? "尚未推进"}</p> : null}
            {simState ? <>
              <p className="muted">模拟现金 {formatCents(simState.cash_cents)} 元；持仓{simState.positions.map((p) => ` · ${p.ts_code} ${p.shares}股（可卖 ${p.sellable_shares ?? "未知"}）`).join("") || " 无"}</p>
              <ul>{simState.orders.map((order) => <li key={order.order_id}>{order.trade_date} · {order.direction === "buy" ? "买入" : "卖出"} {order.ts_code} {order.filled_shares}/{order.planned_shares} 股 · {order.status}{order.reason_code ? ` · 受限原因 ${order.reason_code}` : ""}</li>)}</ul>
            </> : null}
            <p className="muted">提交订单不会推进日期；网络结果不明时重试当前草稿会复用幂等键。日期由用户指定，本页面不编排完整逐日练习。</p>
          </div>
        </article>
      </div>
    </section>
  );
}
