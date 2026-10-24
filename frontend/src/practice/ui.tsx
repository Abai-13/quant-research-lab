import type { ReactNode } from "react";

import { formatBps, formatCents, formatDate, formatShares, pathLabel, statusLabel } from "./format";

export type Tone = "ok" | "warn" | "error" | "neutral";

export function StatusChip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`chip chip-${tone}`}>
      <span className="chip-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

export function statusTone(status: string): Tone {
  if (status === "running" || status === "active" || status === "in_progress" || status === "filled") {
    return "ok";
  }
  if (status === "finished" || status === "closed" || status === "completed") {
    return "neutral";
  }
  if (status === "rejected" || status === "failed") {
    return "error";
  }
  if (status === "pending" || status === "partially_filled" || status === "unfilled") {
    return "warn";
  }
  return "neutral";
}

export function StatusBadge({ status }: { status: string }) {
  return <StatusChip tone={statusTone(status)}>{statusLabel(status)}</StatusChip>;
}

export function Notice({
  tone = "neutral",
  title,
  children,
  onDismiss,
}: {
  tone?: Tone;
  title?: string;
  children: ReactNode;
  onDismiss?: () => void;
}) {
  return (
    <div className={`practice-notice practice-notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
      <div className="practice-notice-body">
        {title ? <strong>{title}</strong> : null}
        <div>{children}</div>
      </div>
      {onDismiss ? (
        <button type="button" className="link-button" onClick={onDismiss}>
          关闭
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {children ? <div className="muted">{children}</div> : null}
    </div>
  );
}

export function SectionHeading({
  id,
  title,
  children,
  actions,
}: {
  id?: string;
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="section-heading practice-section-heading">
      <div>
        <h2 id={id}>{title}</h2>
        {children ? <p>{children}</p> : null}
      </div>
      {actions ? <div className="practice-heading-actions">{actions}</div> : null}
    </div>
  );
}

const FIELD_LABELS: Record<string, string> = {
  nav_cents: "估值（元）",
  return_bps: "收益",
  drawdown_bps: "最大回撤",
  cost_cents: "费用（元）",
  trade_count: "交易次数",
  turnover_bps: "换手",
  concentration_bps: "集中度",
  cash_cents: "现金（元）",
  gross_cents: "成交金额（元）",
  estimated_gross_cents: "预计成交金额（元）",
  fee_cents: "费用（元）",
  estimated_fee_cents: "预计费用（元）",
  cash_delta_cents: "现金变动（元）",
  estimated_cash_delta_cents: "预计现金变动（元）",
  remaining_cash_cents: "交易后现金（元）",
  price_cents: "参考价（元）",
  estimated_price_cents: "参考价（元）",
  known_price_cents: "已知收盘价（元）",
  estimated_notional_cents: "预计成交金额（元）",
  cash_sufficient_at_known_price: "按已知价格现金充足",
  execution_date: "最早成交日",
  planned_shares: "计划股数",
  filled_shares: "成交股数",
  remaining_shares: "剩余股数",
  ts_code: "证券代码",
  direction: "方向",
  trade_date: "成交日",
  as_of: "截至",
  start_date: "开始日期",
  end_date: "结束日期",
  revealed_date: "已揭晓日",
  next_trade_date: "下一交易日",
  total_cost_cents: "总成本（元）",
  sellable_shares: "可卖股数",
  acquire_date: "取得日期",
  synthetic: "合成数据",
  status: "状态",
  name: "名称",
  user_orders: "我的订单数",
  filled_orders: "已成交订单数",
  pending_orders: "待处理订单数",
  decision_count: "决策记录数",
  limitation: "限制",
  path: "路径",
  basis: "口径",
  vs_user_nav_difference_cents: "与我的操作估值差（元）",
};

const INTERNAL_KEY = /(^|_)(id|ids)$|(^|_)(version|snapshot|rule|bundle|seed|revision|segment)(_|$)/i;

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key;
}

export function shouldShowField(key: string, value: unknown): boolean {
  if (INTERNAL_KEY.test(key)) {
    return false;
  }
  if (value === null || value === undefined) {
    return false;
  }
  if (typeof value === "object") {
    return false;
  }
  return true;
}

export function formatFieldValue(key: string, value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "number") {
    if (key.endsWith("_cents")) {
      return formatCents(value);
    }
    if (key.endsWith("_bps")) {
      return formatBps(value);
    }
    if (key.endsWith("_shares")) {
      return formatShares(value);
    }
    if (key.endsWith("_date") || key === "as_of") {
      return formatDate(String(value));
    }
    return value.toLocaleString("zh-CN");
  }
  if (typeof value === "string") {
    if (key.endsWith("_date") || key === "as_of") {
      return formatDate(value);
    }
    if (key === "direction") {
      return value === "buy" ? "买入" : value === "sell" ? "卖出" : value;
    }
    if (key === "status") {
      return statusLabel(value);
    }
    if (key === "path") {
      return pathLabel({ key: value });
    }
    if (key === "synthetic") {
      return value === "true" ? "是" : "否";
    }
    return value;
  }
  return "—";
}

export function ScalarFields({
  record,
  exclude = [],
  emptyText = "暂无更多信息。",
}: {
  record: Record<string, unknown> | null | undefined;
  exclude?: string[];
  emptyText?: string;
}) {
  if (!record) {
    return <p className="muted">{emptyText}</p>;
  }
  const entries = Object.entries(record).filter(
    ([key, value]) => !exclude.includes(key) && shouldShowField(key, value),
  );
  if (entries.length === 0) {
    return <p className="muted">{emptyText}</p>;
  }
  return (
    <dl className="details">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{fieldLabel(key)}</dt>
          <dd>{formatFieldValue(key, value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function sourceLabel(source: { synthetic: boolean; code: string } | null | undefined): string {
  if (!source) {
    return "来源待确认";
  }
  if (source.synthetic) {
    return "合成示例数据";
  }
  return source.code === "tushare" ? "真实数据（Tushare）" : `真实数据（${source.code}）`;
}
