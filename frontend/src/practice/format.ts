const CENT_FACTOR = 100n;

function pad2(value: number): string {
  return value.toString().padStart(2, "0");
}

export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) {
    return "—";
  }
  const value = BigInt(Math.trunc(cents));
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const whole = absolute / CENT_FACTOR;
  const fraction = absolute % CENT_FACTOR;
  const grouped = whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}.${pad2(Number(fraction))}`;
}

export function formatBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) {
    return "—";
  }
  const value = Math.trunc(bps);
  const negative = value < 0;
  const absolute = Math.abs(value);
  const whole = Math.trunc(absolute / 100);
  const fraction = absolute % 100;
  return `${negative ? "-" : "+"}${whole}.${pad2(fraction)}%`;
}

export function formatWeightBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) {
    return "—";
  }
  const absolute = Math.abs(Math.trunc(bps));
  return `${Math.trunc(absolute / 100)}.${pad2(absolute % 100)}%`;
}

export function formatShares(shares: number | null | undefined): string {
  if (shares === null || shares === undefined || !Number.isFinite(shares)) {
    return "—";
  }
  return Math.trunc(shares).toLocaleString("zh-CN");
}

function parseDateParts(value: string | null | undefined): { year: number; month: number; day: number } | null {
  if (!value) {
    return null;
  }
  const digits = value.replace(/\D/g, "");
  if (digits.length !== 8) {
    return null;
  }
  const year = Number(digits.slice(0, 4));
  const month = Number(digits.slice(4, 6));
  const day = Number(digits.slice(6, 8));
  if (!Number.isInteger(year) || month < 1 || month > 12 || day < 1 || day > 31) {
    return null;
  }
  return { year, month, day };
}

export function formatDate(value: string | null | undefined): string {
  const parts = parseDateParts(value);
  if (!parts) {
    return "—";
  }
  return `${parts.year}年${parts.month}月${parts.day}日`;
}

export function formatDateShort(value: string | null | undefined): string {
  const parts = parseDateParts(value);
  if (!parts) {
    return "—";
  }
  return `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return formatDate(value);
  }
  const y = date.getFullYear();
  const m = pad2(date.getMonth() + 1);
  const d = pad2(date.getDate());
  const hh = pad2(date.getHours());
  const mm = pad2(date.getMinutes());
  return `${y}-${m}-${d} ${hh}:${mm}`;
}

export function toApiDate(inputDate: string | null | undefined): string | null {
  if (!inputDate) {
    return null;
  }
  const parts = parseDateParts(inputDate);
  if (!parts) {
    return null;
  }
  return `${parts.year}${pad2(parts.month)}${pad2(parts.day)}`;
}

export function toInputDate(apiDate: string | null | undefined): string {
  const parts = parseDateParts(apiDate);
  if (!parts) {
    return "";
  }
  return `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}`;
}

export function parseYuanToCents(raw: string, allowEmpty = false): number | null {
  const normalized = raw.trim().replace(/,/g, "");
  if (normalized === "") {
    if (allowEmpty) {
      return null;
    }
    throw new Error("金额不能为空。");
  }
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) {
    throw new Error("金额需为非负数，最多两位小数；不接受负号或科学计数法。");
  }
  const [whole, fraction = ""] = normalized.split(".");
  const cents = BigInt(whole) * CENT_FACTOR + BigInt((fraction + "00").slice(0, 2));
  if (cents > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error("金额超出可安全表示的范围。");
  }
  return Number(cents);
}

export function parseShares(raw: string, allowZero = false): number {
  const normalized = raw.trim().replace(/,/g, "");
  if (normalized === "") {
    throw new Error("股数不能为空。");
  }
  if (!/^\d+$/.test(normalized)) {
    throw new Error("股数需为非负整数。");
  }
  const shares = Number(normalized);
  if (!Number.isSafeInteger(shares)) {
    throw new Error("股数超出可安全表示的范围。");
  }
  if (!allowZero && shares === 0) {
    throw new Error("股数必须大于 0。");
  }
  return shares;
}

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(2);
}

export function statusLabel(status: string | null | undefined): string {
  switch (status) {
    case "created":
    case "draft":
      return "待开始";
    case "running":
    case "active":
    case "in_progress":
      return "进行中";
    case "finished":
    case "closed":
    case "completed":
      return "已结束";
    case "pending":
      return "待处理";
    case "filled":
      return "已成交";
    case "partially_filled":
      return "部分成交";
    case "unfilled":
      return "未成交";
    case "rejected":
      return "已拒绝";
    case "blocked":
      return "受限";
    case "expired":
      return "已失效";
    case "cancelled":
    case "canceled":
      return "已取消";
    default:
      return status ? "状态待确认" : "—";
  }
}

export function directionLabel(direction: string | null | undefined): string {
  if (direction === "buy") {
    return "买入";
  }
  if (direction === "sell") {
    return "卖出";
  }
  return direction ?? "—";
}

export function pathLabel(path: { key: string; label?: string | null }): string {
  if (path.label) {
    return path.label;
  }
  switch (path.key) {
    case "user":
      return "我的操作";
    case "incremental":
      return "系统渐进建议";
    case "independent":
      return "系统独立建议";
    case "hold":
      return "持有不动";
    default:
      return "对照路径";
  }
}

export function isFinished(status: string): boolean {
  return status === "finished" || status === "closed" || status === "completed";
}

export function isRunning(status: string): boolean {
  return status === "running" || status === "active" || status === "in_progress";
}
