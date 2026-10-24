import { useMemo, useRef, useState } from "react";

import type { OrderPreview, PracticeOptionSymbol, PracticeView } from "./types";
import { directionLabel, formatDate, formatShares, parseShares } from "./format";
import { Notice, ScalarFields } from "./ui";

interface TradeDraft {
  code: string;
  query: string;
  direction: "buy" | "sell";
  shares: string;
}

const EMPTY_DRAFT: TradeDraft = { code: "", query: "", direction: "buy", shares: "" };

function previewRecord(preview: OrderPreview): Record<string, unknown> {
  const record: Record<string, unknown> = {};
  const mapping: Array<[string, string]> = [
    ["ts_code", "ts_code"],
    ["direction", "direction"],
    ["planned_shares", "planned_shares"],
    ["trade_date", "trade_date"],
    ["execution_date", "trade_date"],
    ["estimated_price_cents", "estimated_price_cents"],
    ["known_price_cents", "estimated_price_cents"],
    ["price_cents", "estimated_price_cents"],
    ["estimated_gross_cents", "estimated_gross_cents"],
    ["estimated_notional_cents", "estimated_gross_cents"],
    ["gross_cents", "estimated_gross_cents"],
    ["estimated_fee_cents", "estimated_fee_cents"],
    ["fee_cents", "estimated_fee_cents"],
    ["estimated_cash_delta_cents", "estimated_cash_delta_cents"],
    ["cash_delta_cents", "estimated_cash_delta_cents"],
    ["remaining_cash_cents", "remaining_cash_cents"],
    ["cash_sufficient_at_known_price", "cash_sufficient_at_known_price"],
    ["as_of", "as_of"],
  ];
  for (const [sourceKey, displayKey] of mapping) {
    if (displayKey in record) {
      continue;
    }
    const value = preview[sourceKey];
    if (value === undefined || value === null) {
      continue;
    }
    record[displayKey] = value;
  }
  return record;
}

function restrictionTexts(value: OrderPreview["restrictions"]): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (typeof item === "object" && item !== null) {
        const code = "code" in item && typeof item.code === "string" ? item.code : null;
        const detail = "detail" in item && typeof item.detail === "string" ? item.detail : null;
        if (code && detail) {
          return `${code}：${detail}`;
        }
        return detail ?? code;
      }
      return null;
    })
    .filter((item): item is string => Boolean(item));
}

export function TradePanel({
  session,
  symbols,
  busy,
  onPreview,
  onSubmit,
}: {
  session: PracticeView;
  symbols: PracticeOptionSymbol[];
  busy: boolean;
  onPreview: (input: { ts_code: string; direction: string; planned_shares: number }) => Promise<OrderPreview>;
  onSubmit: (input: { ts_code: string; direction: string; planned_shares: number; key: string }) => Promise<void>;
}) {
  const [draft, setDraft] = useState<TradeDraft>(EMPTY_DRAFT);
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [previewSignature, setPreviewSignature] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const keyRef = useRef<{ key: string; signature: string } | null>(null);

  const finished = session.status === "finished" || session.status === "closed" || session.status === "completed";
  const signature = `${draft.code}|${draft.direction}|${draft.shares.trim()}`;
  const holding = session.user_state.positions.find((position) => position.ts_code === draft.code) ?? null;
  const selectedName = useMemo(
    () => symbols.find((symbol) => symbol.ts_code === draft.code)?.name ?? "",
    [symbols, draft.code],
  );
  const filteredSymbols = useMemo(() => {
    const query = draft.query.trim().toLowerCase();
    if (!query) {
      return [];
    }
    return symbols
      .filter(
        (symbol) =>
          symbol.ts_code.toLowerCase().includes(query) || symbol.name.toLowerCase().includes(query),
      )
      .slice(0, 6);
  }, [symbols, draft.query]);

  function updateDraft(patch: Partial<TradeDraft>) {
    setDraft((current) => ({ ...current, ...patch }));
    setPreview(null);
    setPreviewSignature(null);
    setError(null);
    setStatus(null);
  }

  async function handlePreview() {
    setError(null);
    setStatus(null);
    if (!/^[0-9]{6}\.(SZ|SH|BJ)$/.test(draft.code)) {
      setError("请先从搜索结果中选择证券，或输入完整的 6 位代码及交易所后缀。");
      return;
    }
    let shares: number;
    try {
      shares = parseShares(draft.shares);
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "股数无效。");
      return;
    }
    setPreviewing(true);
    try {
      const result = await onPreview({
        ts_code: draft.code,
        direction: draft.direction,
        planned_shares: shares,
      });
      setPreview(result);
      setPreviewSignature(signature);
    } catch (previewError) {
      setError(previewError instanceof Error ? previewError.message : "订单预览失败，请稍后重试。");
    } finally {
      setPreviewing(false);
    }
  }

  async function handleSubmit() {
    setError(null);
    setStatus(null);
    let shares: number;
    try {
      shares = parseShares(draft.shares);
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "股数无效。");
      return;
    }
    if (!preview || previewSignature !== signature) {
      setError("请先预览当前订单，确认无误后再提交。");
      return;
    }
    if (keyRef.current?.signature !== signature) {
      keyRef.current = { key: crypto.randomUUID(), signature };
    }
    try {
      await onSubmit({
        ts_code: draft.code,
        direction: draft.direction,
        planned_shares: shares,
        key: keyRef.current.key,
      });
      keyRef.current = null;
      setStatus(
        `订单已登记，将在最早可执行日 ${formatDate(session.next_trade_date)} 按当时价格尝试成交。成交价尚未揭晓。`,
      );
      setDraft((current) => ({ ...current, shares: "" }));
      setPreview(null);
      setPreviewSignature(null);
    } catch (submitError) {
      setError(
        `${submitError instanceof Error ? submitError.message : "下单失败，请稍后重试。"} 网络结果不明时可直接重试，系统会复用同一请求编号，不会重复下单。`,
      );
    }
  }

  const previewFields = preview ? previewRecord(preview) : null;
  const restrictions = preview ? restrictionTexts(preview.restrictions) : [];

  return (
    <div className="practice-card">
      <div className="practice-card-header">
        <div>
          <h3>下单</h3>
          <p className="muted">订单只登记到下一可执行日，不会立即推进日期，也不会修改真实账本。</p>
        </div>
      </div>

      {finished ? (
        <Notice tone="neutral">模拟已结束，不再接受新订单。</Notice>
      ) : null}

      <div className="practice-trade-grid">
        <div className="practice-symbol-picker">
          <label className="practice-field">
            <span>证券</span>
            <input
              value={draft.query}
              disabled={finished || busy}
              placeholder="输入代码或名称"
              autoComplete="off"
              onChange={(event) =>
                updateDraft({ query: event.target.value, code: event.target.value.trim().toUpperCase() })
              }
            />
          </label>
          {draft.query.trim() !== "" && filteredSymbols.length > 0 ? (
            <ul className="practice-symbol-results">
              {filteredSymbols.map((symbol) => (
                <li key={symbol.ts_code}>
                  <button
                    type="button"
                    disabled={finished || busy}
                    onClick={() => updateDraft({ code: symbol.ts_code, query: `${symbol.ts_code} ${symbol.name}`.trim() })}
                  >
                    <span>{symbol.ts_code}</span>
                    <span className="muted">{symbol.name || "名称未知"}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <p className="practice-field-hint">
            {/^[0-9]{6}\.(SZ|SH|BJ)$/.test(draft.code)
              ? `已选择 ${draft.code}${selectedName ? ` · ${selectedName}` : ""}`
              : "请选择或输入完整代码（如 000001.SZ）。"}
          </p>
        </div>

        <label className="practice-field">
          <span>方向</span>
          <select
            value={draft.direction}
            disabled={finished || busy}
            onChange={(event) => updateDraft({ direction: event.target.value as "buy" | "sell" })}
          >
            <option value="buy">买入</option>
            <option value="sell">卖出</option>
          </select>
        </label>

        <label className="practice-field">
          <span>股数</span>
          <input
            inputMode="numeric"
            value={draft.shares}
            disabled={finished || busy}
            placeholder="例如 100"
            onChange={(event) => updateDraft({ shares: event.target.value })}
          />
        </label>

        <div className="practice-trade-actions">
          <button
            type="button"
            className="refresh-button"
            disabled={finished || busy || previewing}
            onClick={() => void handlePreview()}
          >
            {previewing ? "试算中…" : "预览订单"}
          </button>
          <button
            type="button"
            className="practice-primary"
            disabled={finished || busy || !preview || previewSignature !== signature}
            onClick={() => void handleSubmit()}
          >
            确认登记订单
          </button>
        </div>
      </div>

      {holding ? (
        <p className="muted">
          当前持有 {holding.ts_code} {formatShares(holding.shares)} 股，可卖{" "}
          {holding.sellable_shares === null ? "未知" : formatShares(holding.sellable_shares)} 股。
        </p>
      ) : null}

      {preview && previewSignature === signature ? (
        <div className="practice-preview" aria-label="订单预览">
          <h4>订单预览（不写订单）</h4>
          <ScalarFields record={previewFields} emptyText="预览没有返回可展示的数值。" />
          <p className="practice-field-hint">
            方向 {directionLabel(preview.direction ?? draft.direction)}；最早成交日{" "}
            {formatDate(preview.trade_date ?? session.next_trade_date)}。未来成交价格保持未知，费用与可成交数量以实际处理为准。
          </p>
          {restrictions.length > 0 ? (
            <ul className="practice-restrictions">
              {restrictions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          {preview.warnings && preview.warnings.length > 0 ? (
            <ul className="practice-restrictions">
              {preview.warnings.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          {preview.limitations && preview.limitations.length > 0 ? (
            <ul className="practice-restrictions">
              {preview.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          <div className="practice-confirm" role="group" aria-label="确认提交订单">
            <p>确认后只登记订单，不会立刻成交；下一交易日揭晓时才会知道成交结果。</p>
            <div className="practice-confirm-actions">
              <button
                type="button"
                className="practice-primary"
                disabled={finished || busy}
                onClick={() => void handleSubmit()}
              >
                确认提交
              </button>
              <button
                type="button"
                className="link-button"
                onClick={() => {
                  setPreview(null);
                  setPreviewSignature(null);
                }}
              >
                取消预览
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {error ? <Notice tone="error">{error}</Notice> : null}
      {status ? <Notice tone="ok">{status}</Notice> : null}
    </div>
  );
}
