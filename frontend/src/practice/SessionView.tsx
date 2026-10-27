import { useEffect, useMemo, useState } from "react";
import "./practice-session.css";

import type {
  MarketSeries,
  OrderPreview,
  PracticeDecision,
  PracticeOptionSymbol,
  PracticeOptions,
  PracticeReport,
  PracticeView,
} from "./types";
import {
  directionLabel,
  formatCents,
  formatDate,
  formatShares,
  isFinished,
  statusLabel,
} from "./format";
import { ComparisonChart, MarketChart, PathMetrics } from "./ComparisonChart";
import { DecisionPanel } from "./DecisionPanel";
import { ReportView } from "./ReportView";
import { TradePanel } from "./TradePanel";
import {
  EmptyState,
  Notice,
  ScalarFields,
  SectionHeading,
  StatusBadge,
  StatusChip,
  sourceLabel,
  type Tone,
} from "./ui";

type ConfirmAction = "advance" | "finish" | "restart" | null;

function pendingOrderCount(session: PracticeView): number {
  return session.user_state.orders.filter(
    (order) => order.status === "pending" || order.status === "partially_filled",
  ).length;
}

function userValuation(session: PracticeView): number | null {
  const userPath = session.comparison?.paths.find((path) => path.key === "user") ?? null;
  return userPath?.nav_cents ?? null;
}

function symbolLabel(symbol: PracticeOptionSymbol): string {
  return symbol.name ? `${symbol.ts_code} ${symbol.name}` : symbol.ts_code;
}

export function SessionView({
  session,
  options,
  market,
  marketLoading,
  marketError,
  report,
  reportLoading,
  reportError,
  decisions,
  busy,
  notice,
  onDismissNotice,
  onBack,
  onRefresh,
  onOpenMarket,
  onPreview,
  onSubmitOrder,
  onAdvance,
  onFinish,
  onRestart,
  onAddDecision,
  onSendResearchQuestion,
  onRetryReport,
}: {
  session: PracticeView;
  options: PracticeOptions | null;
  market: MarketSeries | null;
  marketLoading: boolean;
  marketError: string | null;
  report: PracticeReport | null;
  reportLoading: boolean;
  reportError: string | null;
  decisions: PracticeDecision[];
  busy: string | null;
  notice: { tone: Tone; text: string } | null;
  onDismissNotice: () => void;
  onBack: () => void;
  onRefresh: () => void;
  onOpenMarket: (code: string) => void;
  onPreview: (input: { ts_code: string; direction: string; planned_shares: number }) => Promise<OrderPreview>;
  onSubmitOrder: (input: { ts_code: string; direction: string; planned_shares: number; key: string }) => Promise<void>;
  onAdvance: () => Promise<void>;
  onFinish: () => Promise<void>;
  onRestart: () => Promise<void>;
  onAddDecision: (input: {
    judgment: string;
    evidence: string;
    exit_condition: string;
    decision_date?: string;
    key: string;
  }) => Promise<void>;
  onSendResearchQuestion?: (question: string) => void;
  onRetryReport: () => void;
}) {
  const [hideSystem, setHideSystem] = useState(Boolean(session.settings.hide_system_hints));
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [marketQuery, setMarketQuery] = useState("");
  const finished = isFinished(session.status);

  useEffect(() => {
    setHideSystem(Boolean(session.settings.hide_system_hints));
  }, [session.practice_id, session.settings.hide_system_hints]);

  const marketCodes = useMemo(() => {
    const seen = new Set<string>();
    const list: PracticeOptionSymbol[] = [];
    const push = (code: string, name = "") => {
      if (!code || seen.has(code)) {
        return;
      }
      seen.add(code);
      list.push({ ts_code: code, name });
    };
    for (const position of session.user_state.positions) {
      push(position.ts_code);
    }
    for (const order of session.user_state.orders) {
      push(order.ts_code);
    }
    for (const symbol of options?.symbols ?? []) {
      push(symbol.ts_code, symbol.name);
    }
    return list;
  }, [session.user_state.positions, session.user_state.orders, options]);

  const marketMatches = useMemo(() => {
    const query = marketQuery.trim().toLowerCase();
    if (!query) {
      return [];
    }
    return marketCodes
      .filter(
        (symbol) =>
          symbol.ts_code.toLowerCase().includes(query) || symbol.name.toLowerCase().includes(query),
      )
      .slice(0, 8);
  }, [marketCodes, marketQuery]);

  function handleAdvance() {
    setConfirmAction(null);
    void onAdvance();
  }

  const valuation = userValuation(session);
  const sourceLimitations = session.source.limitations;
  const visibleMarket = useMemo(() => {
    if (!market) {
      return null;
    }
    const cutoff = session.revealed_date;
    if (!cutoff) {
      return market;
    }
    return { ...market, points: market.points.filter((point) => point.date <= cutoff) };
  }, [market, session.revealed_date]);

  return (
    <div className="practice-session">
      <div className="practice-session-topbar">
        <button type="button" className="link-button" onClick={onBack}>
          ← 返回历史模拟
        </button>
        <div className="practice-session-top-actions">
          <button type="button" className="refresh-button" onClick={onRefresh} disabled={Boolean(busy)}>
            {busy === "refresh" ? "刷新中…" : "刷新进度"}
          </button>
          <button
            type="button"
            className="link-button"
            disabled={Boolean(busy)}
            onClick={() => setConfirmAction("restart")}
          >
            重新创建模拟
          </button>
        </div>
      </div>

      <header className="practice-session-header">
        <div>
          <p className="eyebrow">历史模拟</p>
          <h1>{session.name}</h1>
          <div className="practice-session-tags">
            <StatusBadge status={session.status} />
            <StatusChip tone={session.source.synthetic ? "warn" : "ok"}>
              {sourceLabel(session.source)}
            </StatusChip>
            <span className="muted">
              {formatDate(session.start_date)} ~ {formatDate(session.end_date)}
            </span>
          </div>
        </div>
        <div className="practice-session-progress">
          <span>当前揭晓日</span>
          <strong>{session.revealed_date ? formatDate(session.revealed_date) : "尚未揭晓"}</strong>
          <span>下一交易日</span>
          <strong>{session.next_trade_date ? formatDate(session.next_trade_date) : "—"}</strong>
        </div>
      </header>

      {notice ? (
        <Notice tone={notice.tone} onDismiss={onDismissNotice}>
          {notice.text}
        </Notice>
      ) : null}

      {sourceLimitations.length > 0 ? (
        <details className="practice-details">
          <summary>数据来源与覆盖限制</summary>
          <ul className="practice-restrictions">
            {sourceLimitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </details>
      ) : null}

      <div className="practice-summary-grid">
        <div className="practice-summary-card">
          <span>可用现金</span>
          <strong>{formatCents(session.user_state.cash_cents)} 元</strong>
        </div>
        <div className="practice-summary-card">
          <span>组合估值（已揭晓）</span>
          <strong>{valuation === null ? "待揭晓" : `${formatCents(valuation)} 元`}</strong>
        </div>
        <div className="practice-summary-card">
          <span>持仓</span>
          <strong>{session.user_state.positions.length} 项</strong>
        </div>
        <div className="practice-summary-card">
          <span>待处理订单</span>
          <strong>{pendingOrderCount(session)} 笔</strong>
        </div>
      </div>

      {finished ? (
        <Notice tone="neutral" title="模拟已结束">
          当前记录与模拟报告可读；不再接受新订单、推进或新决策。可在上方重新创建一段新模拟。
        </Notice>
      ) : null}

      <div className="practice-workspace-grid">
        <div className="practice-workspace-main">
          <section className="practice-card" aria-labelledby="positions-heading">
            <div className="practice-card-header">
              <h3 id="positions-heading">当前持仓</h3>
            </div>
            {session.user_state.positions.length === 0 ? (
              <p className="muted">当前为空仓。可以在右侧下单买入。</p>
            ) : (
              <div className="practice-table-wrap">
                <table className="practice-table">
                  <thead>
                    <tr>
                      <th scope="col">证券</th>
                      <th scope="col">股数</th>
                      <th scope="col">可卖</th>
                      <th scope="col">总成本（元）</th>
                      <th scope="col">取得日</th>
                    </tr>
                  </thead>
                  <tbody>
                    {session.user_state.positions.map((position) => (
                      <tr key={position.ts_code}>
                        <th scope="row">{position.ts_code}</th>
                        <td>{formatShares(position.shares)}</td>
                        <td>{position.sellable_shares === null ? "未知" : formatShares(position.sellable_shares)}</td>
                        <td>{formatCents(position.total_cost_cents)}</td>
                        <td>{formatDate(position.acquire_date)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="practice-card" aria-labelledby="orders-heading">
            <div className="practice-card-header">
              <h3 id="orders-heading">订单</h3>
            </div>
            {session.user_state.orders.length === 0 ? (
              <p className="muted">还没有订单。下单只登记下一可执行日，不会推进日期。</p>
            ) : (
              <div className="practice-table-wrap">
                <table className="practice-table">
                  <thead>
                    <tr>
                      <th scope="col">成交日</th>
                      <th scope="col">方向</th>
                      <th scope="col">证券</th>
                      <th scope="col">计划 / 成交 / 剩余</th>
                      <th scope="col">状态</th>
                      <th scope="col">受限原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {session.user_state.orders.map((order) => (
                      <tr key={order.order_id}>
                        <td>{formatDate(order.trade_date)}</td>
                        <td>{directionLabel(order.direction)}</td>
                        <th scope="row">{order.ts_code}</th>
                        <td>
                          {formatShares(order.planned_shares)} /{" "}
                          {order.filled_shares === null ? "—" : formatShares(order.filled_shares)} /{" "}
                          {order.remaining_shares === null ? "—" : formatShares(order.remaining_shares)}
                        </td>
                        <td>{statusLabel(order.status)}</td>
                        <td>{order.reason_code ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="practice-card" aria-labelledby="market-heading">
            <div className="practice-card-header">
              <div>
                <h3 id="market-heading">已揭晓行情</h3>
                <p className="muted">只显示当前揭晓日及以前的行情，不提前画出未来价格。</p>
              </div>
            </div>
            <div className="practice-market-search">
              <input
                value={marketQuery}
                placeholder="输入代码或名称查看行情"
                aria-label="行情证券代码或名称"
                onChange={(event) => setMarketQuery(event.target.value)}
              />
              <button
                type="button"
                className="refresh-button"
                disabled={marketLoading || !/^[0-9]{6}\.(SZ|SH|BJ)$/.test(marketQuery.trim().toUpperCase())}
                onClick={() => onOpenMarket(marketQuery.trim().toUpperCase())}
              >
                {marketLoading ? "读取中…" : "查看行情"}
              </button>
            </div>
            {marketMatches.length > 0 ? (
              <ul className="practice-symbol-results practice-market-results">
                {marketMatches.map((symbol) => (
                  <li key={symbol.ts_code}>
                    <button
                      type="button"
                      onClick={() => {
                        setMarketQuery(symbol.ts_code);
                        onOpenMarket(symbol.ts_code);
                      }}
                    >
                      <span>{symbolLabel(symbol)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            {marketError ? (
              <Notice tone="error">{marketError}</Notice>
            ) : visibleMarket ? (
              <div className="practice-market">
                <p className="muted">
                  {visibleMarket.code} · 截至{" "}
                  {formatDate(visibleMarket.as_of ?? session.revealed_date)}
                </p>
                <MarketChart market={visibleMarket} />
              </div>
            ) : (
              <EmptyState title="选择证券查看已揭晓行情">
                可以从持仓、订单或可选证券中选择；没有数据时不会显示占位价格。
              </EmptyState>
            )}
          </section>
        </div>

        <div className="practice-workspace-side">
          <TradePanel
            session={session}
            symbols={options?.symbols ?? []}
            busy={Boolean(busy)}
            onPreview={onPreview}
            onSubmit={onSubmitOrder}
          />

          <div className="practice-card practice-actions-card">
            <div className="practice-card-header">
              <div>
                <h3>推进与结束</h3>
                <p className="muted">交易、揭晓和结束是三个独立动作，不会自动连续执行。</p>
              </div>
            </div>
            <div className="practice-action-buttons">
              <button
                type="button"
                className="practice-primary"
                disabled={finished || Boolean(busy) || !session.next_trade_date}
                onClick={() => setConfirmAction("advance")}
              >
                {session.next_trade_date
                  ? `揭晓 ${formatDate(session.next_trade_date)}`
                  : "没有下一交易日"}
              </button>
              <button
                type="button"
                className="refresh-button"
                disabled={finished || Boolean(busy)}
                onClick={() => setConfirmAction("finish")}
              >
                结束模拟并生成复盘
              </button>
            </div>
            <p className="practice-field-hint">
              揭晓恰好推进一个交易日；无订单也可以推进。结束按当前已揭晓日估值，不强制卖出。
            </p>
          </div>
        </div>
      </div>

      {confirmAction === "advance" ? (
        <div className="practice-confirm practice-confirm-wide" role="group" aria-label="确认揭晓下一交易日">
          <p>
            将揭晓 {formatDate(session.next_trade_date)} 的行情并处理待执行订单。此操作不能撤销。
          </p>
          <div className="practice-confirm-actions">
            <button type="button" className="practice-primary" disabled={Boolean(busy)} onClick={handleAdvance}>
              确认揭晓
            </button>
            <button type="button" className="link-button" onClick={() => setConfirmAction(null)}>
              取消
            </button>
          </div>
        </div>
      ) : null}

      {confirmAction === "finish" ? (
        <div className="practice-confirm practice-confirm-wide" role="group" aria-label="确认结束模拟">
          <p>将在当前揭晓日结束模拟并生成模拟报告，之后不能再交易或推进。持仓不会被强制卖出。</p>
          <div className="practice-confirm-actions">
            <button
              type="button"
              className="practice-primary"
              disabled={Boolean(busy)}
              onClick={() => {
                setConfirmAction(null);
                void onFinish();
              }}
            >
              确认结束
            </button>
            <button type="button" className="link-button" onClick={() => setConfirmAction(null)}>
              取消
            </button>
          </div>
        </div>
      ) : null}

      {confirmAction === "restart" ? (
        <div className="practice-confirm practice-confirm-wide" role="group" aria-label="确认重新创建模拟">
          <p>将基于同一设置创建新的模拟身份，旧记录与报告保留。确认重新创建吗？</p>
          <div className="practice-confirm-actions">
            <button
              type="button"
              className="practice-primary"
              disabled={Boolean(busy)}
              onClick={() => {
                setConfirmAction(null);
                void onRestart();
              }}
            >
              确认重开
            </button>
            <button type="button" className="link-button" onClick={() => setConfirmAction(null)}>
              取消
            </button>
          </div>
        </div>
      ) : null}

      <section className="section" aria-labelledby="comparison-heading">
        <SectionHeading
          id="comparison-heading"
          title="对照曲线"
          actions={
            <label className="practice-checkbox practice-inline-checkbox">
              <input
                type="checkbox"
                checked={hideSystem}
                onChange={(event) => setHideSystem(event.target.checked)}
              />
              <span>隐藏系统建议</span>
            </label>
          }
        >
          三条路径与轻量持有不动参考共用同一起点、数据版本、费用与规则；系统建议仅供参考，不会自动下单。
        </SectionHeading>
        <div className="practice-card">
          <ComparisonChart
            comparison={session.comparison}
            revealedDate={session.revealed_date}
            hideSystem={hideSystem}
          />
          <PathMetrics comparison={session.comparison} hideSystem={hideSystem} />
          {session.comparison?.contributions && session.comparison.contributions.length > 0 ? (
            <div className="practice-contributions">
              <h4>算术贡献</h4>
              <ul>
                {session.comparison.contributions.map((item, index) => (
                  <li key={index}>
                    {typeof item === "string" ? (
                      item
                    ) : typeof item === "object" && item !== null ? (
                      <ScalarFields
                        record={item as Record<string, unknown>}
                        emptyText="该项没有可展示的数值。"
                      />
                    ) : (
                      String(item)
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {session.comparison?.limitations && session.comparison.limitations.length > 0 ? (
            <ul className="practice-restrictions">
              {session.comparison.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </section>

      <section className="section" aria-labelledby="decisions-heading">
        <SectionHeading id="decisions-heading" title="决策记录">
          可选记录判断与退出条件；原文追加保存，补记以新的更正记录呈现。
        </SectionHeading>
        <DecisionPanel
          decisions={decisions}
          finished={finished}
          busy={Boolean(busy)}
          onAdd={onAddDecision}
          onSendQuestion={onSendResearchQuestion}
        />
      </section>

      {finished ? (
        <section className="section" aria-labelledby="report-heading">
          <SectionHeading id="report-heading" title="复盘报告">
            报告历史可在历史模拟首页重新打开；结束不强制卖出，未知估值保持为空。
          </SectionHeading>
          <ReportView
            report={report}
            revealedDate={session.revealed_date}
            hideSystem={hideSystem}
            loading={reportLoading}
            error={reportError}
            onRetry={onRetryReport}
          />
        </section>
      ) : null}
    </div>
  );
}
