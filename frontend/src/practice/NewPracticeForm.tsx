import { useEffect, useMemo, useRef, useState } from "react";

import type {
  CreatePracticeInput,
  DemoPreparation,
  NewPositionInput,
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
  onCreate,
  onPrepareDemo,
  setupDraft,
}: {
  options: PracticeOptions | null;
  demo: DemoPreparation | null;
  demoBusy: boolean;
  creating: boolean;
  onCreate: (input: CreatePracticeInput) => Promise<void>;
  onPrepareDemo: () => Promise<void>;
  setupDraft?: Record<string, unknown> | null;
}) {
  const [name, setName] = useState("我的历史模拟");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [cash, setCash] = useState("100000.00");
  const [versionId, setVersionId] = useState("");
  const [sourceCode, setSourceCode] = useState("");
  const [hideSystemHints, setHideSystemHints] = useState(false);
  const [positions, setPositions] = useState<PositionDraft[]>([]);
  const [formError, setFormError] = useState<string | null>(null);
  const [step, setStep] = useState<1 | 2 | 3>(1);
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
    const date = (value: unknown) => typeof value === "string" ? toInputDate(value) : "";
    if (typeof setupDraft.name === "string") setName(setupDraft.name);
    if (setupDraft.start_date) setStartDate(date(setupDraft.start_date));
    if (setupDraft.end_date) setEndDate(date(setupDraft.end_date));
    const cashCents = setupDraft.cash_cents ?? setupDraft.initial_cash_cents;
    if (typeof cashCents === "number" && Number.isFinite(cashCents)) setCash((cashCents / 100).toFixed(2));
    if (typeof setupDraft.data_version_id === "string") setVersionId(setupDraft.data_version_id);
    if (typeof setupDraft.source_code === "string") setSourceCode(setupDraft.source_code);
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
    setFormError(null);
  }, [setupDraft]);

  const selectedVersion = options?.versions.find((version) => version.version_id === versionId) ?? null;
  const reviewSourceCode = sourceCode || selectedVersion?.source_code || options?.sources[0]?.code || "";

  function updatePosition(id: string, patch: Partial<PositionDraft>) {
    setPositions((current) =>
      current.map((position) => (position.id === id ? { ...position, ...patch } : position)),
    );
  }

  function changeVersion(nextId: string) {
    setVersionId(nextId);
    const version = options?.versions.find((item) => item.version_id === nextId);
    if (version?.source_code) {
      setSourceCode(version.source_code);
    }
  }

  async function handleSubmit() {
    if (step !== 3 || creating || submitInFlightRef.current) {
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

    const payload: CreatePracticeInput = {
      name: name.trim(),
      start_date: startApi,
      end_date: endApi,
      cash_cents: cashCents,
      positions: positionPayload,
      data_version_id: versionId,
      source_code: resolvedSource,
      settings: { hide_system_hints: hideSystemHints },
      idempotency_key: "",
    };
    const signature = JSON.stringify({ ...payload, idempotency_key: undefined });
    if (keyRef.current?.signature !== signature) {
      keyRef.current = { key: newIdempotencyKey(), signature };
    }
    payload.idempotency_key = keyRef.current.key;

    submitInFlightRef.current = true;
    try {
      await onCreate(payload);
      keyRef.current = null;
    } catch {
      // 失败时保留幂等键，用户重试同一份内容不会创建重复会话。
    } finally {
      submitInFlightRef.current = false;
    }
  }

  const ready = Boolean(options) && !creating && (options?.versions.length ?? 0) > 0;

  function continueTo(next: 1 | 2 | 3) {
    setFormError(null);
    if (next > step && step === 1) {
      if (!options || !versionId) {
        setFormError("请选择一个服务端提供的数据版本后继续。");
        return;
      }
      const start = toApiDate(startDate);
      const end = toApiDate(endDate);
      if (!start || !end || start > end) {
        setFormError("请检查开始和结束日期；结束日期不能早于开始日期。");
        return;
      }
    }
    if (next > step && step === 2) {
      try {
        if (parseYuanToCents(cash) === null) throw new Error("请填写起始现金（元）。");
        for (const [index, position] of positions.entries()) {
          parseShares(position.shares);
          if (position.cost.trim()) parseYuanToCents(position.cost, true);
          if (position.sellable.trim()) parseShares(position.sellable, true);
          if (!/^[0-9]{6}\.(SZ|SH|BJ)$/.test(position.code.trim().toUpperCase())) {
            throw new Error(`请为第 ${index + 1} 笔持仓选择有效证券代码。`);
          }
        }
      } catch (error) {
        setFormError(error instanceof Error ? error.message : "请检查本金与持仓输入。");
        return;
      }
    }
    setStep(next);
  }

  return (
    <section className="section practice-card-section" aria-labelledby="new-practice-heading">
      <div className="practice-card practice-form-card">
        <div className="practice-card-header">
          <div>
            <h2 id="new-practice-heading">创建历史模拟</h2>
            <p className="muted">
              设定一段已经发生的历史区间，从当时可用的信息开始；每天由你手动揭晓下一交易日。
            </p>
          </div>
          <button
            type="button"
            className="refresh-button"
            onClick={() => void onPrepareDemo()}
            disabled={demoBusy || !options?.demo_available}
            title={options?.demo_available === false ? "当前没有可用的合成示例数据" : undefined}
          >
            {demoBusy ? "准备中…" : "准备内置演示"}
          </button>
        </div>

        {!options ? (
          <Notice tone="neutral">
              正在读取可用的数据来源与版本；若历史模拟服务尚未就绪，这里会给出明确提示，而不会显示假选项。
          </Notice>
        ) : options.versions.length === 0 ? (
          <Notice tone="warn" title="暂无可选数据版本">
            当前没有可选数据版本。可准备内置演示，或展开下方历史数据准备区查看受支持的数据范围。
          </Notice>
        ) : null}

        {demo ? (
          <Notice tone="warn" title="内置合成模拟演示已准备">
            <p>
              示例区间 {formatDate(demo.startDate)} ~ {formatDate(demo.endDate)}。这是内置模拟演示使用的合成行情；
              成交资格仍由所选版本和服务端规则决定。准备完成不会创建模拟，只有第三步确认后才会创建。
            </p>
            {demo.limitations.length > 0 ? (
              <ul className="practice-restrictions">
                {demo.limitations.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : null}
          </Notice>
        ) : null}

        {options && options.limitations.length > 0 ? (
          <details className="practice-details">
            <summary>数据与范围说明</summary>
            <ul className="practice-restrictions">
              {options.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
        ) : null}

        <form
          className="practice-form"
          onSubmit={(event) => {
            event.preventDefault();
            setFormError(step === 3 ? "请点击“确认并创建模拟”按钮提交。" : "请使用“继续”完成当前步骤；前两步不会创建模拟。");
          }}
        >
          <ol className="practice-wizard-steps" aria-label="创建步骤">
            {(["数据与区间", "本金与持仓", "确认摘要"] as const).map((label, index) => {
              const stepNumber = (index + 1) as 1 | 2 | 3;
              return (
                <li key={label}>
                  <button
                    type="button"
                    className={step === stepNumber ? "is-current" : step > stepNumber ? "is-complete" : ""}
                    aria-current={step === stepNumber ? "step" : undefined}
                    disabled={stepNumber > step + 1}
                    onClick={() => continueTo(stepNumber)}
                  >
                    <span>{stepNumber}</span>{label}
                  </button>
                </li>
              );
            })}
          </ol>

          {step === 1 ? <div className="practice-form-grid">
            <label className="practice-field">
              <span>模拟名称</span>
              <input
                value={name}
                disabled={!ready}
                maxLength={80}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="practice-field">
              <span>数据版本</span>
              <select
                value={versionId}
                disabled={!ready || !options || options.versions.length === 0}
                onChange={(event) => changeVersion(event.target.value)}
              >
                {options && options.versions.length === 0 ? (
                  <option value="">暂无可选版本</option>
                ) : null}
                {options?.versions.map((version, index) => (
                  <option key={version.version_id} value={version.version_id}>
                    {versionOptionLabel(version, index)}
                  </option>
                ))}
              </select>
            </label>
            <label className="practice-field">
              <span>开始日期</span>
              <input
                type="date"
                value={startDate}
                disabled={!ready}
                onChange={(event) => setStartDate(event.target.value)}
              />
            </label>
            <label className="practice-field">
              <span>结束日期</span>
              <input
                type="date"
                value={endDate}
                disabled={!ready}
                onChange={(event) => setEndDate(event.target.value)}
              />
            </label>
          </div> : null}

          {step === 2 ? <>
          <div className="practice-form-grid">
            <label className="practice-field">
              <span>起始现金（元）</span>
              <input
                inputMode="decimal"
                value={cash}
                disabled={!ready}
                onChange={(event) => setCash(event.target.value)}
              />
            </label>
            <label className="practice-field practice-checkbox">
              <input
                type="checkbox"
                checked={hideSystemHints}
                disabled={!ready}
                onChange={(event) => setHideSystemHints(event.target.checked)}
              />
              <span>默认隐藏系统建议（可随时在模拟中显示）</span>
            </label>
          </div>

          <div className="practice-positions-editor">
            <div className="practice-positions-head">
              <h3>期初持仓（可选）</h3>
              <button
                type="button"
                className="refresh-button"
                disabled={!ready}
                onClick={() => setPositions((current) => [...current, newPosition()])}
              >
                添加持仓
              </button>
            </div>
            {positions.length === 0 ? (
              <p className="muted">当前为空仓起点。你也可以添加一笔或多笔期初持仓。</p>
            ) : (
              <div className="practice-positions-list">
                {positions.map((position, index) => (
                  <div className="practice-position-row" key={position.id}>
                    <div className="practice-position-index">持仓 {index + 1}</div>
                    <div className="practice-position-fields">
                      <SymbolPicker
                        value={position.code}
                        query={position.query}
                        symbols={options?.symbols ?? []}
                        disabled={!ready}
                        onChange={(code, query) => updatePosition(position.id, { code, query })}
                      />
                      <label className="practice-field">
                        <span>股数</span>
                        <input
                          inputMode="numeric"
                          value={position.shares}
                          disabled={!ready}
                          onChange={(event) => updatePosition(position.id, { shares: event.target.value })}
                        />
                      </label>
                      <label className="practice-field">
                        <span>总成本（元，可空）</span>
                        <input
                          inputMode="decimal"
                          value={position.cost}
                          disabled={!ready}
                          onChange={(event) => updatePosition(position.id, { cost: event.target.value })}
                        />
                      </label>
                      <label className="practice-field">
                        <span>可卖股数（默认全部）</span>
                        <input
                          inputMode="numeric"
                          value={position.sellable}
                          disabled={!ready}
                          onChange={(event) => updatePosition(position.id, { sellable: event.target.value })}
                        />
                      </label>
                    </div>
                    <button
                      type="button"
                      className="link-button practice-remove"
                      disabled={!ready}
                      onClick={() => setPositions((current) => current.filter((item) => item.id !== position.id))}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
          </> : null}

          {step === 3 ? (
            <section className="practice-review" aria-labelledby="practice-review-heading">
              <h3 id="practice-review-heading">检查模拟设置</h3>
              <dl>
                <div><dt>模拟名称</dt><dd>{name.trim() || "未填写"}</dd></div>
                <div><dt>数据模式</dt><dd>{selectedVersion?.synthetic ? "合成示例" : "历史数据"}</dd></div>
                <div><dt>来源</dt><dd>{options?.sources.find((source) => source.code === reviewSourceCode)?.label || reviewSourceCode || "服务端数据来源"}</dd></div>
                <div><dt>模拟区间</dt><dd>{formatDate(startDate)} 至 {formatDate(endDate)}</dd></div>
                <div><dt>起始现金</dt><dd>{cash || "未填写"} 元</dd></div>
                <div><dt>期初持仓</dt><dd>{positions.length ? `${positions.length} 笔` : "空仓起点"}</dd></div>
              </dl>
              {positions.length ? <ul className="practice-review-positions">{positions.map((position, index) => <li key={position.id}>持仓 {index + 1}：{position.query || position.code || "待选证券"}，{position.shares || "未填股数"} 股</li>)}</ul> : null}
              <p>金额以元填写并按分保存；创建后只会揭晓到已选区间内的日期。所有价格与资格由所选数据版本决定。</p>
              <details className="practice-details">
                <summary>高级规则说明</summary>
                <p className="practice-field-hint">系统路径默认每 5 个已揭晓交易日检查一次，并受个股权重上限与现金保留约束；默认值来自服务端规则组合。</p>
                <p className="practice-field-hint">内部数据版本：{versionId || "尚未选择"}</p>
              </details>
            </section>
          ) : null}

          {formError ? <Notice tone="error">{formError}</Notice> : null}

          <div className="practice-form-actions">
            {step > 1 ? <button type="button" className="refresh-button" disabled={creating} onClick={() => continueTo((step - 1) as 1 | 2 | 3)}>上一步</button> : null}
            {step < 3 ? (
              <button
                key="wizard-next"
                type="button"
                className="practice-primary"
                disabled={!ready}
                onClick={() => continueTo((step + 1) as 1 | 2 | 3)}
              >
                继续
              </button>
            ) : (
              <button
                key="wizard-confirm-create"
                type="button"
                className="practice-primary"
                disabled={!ready || !versionId || creating}
                onClick={() => void handleSubmit()}
              >
                {creating ? "创建中…" : "确认并创建模拟"}
              </button>
            )}
            <span className="muted">草稿保留在本页；确认创建后才会生成新模拟。</span>
          </div>
        </form>
      </div>
    </section>
  );
}
