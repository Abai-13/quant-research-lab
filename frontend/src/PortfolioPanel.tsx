import { useState } from "react";

import { getJson, postJson } from "./api";

interface DemoInfo {
  ok: boolean;
  account_id: string;
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

interface SimulationState {
  cash_cents: number;
  positions: Array<{ ts_code: string; shares: number; sellable_shares: number | null }>;
}

const fieldStyle = { display: "grid", gap: 4, marginBottom: 8 } as const;

export function PortfolioPanel() {
  const [demo, setDemo] = useState<DemoInfo | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [accountId, setAccountId] = useState("my_account");
  const [displayName, setDisplayName] = useState("我的持仓");
  const [cash, setCash] = useState("10000000");
  const [code, setCode] = useState("000001.SZ");
  const [shares, setShares] = useState("100");
  const [cost, setCost] = useState("100000");
  const [state, setState] = useState<SimulationState | null>(null);

  const [versionId, setVersionId] = useState("");
  const [poolId, setPoolId] = useState("");
  const [asOf, setAsOf] = useState("");
  const [parameterBundle, setParameterBundle] = useState("");
  const [feeBundle, setFeeBundle] = useState("");
  const [mode, setMode] = useState<"incremental" | "independent">("incremental");
  const [recommendation, setRecommendation] = useState<RecommendationResult | null>(null);

  const [snapshotId, setSnapshotId] = useState("");
  const [segmentId, setSegmentId] = useState("");
  const [ruleBundle, setRuleBundle] = useState("");
  const [tradeDate, setTradeDate] = useState("");
  const [simState, setSimState] = useState<SimulationState | null>(null);

  async function startDemo() {
    setBusy(true);
    try {
      const response = await getJson<DemoInfo>("/api/portfolio/demo/bootstrap", new AbortController().signal);
      if (!response.ok || !response.body) {
        setMessage(`无法启动离线演示（HTTP ${response.status}）`);
        return;
      }
      const info = response.body;
      setDemo(info);
      setAccountId(info.account_id);
      setVersionId(info.version_id);
      setPoolId(info.pool_snapshot_id);
      setAsOf(info.as_of);
      setParameterBundle(info.parameter_bundle_id);
      setFeeBundle(info.fee_bundle_id);
      setRuleBundle(info.simulation_rule_bundle_id);
      setTradeDate(info.simulation_next_trade_date);
      const stateResponse = await getJson<{ state: SimulationState }>(
        `/api/portfolio/accounts/${info.account_id}?demo=true`,
        new AbortController().signal,
      );
      setState(stateResponse.body?.state ?? null);
      const snapshots = await getJson<{ snapshots: Array<{ snapshot_id: string }> }>(
        `/api/portfolio/accounts/${info.account_id}/snapshots?demo=true`,
        new AbortController().signal,
      );
      setSnapshotId(snapshots.body?.snapshots?.[0]?.snapshot_id ?? "");
      setMessage("离线演示已启动：合成数据，独立数据库，不写入个人账户。");
    } finally {
      setBusy(false);
    }
  }

  async function createManualAccount() {
    setBusy(true);
    try {
      const created = await postJson<{ ok: boolean }>("/api/portfolio/accounts", {
        account_id: accountId,
        display_name: displayName,
      });
      if (!created.ok) {
        setMessage(`创建账户失败：${JSON.stringify(created.body)}`);
        return;
      }
      const opening = {
        event_type: "opening",
        business_date: asOf || "20250301",
        idempotency_key: `web-opening-${accountId}`,
        payload: {
          cash_cents: Number(cash),
          positions: Number(shares) > 0
            ? [{
                ts_code: code,
                shares: Number(shares),
                total_cost_cents: cost === "" ? null : Number(cost),
                sellable_shares: Number(shares),
                acquire_date: asOf || "20250301",
                locked: false,
              }]
            : [],
        },
        expected_account_revision: 0,
      };
      const posted = await postJson(`/api/portfolio/accounts/${accountId}/events`, opening);
      if (!posted.ok) {
        setMessage(`期初保存失败：${JSON.stringify(posted.body)}`);
        return;
      }
      const loaded = await getJson<{ state: SimulationState }>(
        `/api/portfolio/accounts/${accountId}`,
        new AbortController().signal,
      );
      setState(loaded.body?.state ?? null);
      setMessage("账户与期初持仓已保存。");
    } finally {
      setBusy(false);
    }
  }

  async function createRecommendation() {
    setBusy(true);
    try {
      const response = await postJson<RecommendationResult>("/api/portfolio/recommendation/runs", {
        demo: Boolean(demo),
        account_id: accountId,
        source_code: "tushare-fixture",
        as_of: asOf,
        data_version_id: versionId,
        pool_snapshot_id: poolId || null,
        rule_bundle_id: parameterBundle,
        fee_bundle_id: feeBundle,
        mode,
      });
      if (!response.ok || !response.body) {
        setMessage(`生成建议失败：${JSON.stringify(response.body)}`);
        return;
      }
      setRecommendation(response.body);
      setMessage(`建议已生成：${response.body.run_id}`);
    } finally {
      setBusy(false);
    }
  }

  async function createSimulation() {
    setBusy(true);
    try {
      const response = await postJson<{ segment_id: string }>("/api/portfolio/simulation/segments", {
        demo: Boolean(demo),
        source_account_id: accountId,
        base_account_snapshot_id: snapshotId,
        rule_bundle_id: ruleBundle,
        fee_bundle_id: feeBundle,
        source_code: "tushare-fixture",
        data_version_id: versionId,
        as_of: demo ? demo.simulation_as_of : asOf,
      });
      if (!response.ok || !response.body) {
        setMessage(`创建模拟段失败：${JSON.stringify(response.body)}`);
        return;
      }
      setSegmentId(response.body.segment_id);
      setMessage(`模拟段已创建：${response.body.segment_id}`);
    } finally {
      setBusy(false);
    }
  }

  async function placeAndAdvance() {
    if (!segmentId) {
      setMessage("请先创建模拟段。");
      return;
    }
    setBusy(true);
    try {
      const order = await postJson(`/api/portfolio/simulation/segments/${segmentId}/orders`, {
        demo: Boolean(demo),
        ts_code: code,
        direction: "buy",
        planned_shares: Number(shares),
        trade_date: tradeDate,
        idempotency_key: `web-${segmentId}-${tradeDate}`,
      });
      if (!order.ok) {
        setMessage(`下单失败：${JSON.stringify(order.body)}`);
        return;
      }
      const advanced = await postJson(`/api/portfolio/simulation/segments/${segmentId}/advance`, {
        demo: Boolean(demo),
        on_trade_date: tradeDate,
      });
      if (!advanced.ok) {
        setMessage(`推进失败：${JSON.stringify(advanced.body)}`);
        return;
      }
      const loaded = await getJson<{ state: SimulationState }>(
        `/api/portfolio/simulation/segments/${segmentId}/state?demo=${demo ? "true" : "false"}`,
        new AbortController().signal,
      );
      setSimState(loaded.body?.state ?? null);
      setMessage("模拟订单已推进；实际账户未被修改。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section" aria-labelledby="portfolio-heading">
      <div className="section-heading">
        <h2 id="portfolio-heading">我的持仓、组合建议与模拟</h2>
        <p>
          用户实际记录、系统建议与模拟成交分开显示。不确定的数据保持未知；
          费用与成交仅为版本化合成假设，不冒充真实费率或投资建议。
        </p>
      </div>

      {message ? <p className="muted">{message}</p> : null}
      {demo ? (
        <p className="chip chip-warn">
          <span className="chip-dot" aria-hidden="true" />
          离线演示：合成数据与独立数据库
        </p>
      ) : null}

      <div className="status-grid">
        <article className="card">
          <div className="card-top"><h3>账户与期初</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>账户标识
              <input value={accountId} onChange={(event) => setAccountId(event.target.value)} />
            </label>
            <label style={fieldStyle}>显示名称
              <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
            </label>
            <label style={fieldStyle}>现金（分）
              <input value={cash} onChange={(event) => setCash(event.target.value)} />
            </label>
            <label style={fieldStyle}>证券代码
              <input value={code} onChange={(event) => setCode(event.target.value)} />
            </label>
            <label style={fieldStyle}>数量（股）
              <input value={shares} onChange={(event) => setShares(event.target.value)} />
            </label>
            <label style={fieldStyle}>总成本（分，可空）
              <input value={cost} onChange={(event) => setCost(event.target.value)} />
            </label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button type="button" onClick={startDemo} disabled={busy}>启动离线演示</button>
              <button type="button" onClick={createManualAccount} disabled={busy}>保存我的期初</button>
            </div>
            {state ? (
              <p className="muted">
                现金 {state.cash_cents} 分；持仓 {state.positions.length} 项
                {state.positions.map((p) => ` · ${p.ts_code} ${p.shares}股`).join("")}
              </p>
            ) : null}
          </div>
        </article>

        <article className="card">
          <div className="card-top"><h3>组合建议</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>数据版本
              <input value={versionId} onChange={(event) => setVersionId(event.target.value)} />
            </label>
            <label style={fieldStyle}>股票池快照
              <input value={poolId} onChange={(event) => setPoolId(event.target.value)} />
            </label>
            <label style={fieldStyle}>截至日 YYYYMMDD
              <input value={asOf} onChange={(event) => setAsOf(event.target.value)} />
            </label>
            <label style={fieldStyle}>参数规则组合
              <input value={parameterBundle} onChange={(event) => setParameterBundle(event.target.value)} />
            </label>
            <label style={fieldStyle}>费用组合
              <input value={feeBundle} onChange={(event) => setFeeBundle(event.target.value)} />
            </label>
            <label style={fieldStyle}>模式
              <select value={mode} onChange={(event) => setMode(event.target.value as "incremental" | "independent")}>
                <option value="incremental">渐进调整（默认）</option>
                <option value="independent">同资金独立推荐</option>
              </select>
            </label>
            <button type="button" onClick={createRecommendation} disabled={busy || !versionId}>
              生成建议
            </button>
            {recommendation ? (
              <div>
                <p className="muted">
                  {recommendation.run_id} · {recommendation.report.mode} ·
                  {recommendation.report.restricted ? " 受限" : " 完整"}
                </p>
                {recommendation.report.restriction_detail ? (
                  <p className="muted">{recommendation.report.restriction_detail}</p>
                ) : null}
                <ul>
                  {recommendation.report.items.slice(0, 20).map((item) => (
                    <li key={`${item.ts_code}-${item.decision}`}>
                      {item.ts_code} · {item.decision}
                      {item.quantity_shares != null ? ` · ${item.quantity_shares}股` : ""}
                      {item.restriction_code ? ` · ${item.restriction_code}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </article>

        <article className="card">
          <div className="card-top"><h3>模拟记录</h3></div>
          <div className="card-body">
            <label style={fieldStyle}>基线快照
              <input value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)} />
            </label>
            <label style={fieldStyle}>模拟规则组合
              <input value={ruleBundle} onChange={(event) => setRuleBundle(event.target.value)} />
            </label>
            <label style={fieldStyle}>订单处理日 YYYYMMDD
              <input value={tradeDate} onChange={(event) => setTradeDate(event.target.value)} />
            </label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button type="button" onClick={createSimulation} disabled={busy || !snapshotId}>创建模拟段</button>
              <button type="button" onClick={placeAndAdvance} disabled={busy || !segmentId}>模拟买入并推进</button>
            </div>
            {segmentId ? <p className="muted">模拟段：{segmentId}</p> : null}
            {simState ? (
              <p className="muted">
                模拟现金 {simState.cash_cents} 分；持仓
                {simState.positions.map((p) => ` · ${p.ts_code} ${p.shares}股`).join("") || " 无"}
              </p>
            ) : null}
            <p className="muted">模拟成交只写模拟域，不修改实际账户；未成交/受限原因在接口中显式返回。</p>
          </div>
        </article>
      </div>
    </section>
  );
}
