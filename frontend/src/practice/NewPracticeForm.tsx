import { useEffect, useMemo, useRef, useState } from "react";

import type {
  CreatePracticeInput,
  DemoPreparation,
  NewPositionInput,
  PlanPayload,
  PracticePlan,
  PracticeOptionSymbol,
  PracticeOptions,
} from "./types";
import { formatDate, parseShares, parseYuanToCents, toApiDate, toInputDate } from "./format";
import { Notice } from "./ui";

interface PositionDraft {
  id: string;
  code: string;
  query: string;
  shares: string;
  cost: string;
  sellable: string;
}

function newPosition(): PositionDraft {
  return {
    id: crypto.randomUUID(),
    code: "",
    query: "",
    shares: "",
    cost: "",
    sellable: "",
  };
}

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function versionOptionLabel(
  version: PracticeOptions["versions"][number],
  index: number,
): string {
  const parts: string[] = [];
  if (version.synthetic) {
    parts.push("合成数据，资格以服务端为准");
  }
  if (version.start_date || version.end_date) {
    parts.push(`${formatDate(version.start_date)} ~ ${formatDate(version.end_date)}`);
  }
  const base =
    version.label && version.label !== version.version_id ? version.label : `数据版本 ${index + 1}`;
  return parts.length > 0 ? `${base}（${parts.join(" · ")}）` : base;
}

function qualificationHint(issue: { code: string; detail: string }, availableRange: string): string {
  const text = `${issue.code} ${issue.detail}`.toLowerCase().replace(/[-\s]+/g, "_");
  const dateRange = availableRange || "所选数据版本提供的范围";
  if (/start_date_after_end|invalid_date_range|date_range_order/.test(text)) {
    return "结束日期不能早于开始日期。请检查所选区间后重新检查资格。";
  }
  if (/start|begin/.test(text) && /date|trading|daily/.test(text)) {
    return `开始日期暂不可用。请在当前可用范围（${dateRange}）内，选择有行情数据的交易日。`;
  }
  if (/end|until/.test(text) && /date|trading|daily/.test(text)) {
    return `结束日期暂不可用。请在当前可用范围（${dateRange}）内，选择有行情数据的交易日。`;
  }
  if (/weekend|holiday|non_trading|not_trading|no_daily_data|missing.*daily.*(bar|data)|daily.*(bar|data).*missing/.test(text)) {
    return `所选日期可能是休市日或缺少日线行情。请在当前可用范围（${dateRange}）内改选有数据的交易日。`;
  }
  if (/date|trading|daily|date_range|trading_range|daily_range/.test(text)) {
    return `所选日期或行情范围暂不可用。请在当前可用范围（${dateRange}）内选择有数据的交易日。`;
  }
  if (/symbol|stock|security|ts_code|ticker/.test(text)) {
    return "所选证券可能不在当前数据版本中。请核对关注证券和期初持仓后重新检查资格。";
  }
  if (/source|provider|data_source/.test(text)) {
    return "当前数据来源可能不支持这项方案。请检查高级设置中的数据来源后重新检查资格。";
  }
  if (/cash|funds|capital|budget/.test(text)) {
    return "起始资金可能不符合这项方案的要求。请核对起始现金后重新检查资格。";
  }
  return "这项方案条件尚未通过检查。请核对方案设置后重新检查资格，并展开下方服务端说明查看具体原因。";
}

function dateInputBound(value: string | null | undefined): string | undefined {
  const inputDate = toInputDate(value);
  return inputDate || undefined;
}

function SymbolPicker({
  value,
  query,
  symbols,
  disabled,
  onChange,
}: {
  value: string;
  query: string;
  symbols: PracticeOptionSymbol[];
  disabled: boolean;
  onChange: (code: string, query: string) => void;
}) {
  const [focused, setFocused] = useState(false);
  const normalized = query.trim().toLowerCase();
  const matches = useMemo(() => {
    if (!normalized) {
      return [];
    }
    return symbols
      .filter(
        (symbol) =>
          symbol.ts_code.toLowerCase().includes(normalized) ||
          symbol.name.toLowerCase().includes(normalized),
      )
      .slice(0, 6);
  }, [normalized, symbols]);

  const showMatches = focused && normalized.length > 0 && matches.length > 0;
  const selectedName = symbols.find((symbol) => symbol.ts_code === value)?.name ?? "";

  return (
    <div className="practice-symbol-picker">
      <input
        value={query}
        disabled={disabled}
        placeholder="输入证券代码或名称"
        onChange={(event) => onChange(event.target.value.trim().toUpperCase(), event.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => window.setTimeout(() => setFocused(false), 120)}
        aria-label="证券代码或名称"
        autoComplete="off"
      />
      {showMatches ? (
        <ul className="practice-symbol-results">
          {matches.map((symbol) => (
            <li key={symbol.ts_code}>
              <button
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onChange(symbol.ts_code, `${symbol.ts_code} ${symbol.name}`.trim());
                  setFocused(false);
                }}
              >
                <span>{symbol.ts_code}</span>
                <span className="muted">{symbol.name || "名称未知"}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {value && /^[0-9]{6}\.(SZ|SH|BJ)$/.test(value) ? (
        <p className="practice-field-hint">
          已选择 {value}
          {selectedName ? ` · ${selectedName}` : ""}
        </p>
      ) : (
        <p className="practice-field-hint">也可直接输入 6 位代码加 .SZ、.SH 或 .BJ。</p>
      )}
    </div>
  );
}

export function NewPracticeForm({
  options,
  demo,
  demoBusy,
  creating,
  planBusy = false,
  onCreate,
  plan,
  onSavePlan,
  onConfirmPlan,
  onPrepareDemo,
  setupDraft,
  onSetupDraftConsumed,
}: {
  options: PracticeOptions | null;
  demo: DemoPreparation | null;
  demoBusy: boolean;
  creating: boolean;
  planBusy?: boolean;
  onCreate?: (input: CreatePracticeInput) => Promise<void>;
  plan?: PracticePlan | null;
  onSavePlan?: (payload: PlanPayload, symbols: string[], goal: string, key: string) => Promise<void>;
  onConfirmPlan?: () => Promise<void>;
  onPrepareDemo: () => Promise<void>;
  setupDraft?: Record<string, unknown> | null;
  onSetupDraftConsumed?: () => void;
}) {
  const [name, setName] = useState("我的历史模拟");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [cash, setCash] = useState("");
  const [versionId, setVersionId] = useState("");
  const [sourceCode, setSourceCode] = useState("");
  const [hideSystemHints, setHideSystemHints] = useState(false);
  const [positions, setPositions] = useState<PositionDraft[]>([]);
  const [watchlistSymbols, setWatchlistSymbols] = useState<string[]>([]);
  const [goal, setGoal] = useState("手动历史模拟练习");
  const [planDirty, setPlanDirty] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const keyRef = useRef<{ key: string; signature: string } | null>(null);
  const submitInFlightRef = useRef(false);

  useEffect(() => {
    if (!options) {
      return;
    }
    const initialVersionId =
      options.defaults.data_version_id || options.versions[0]?.version_id || "";
    const defaultVersion =
      options.versions.find((version) => version.version_id === initialVersionId) ??
      options.versions[0] ??
      null;
    setStartDate(
      (current) =>
        current || toInputDate(options.defaults.start_date ?? defaultVersion?.start_date ?? null),
    );
    setEndDate(
      (current) =>
        current || toInputDate(options.defaults.end_date ?? defaultVersion?.end_date ?? null),
    );
    setVersionId((current) => current || initialVersionId);
    setSourceCode(
      (current) =>
        current ||
        options.defaults.source_code ||
        defaultVersion?.source_code ||
        options.sources[0]?.code ||
        "",
    );
    setWatchlistSymbols((current) => current.length ? current : options.symbols[0] ? [options.symbols[0].ts_code] : []);
    setCash((current) => current || (options.defaults.cash_cents !== null && options.defaults.cash_cents !== undefined ? (options.defaults.cash_cents / 100).toFixed(2) : ""));
    if (options.defaults.watchlist_symbols?.length) setWatchlistSymbols(options.defaults.watchlist_symbols.slice(0, 20));
  }, [options]);

  useEffect(() => {
    if (!demo) {
      return;
    }
    if (demo.startDate) {
      setStartDate(toInputDate(demo.startDate));
    }
    if (demo.endDate) {
      setEndDate(toInputDate(demo.endDate));
    }
    if (demo.dataVersionId) {
      setVersionId(demo.dataVersionId);
    }
    if (demo.sourceCode) {
      setSourceCode(demo.sourceCode);
    }
    if (demo.options) {
      setName((current) => (current === "我的历史模拟" ? "合成示例模拟" : current));
    }
  }, [demo]);

  useEffect(() => {
    if (!setupDraft) return;
    if (planBusy || !options) return;
    const date = (value: unknown) => typeof value === "string" ? toInputDate(value) : "";
    if (setupDraft.kind === "prepared_dataset") {
      if (typeof setupDraft.data_version_id === "string") setVersionId(setupDraft.data_version_id);
      if (typeof setupDraft.source_code === "string") setSourceCode(setupDraft.source_code);
    }
    if (typeof setupDraft.name === "string") setName(setupDraft.name);
    if (setupDraft.start_date) setStartDate(date(setupDraft.start_date));
    if (setupDraft.end_date) setEndDate(date(setupDraft.end_date));
    const cashCents = setupDraft.cash_cents ?? setupDraft.initial_cash_cents;
    if (typeof cashCents === "number" && Number.isFinite(cashCents)) setCash((cashCents / 100).toFixed(2));
    if (Array.isArray(setupDraft.positions)) {
      const prepared = setupDraft.positions.flatMap((raw): PositionDraft[] => {
        if (!raw || typeof raw !== "object") return [];
        const position = raw as Record<string, unknown>;
        if (typeof position.ts_code !== "string") return [];
        return [{
          id: crypto.randomUUID(), code: position.ts_code, query: position.ts_code,
          shares: String(position.shares ?? ""),
          cost: typeof position.total_cost_cents === "number" ? (position.total_cost_cents / 100).toFixed(2) : "",
          sellable: position.sellable_shares === undefined || position.sellable_shares === null ? "" : String(position.sellable_shares),
        }];
      });
      setPositions(prepared);
    }
    if (setupDraft.kind === "prepared_dataset" && Array.isArray(setupDraft.symbols) && setupDraft.symbols.every((item) => typeof item === "string")) {
      const available = new Set(options.symbols.map((item) => item.ts_code));
      const selected = (setupDraft.symbols as string[]).filter((item) => available.has(item)).slice(0, 20);
      if (selected.length > 0) setWatchlistSymbols(selected);
    }
    setPlanDirty(true);
    setFormError(null);
    onSetupDraftConsumed?.();
  }, [setupDraft, planBusy, options, plan?.plan_id, plan?.version]);

  useEffect(() => {
    if (!plan) return;
    const payload = plan.payload;
    setName(payload.name);
    setStartDate(toInputDate(payload.start_date));
    setEndDate(toInputDate(payload.end_date));
    setCash((payload.cash_cents / 100).toFixed(2));
    setVersionId(payload.data_version_id);
    setSourceCode(payload.source_code);
    setHideSystemHints(Boolean(payload.settings.hide_system_hints));
    setPositions(payload.positions.map((position) => ({
      id: crypto.randomUUID(), code: position.ts_code, query: position.ts_code,
      shares: String(position.shares),
      cost: position.total_cost_cents === null ? "" : (position.total_cost_cents / 100).toFixed(2),
      sellable: String(position.sellable_shares),
    })));
    setWatchlistSymbols(plan.symbols);
    setGoal(plan.goal);
    setPlanDirty(false);
  }, [plan?.plan_id, plan?.version]);

  const selectedVersion = options?.versions.find((version) => version.version_id === versionId) ?? null;
  function updatePosition(id: string, patch: Partial<PositionDraft>) {
    setPlanDirty(true);
    setPositions((current) =>
      current.map((position) => (position.id === id ? { ...position, ...patch } : position)),
    );
  }

  function changeVersion(nextId: string) {
    setPlanDirty(true);
    setVersionId(nextId);
    const version = options?.versions.find((item) => item.version_id === nextId);
    if (version?.source_code) {
      setSourceCode(version.source_code);
    }
  }

  async function handleSubmit() {
    if (creating || submitInFlightRef.current) {
      return;
    }
    setFormError(null);
    if (!options) {
      setFormError("数据来源与版本仍在读取，请稍后再试。");
      return;
    }
    const startApi = toApiDate(startDate);
    const endApi = toApiDate(endDate);
    if (!startApi || !endApi) {
      setFormError("请选择有效的开始日期和结束日期。");
      return;
    }
    if (startApi > endApi) {
      setFormError("结束日期不能早于开始日期。");
      return;
    }
    if (!versionId) {
      setFormError("请选择一个数据版本。");
      return;
    }
    const resolvedSource =
      sourceCode || selectedVersion?.source_code || options.sources[0]?.code || "";
    if (!resolvedSource) {
      setFormError("当前没有可用的数据来源，请先准备数据或合成示例。");
      return;
    }
    let cashCents: number | null;
    try {
      cashCents = parseYuanToCents(cash);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "起始现金无效。");
      return;
    }
    if (cashCents === null) {
      setFormError("起始现金不能为空。");
      return;
    }

    const seen = new Set<string>();
    const positionPayload: NewPositionInput[] = [];
    for (const [index, position] of positions.entries()) {
      const code = position.code.trim().toUpperCase();
      if (!/^[0-9]{6}\.(SZ|SH|BJ)$/.test(code)) {
        setFormError(`第 ${index + 1} 笔持仓的证券代码无效，请从搜索结果中选择或输入完整代码。`);
        return;
      }
      if (seen.has(code)) {
        setFormError(`持仓中重复出现 ${code}，请合并为一行。`);
        return;
      }
      seen.add(code);
      let shares: number;
      try {
        shares = parseShares(position.shares);
      } catch (error) {
        setFormError(`第 ${index + 1} 笔持仓：${error instanceof Error ? error.message : "股数无效。"}`);
        return;
      }
      let sellable = shares;
      if (position.sellable.trim() !== "") {
        try {
          sellable = parseShares(position.sellable, true);
        } catch (error) {
          setFormError(`第 ${index + 1} 笔持仓可卖股数：${error instanceof Error ? error.message : "无效。"}`);
          return;
        }
        if (sellable > shares) {
          setFormError(`第 ${index + 1} 笔持仓的可卖股数不能超过持股数。`);
          return;
        }
      }
      let totalCostCents: number | null;
      try {
        totalCostCents = parseYuanToCents(position.cost, true);
      } catch (error) {
        setFormError(`第 ${index + 1} 笔持仓总成本：${error instanceof Error ? error.message : "无效。"}`);
        return;
      }
      positionPayload.push({
        ts_code: code,
        shares,
        total_cost_cents: totalCostCents,
        sellable_shares: sellable,
        acquire_date: startApi,
      });
    }

    if (name.trim() === "") {
      setFormError("请填写模拟名称。");
      return;
    }
    if (!goal.trim()) {
      setFormError("请填写本次练习目标。");
      return;
    }

    if (watchlistSymbols.length === 0 || watchlistSymbols.length > 20) {
      setFormError("请从所选版本的证券中选择 1 至 20 只关注证券。");
      return;
    }
    const payload: PlanPayload = {
      name: name.trim(),
      start_date: startApi,
      end_date: endApi,
      cash_cents: cashCents,
      positions: positionPayload,
      data_version_id: versionId,
      source_code: resolvedSource,
      settings: { hide_system_hints: hideSystemHints, watchlist_symbols: watchlistSymbols },
    };
    const signature = JSON.stringify({ planId: plan?.plan_id ?? null, planVersion: plan?.version ?? null, name, startDate, endDate, cash, versionId, sourceCode, hideSystemHints, positions, watchlistSymbols, goal });
    if (keyRef.current?.signature !== signature) {
      keyRef.current = { key: newIdempotencyKey(), signature };
    }

    submitInFlightRef.current = true;
    try {
      if (onSavePlan) {
        await onSavePlan(payload, watchlistSymbols, goal, keyRef.current.key);
      } else if (onCreate) {
        await onCreate({ ...payload, idempotency_key: keyRef.current.key });
      }
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "保存方案失败；输入已保留，可以重试。");
    } finally {
      submitInFlightRef.current = false;
    }
  }


  const ready = Boolean(options) && !creating && !planBusy && (options?.versions.length ?? 0) > 0;

  return (
    <section className="section practice-card-section" aria-labelledby="new-practice-heading">
      <div className="practice-card practice-form-card">
        <div className="practice-card-header">
          <div><h2 id="new-practice-heading">准备一段历史模拟</h2><p className="muted">先定目标和范围，Agent 可协助整理；资格检查通过后由你确认创建。</p></div>
          <button type="button" className="refresh-button" onClick={() => void onPrepareDemo()} disabled={demoBusy || options?.demo_available === false}>{demoBusy ? "正在准备…" : "准备内置演示"}</button>
        </div>
        {demo ? <Notice tone="neutral" title="演示数据已准备"><p>{formatDate(demo.startDate)} 至 {formatDate(demo.endDate)} · 由服务端返回的合成示例数据。准备数据不会创建练习。</p>{demo.limitations.length ? <ul>{demo.limitations.map(item => <li key={item}>{item}</li>)}</ul> : null}</Notice> : null}
        {!options ? <Notice tone="neutral">正在读取服务端提供的数据范围与默认值；可用前不会显示假日期或证券。</Notice> : options.versions.length === 0 ? <Notice tone="warn" title="暂无可用数据版本">准备内置演示，或稍后重试服务端数据选项。</Notice> : null}
        <form className="practice-form" onSubmit={(event) => { event.preventDefault(); void handleSubmit(); }}>
          <div className="practice-form-grid">
            <label className="practice-field practice-field-wide"><span>练习目标</span><input value={goal} maxLength={500} disabled={!ready} onChange={event => { setGoal(event.target.value); setPlanDirty(true); }} placeholder="例如：观察长期持有策略在回撤期的表现" /></label>
            <label className="practice-field practice-field-wide"><span>练习名称</span><input value={name} maxLength={80} disabled={!ready} onChange={event => { setName(event.target.value); setPlanDirty(true); }} /></label>
            <label className="practice-field"><span>开始日期</span><input type="date" value={startDate} min={dateInputBound(selectedVersion?.start_date)} max={dateInputBound(selectedVersion?.end_date)} disabled={!ready} onChange={event => { setStartDate(event.target.value); setPlanDirty(true); }} /><small className="practice-field-hint">可用范围：{formatDate(selectedVersion?.start_date ?? null)} 至 {formatDate(selectedVersion?.end_date ?? null)}</small></label>
            <label className="practice-field"><span>结束日期</span><input type="date" value={endDate} min={dateInputBound(selectedVersion?.start_date)} max={dateInputBound(selectedVersion?.end_date)} disabled={!ready} onChange={event => { setEndDate(event.target.value); setPlanDirty(true); }} /><small className="practice-field-hint">周末、节假日或缺少日线时，请按资格提示改选范围内有数据的交易日。</small></label>
            <label className="practice-field"><span>起始现金（元）</span><input inputMode="decimal" value={cash} disabled={!ready} onChange={event => { setCash(event.target.value); setPlanDirty(true); }} placeholder="按服务端默认值填写" /></label>
          </div>
          <fieldset className="practice-positions-editor"><legend>关注证券</legend><p className="practice-field-hint">用于练习室行情表；选择所选版本实际提供的证券。</p><div className="practice-review-positions">{(options?.symbols ?? []).map(symbol => <label className="practice-checkbox" key={symbol.ts_code}><input type="checkbox" checked={watchlistSymbols.includes(symbol.ts_code)} disabled={!ready} onChange={event => { setWatchlistSymbols(current => event.target.checked ? [...current, symbol.ts_code].slice(0, 20) : current.filter(code => code !== symbol.ts_code)); setPlanDirty(true); }} /><span>{symbol.ts_code} {symbol.name}</span></label>)}{options?.symbols.length === 0 ? <p className="muted">当前版本未返回可用证券。</p> : null}</div></fieldset>
          <details className="practice-details"><summary>高级设置 · 数据来源、版本与期初持仓</summary>
            <div className="practice-form-grid">
              <label className="practice-field"><span>数据版本</span><select value={versionId} disabled={!ready} onChange={event => changeVersion(event.target.value)}>{options?.versions.map((version, index) => <option key={version.version_id} value={version.version_id}>{versionOptionLabel(version, index)}</option>)}</select></label>
              <label className="practice-field"><span>数据来源</span><select value={sourceCode} disabled={!ready} onChange={event => { setSourceCode(event.target.value); setPlanDirty(true); }}>{options?.sources.map(source => <option key={source.code} value={source.code}>{source.label}</option>)}</select></label>
            </div>
            <div className="practice-positions-editor"><div className="practice-positions-head"><h3>期初持仓（可选）</h3><button type="button" className="refresh-button" disabled={!ready} onClick={() => { setPositions(current => [...current, newPosition()]); setPlanDirty(true); }}>添加持仓</button></div>
              {positions.length === 0 ? <p className="muted">空仓起点。需要时可添加期初持仓。</p> : <div className="practice-positions-list">{positions.map((position, index) => <div className="practice-position-row" key={position.id}><div className="practice-position-index">持仓 {index + 1}</div><div className="practice-position-fields"><SymbolPicker value={position.code} query={position.query} symbols={options?.symbols ?? []} disabled={!ready} onChange={(code, query) => updatePosition(position.id, { code, query })} /><label className="practice-field"><span>股数</span><input inputMode="numeric" value={position.shares} disabled={!ready} onChange={event => updatePosition(position.id, { shares: event.target.value })} /></label><label className="practice-field"><span>总成本（元，可空）</span><input inputMode="decimal" value={position.cost} disabled={!ready} onChange={event => updatePosition(position.id, { cost: event.target.value })} /></label><label className="practice-field"><span>可卖股数（默认全部）</span><input inputMode="numeric" value={position.sellable} disabled={!ready} onChange={event => updatePosition(position.id, { sellable: event.target.value })} /></label></div><button type="button" className="link-button practice-remove" disabled={!ready} onClick={() => { setPositions(current => current.filter(item => item.id !== position.id)); setPlanDirty(true); }}>移除</button></div>)}</div>}
            </div>
            <label className="practice-checkbox"><input type="checkbox" checked={hideSystemHints} disabled={!ready} onChange={event => { setHideSystemHints(event.target.checked); setPlanDirty(true); }} /><span>默认收起系统建议</span></label>
          </details>
          {formError ? <Notice tone="error">{formError}</Notice> : null}
          <div className="practice-form-actions"><button type="submit" className="practice-primary" disabled={!ready || !versionId || creating || !onSavePlan}>{creating ? "正在检查资格…" : plan ? "保存修改并检查资格" : "保存方案并检查资格"}</button><span className="muted">保存会更新同一份方案，不会创建练习。</span></div>
          {plan ? <section className="practice-review" aria-live="polite"><h3>方案 v{plan.version} · {plan.qualification.ready ? "资格可用" : "需要处理问题"}</h3>{plan.qualification.issues.length ? <ul className="practice-restrictions">{plan.qualification.issues.map((issue, index) => <li key={`${issue.code}-${index}`}><p>{qualificationHint(issue, selectedVersion?.start_date && selectedVersion.end_date ? `${formatDate(selectedVersion.start_date)} 至 ${formatDate(selectedVersion.end_date)}` : "")}</p><details><summary>查看服务端说明</summary><dl><div><dt>代码</dt><dd><code>{issue.code}</code></dd></div><div><dt>详情</dt><dd>{issue.detail}</dd></div></dl></details></li>)}</ul> : <p>服务端未返回阻断问题。</p>}{plan.qualification.limitations.length ? <ul className="practice-restrictions">{plan.qualification.limitations.map(item => <li key={item}>{item}</li>)}</ul> : null}{plan.status === "draft" ? <button type="button" className="practice-primary" disabled={!plan.qualification.ready || planDirty || creating || planBusy || !onConfirmPlan} onClick={() => void onConfirmPlan?.()}>{creating ? "确认中…" : "确认创建练习"}</button> : <p>当前方案状态：{plan.status}。</p>}<p className="practice-field-hint">{planDirty ? "表单有未保存修改；请先保存并重新检查资格。" : `确认将使用已保存的方案 v${plan.version}。`}</p></section> : null}
        </form>
      </div>
    </section>
  );
}
