import type {
  Comparison,
  ComparisonPath,
  ComparisonPoint,
  ComparisonRestriction,
  CreatePracticeInput,
  DemoPreparation,
  MarketPoint,
  MarketSeries,
  OrderPreview,
  PracticeDecision,
  PracticeOptions,
  PracticeOrder,
  PracticePosition,
  PracticeReport,
  PracticeSummary,
  PracticeView,
  ResearchContextAvailability,
  ResearchSourceContext,
} from "./types";

const REASON_MESSAGES: Record<string, string> = {
  invalid_request: "提交的内容不符合要求，请检查输入后重试。",
  validation_error: "提交的内容不符合要求，请检查输入后重试。",
  invalid_payload: "提交的内容不符合要求，请检查输入后重试。",
  not_found: "找不到这个模拟，可能已被删除或地址有误。",
  practice_not_found: "找不到这个模拟，可能已被删除或地址有误。",
  revision_conflict: "页面状态已过期，已重新读取最新进度；请确认后再试。",
  stale_revision: "页面状态已过期，已重新读取最新进度；请确认后再试。",
  expected_revision_mismatch: "页面状态已过期，已重新读取最新进度；请确认后再试。",
  conflict: "页面状态已过期，已重新读取最新进度；请确认后再试。",
  idempotency_conflict: "这次请求与之前的请求编号冲突，请重新打开表单后再试。",
  session_finished: "模拟已结束，不能再交易或推进。",
  practice_finished: "模拟已结束，不能再交易或推进。",
  session_not_running: "模拟尚未开始或已经结束，当前不能执行此操作。",
  data_unavailable: "当前数据版本没有可用行情，暂时无法完成此操作。",
  data_not_ready: "当前数据版本尚未就绪，暂时无法完成此操作。",
  no_data: "当前数据版本没有可用行情，暂时无法完成此操作。",
  demo_unavailable: "合成示例数据暂不可用。",
  rate_limited: "请求过于频繁，请稍后再试。",
  internal_error: "服务端处理失败，请稍后重试。",
  server_error: "服务端处理失败，请稍后重试。",
  unavailable: "服务暂时不可用，请稍后重试。",
  name_required: "请填写 1 到 120 个字符的模拟名称。",
  date_range_invalid: "结束日期不能早于开始日期。",
  cash_invalid: "起始现金必须为非负数。",
  data_version_required: "请选择一个数据版本。",
  data_version_unavailable: "所选数据版本不存在或尚未发布。",
  source_version_mismatch: "数据来源与所选版本不一致，请重新选择。",
  start_date_unavailable: "开始日期必须位于所选版本的交易日范围内。",
  date_range_unavailable: "所选日期范围没有已发布交易日。",
  settings_invalid: "模拟设置无效，请刷新后重试。",
  rebalance_schedule_frozen: "系统路径当前固定每 5 个已揭晓交易日检查一次。",
  rule_bundle_unavailable: "规则版本不可用，请重新读取数据选项后重试。",
  positions_invalid: "期初持仓无效，请检查证券、股数和成本。",
  seed_snapshot_unavailable: "该数据版本没有可用的历史股票池快照。",
  comparison_unavailable: "对照引擎尚未就绪，暂时不能创建、推进或结束模拟。",
  demo_seed_invalid: "合成示例数据不完整，暂时不能使用。",
  order_invalid: "订单参数无效，请检查证券、方向和股数。",
  security_not_in_version: "该证券不属于当前模拟冻结的数据版本。",
  insufficient_sellable_shares: "卖出数量超过已知可卖持仓，或持仓仍受限制。",
  no_future_trade_date: "模拟区间内已经没有下一交易日。",
  insufficient_cash: "按已揭晓价格估算，当前现金不足。",
  decision_text_required: "判断、依据和退出条件均须填写原文。",
  decision_date_future: "决策日期不能晚于已揭晓日。",
  decision_correction_target_missing: "要更正的决策记录不存在。",
  expected_revision_required: "页面缺少版本号，请刷新后重试。",
  idempotency_key_required: "请求编号缺失，请重新打开表单后再试。",
};

export class PracticeApiError extends Error {
  readonly reason: string;
  readonly status: number;
  readonly detail: string | null;

  constructor(message: string, reason: string, status: number, detail: string | null) {
    super(message);
    this.name = "PracticeApiError";
    this.reason = reason;
    this.status = status;
    this.detail = detail;
  }
}

export function isRevisionConflict(error: unknown): boolean {
  if (!(error instanceof PracticeApiError)) {
    return false;
  }
  return (
    error.reason === "revision_conflict" ||
    error.reason === "stale_revision" ||
    error.reason === "expected_revision_mismatch"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseMaybeJson(value: unknown): Record<string, unknown> {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value === "string") {
    try {
      const parsed: unknown = JSON.parse(value);
      return isRecord(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function integerOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && /^-?\d+$/.test(value.trim())) {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : null;
  }
  return null;
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function bool(value: unknown): boolean {
  return value === true || value === 1 || value === "true";
}

function textList(value: unknown): string[] {
  return asArray(value)
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (isRecord(item)) {
        const code = text(item.code);
        const detail = text(item.detail);
        if (code && detail) {
          return `${code}：${detail}`;
        }
        return detail ?? code;
      }
      return null;
    })
    .filter((item): item is string => Boolean(item));
}

function describeFailure(status: number, body: unknown): PracticeApiError {
  if (isRecord(body) && body.ok === false) {
    const reason = text(body.reason) ?? "unknown";
    const detail = text(body.detail);
    const message =
      REASON_MESSAGES[reason] ??
      (status === 404
        ? "找不到请求的资源。"
        : status === 409
          ? "当前操作与最新状态冲突，请刷新后重试。"
          : "请求未能完成，请稍后重试。");
    return new PracticeApiError(message, reason, status, detail);
  }
  if (status === 404) {
    return new PracticeApiError("找不到请求的资源，请确认地址后重试。", "not_found", status, null);
  }
  return new PracticeApiError(`请求未能完成（HTTP ${status}），请稍后重试。`, "http_error", status, null);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init?.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  let response: Response;
  try {
    response = await fetch(path, { ...init, cache: "no-store", headers });
  } catch {
    throw new PracticeApiError(
      "无法连接本地后端服务，请确认后端已启动后重试。",
      "network_error",
      0,
      null,
    );
  }
  const raw = await response.text();
  let body: unknown = null;
  if (raw.length > 0) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    throw describeFailure(response.status, body);
  }
  if (body === null) {
    throw new PracticeApiError("服务返回了无法解析的内容，请稍后重试。", "invalid_response", response.status, null);
  }
  if (isRecord(body) && body.ok === false) {
    throw describeFailure(response.status, body);
  }
  return body as T;
}

function normalizeSource(raw: unknown): PracticeView["source"] {
  const record = isRecord(raw) ? raw : {};
  return {
    code: text(record.code) ?? text(record.source_code) ?? "unknown",
    version_id: text(record.version_id) ?? "",
    synthetic: bool(record.synthetic),
    limitations: textList(record.limitations),
  };
}

function normalizePosition(raw: unknown): PracticePosition | null {
  const record = isRecord(raw) ? raw : {};
  const tsCode = text(record.ts_code);
  if (!tsCode) {
    return null;
  }
  return {
    ts_code: tsCode,
    shares: integerOrNull(record.shares) ?? 0,
    sellable_shares: integerOrNull(record.sellable_shares),
    total_cost_cents: integerOrNull(record.total_cost_cents),
    acquire_date: text(record.acquire_date),
    locked: bool(record.locked),
  };
}

function normalizeOrder(raw: unknown): PracticeOrder | null {
  const record = isRecord(raw) ? raw : {};
  const tsCode = text(record.ts_code);
  if (!tsCode) {
    return null;
  }
  return {
    order_id: text(record.order_id) ?? `order-${tsCode}-${text(record.trade_date) ?? "unknown"}`,
    trade_date: text(record.trade_date) ?? "",
    ts_code: tsCode,
    direction: text(record.direction) ?? "buy",
    planned_shares: integerOrNull(record.planned_shares) ?? 0,
    filled_shares: integerOrNull(record.filled_shares),
    remaining_shares: integerOrNull(record.remaining_shares),
    status: text(record.status) ?? "pending",
    reason_code: text(record.reason_code),
  };
}

function normalizeRestrictions(value: unknown): ComparisonRestriction[] {
  return asArray(value)
    .map((item) => {
      if (typeof item === "string") {
        return { code: item, detail: null };
      }
      if (isRecord(item)) {
        const code = text(item.code);
        if (!code) {
          return null;
        }
        return { code, detail: text(item.detail) };
      }
      return null;
    })
    .filter((item): item is ComparisonRestriction => item !== null);
}

function normalizePoint(raw: unknown): ComparisonPoint | null {
  const record = isRecord(raw) ? raw : {};
  const date = text(record.date);
  if (!date) {
    return null;
  }
  return {
    date,
    nav_cents: integerOrNull(record.nav_cents),
    return_bps: integerOrNull(record.return_bps),
  };
}

function normalizePath(raw: unknown): ComparisonPath | null {
  const record = isRecord(raw) ? raw : {};
  const key = text(record.key);
  if (!key) {
    return null;
  }
  return {
    key,
    label: text(record.label) ?? "",
    nav_cents: integerOrNull(record.nav_cents),
    return_bps: integerOrNull(record.return_bps),
    drawdown_bps: integerOrNull(record.drawdown_bps),
    cost_cents: integerOrNull(record.cost_cents),
    trade_count: integerOrNull(record.trade_count),
    turnover_bps: integerOrNull(record.turnover_bps),
    concentration_bps: integerOrNull(record.concentration_bps),
    points: asArray(record.points)
      .map(normalizePoint)
      .filter((point): point is ComparisonPoint => point !== null),
    restrictions: normalizeRestrictions(record.restrictions),
  };
}

export function normalizeComparison(raw: unknown): Comparison | null {
  if (!isRecord(raw)) {
    return null;
  }
  const paths = asArray(raw.paths)
    .map(normalizePath)
    .filter((path): path is ComparisonPath => path !== null);
  if (paths.length === 0 && !text(raw.as_of)) {
    return null;
  }
  return {
    as_of: text(raw.as_of),
    paths,
    contributions: asArray(raw.contributions),
    limitations: textList(raw.limitations),
  };
}

function normalizeDecision(raw: unknown, index: number): PracticeDecision | null {
  const record = isRecord(raw) ? raw : {};
  const judgment = text(record.judgment);
  if (!judgment) {
    return null;
  }
  return {
    decision_id: text(record.decision_id) ?? `decision-${index + 1}`,
    judgment,
    evidence: text(record.evidence),
    exit_condition: text(record.exit_condition),
    decision_date: text(record.decision_date),
    recorded_at: text(record.recorded_at),
    correction:
      bool(record.correction) || bool(record.is_backfill) || text(record.corrects_decision_id) !== null,
  };
}

function normalizeDecisionList(raw: unknown): PracticeDecision[] {
  return asArray(raw)
    .map(normalizeDecision)
    .filter((item): item is PracticeDecision => item !== null);
}

export function normalizeSession(raw: unknown): PracticeView {
  const record = isRecord(raw) ? raw : {};
  const practiceId = text(record.practice_id) ?? text(record.id);
  if (!practiceId) {
    throw new PracticeApiError(
      "服务返回的模拟数据缺少标识，请刷新页面后重试。",
      "invalid_response",
      200,
      null,
    );
  }
  const userState = isRecord(record.user_state) ? record.user_state : {};
  const settings = isRecord(record.settings) ? record.settings : {};
  return {
    practice_id: practiceId,
    name: text(record.name) ?? "未命名模拟",
    status: text(record.status) ?? "running",
    revision: integerOrNull(record.revision) ?? 1,
    start_date: text(record.start_date) ?? "",
    end_date: text(record.end_date) ?? "",
    revealed_date: text(record.revealed_date),
    next_trade_date: text(record.next_trade_date),
    settings,
    source: normalizeSource(record.source),
    user_state: {
      cash_cents: integerOrNull(userState.cash_cents),
      positions: asArray(userState.positions)
        .map(normalizePosition)
        .filter((item): item is PracticePosition => item !== null),
      orders: asArray(userState.orders)
        .map(normalizeOrder)
        .filter((item): item is PracticeOrder => item !== null),
    },
    comparison: normalizeComparison(record.comparison),
    decisions: normalizeDecisionList(record.decisions),
  };
}

function normalizeSummary(raw: unknown): PracticeSummary | null {
  const record = isRecord(raw) ? raw : {};
  const practiceId = text(record.practice_id) ?? text(record.id);
  if (!practiceId) {
    return null;
  }
  return {
    practice_id: practiceId,
    name: text(record.name) ?? "未命名模拟",
    status: text(record.status) ?? "running",
    start_date: text(record.start_date) ?? "",
    end_date: text(record.end_date) ?? "",
    revealed_date: text(record.revealed_date),
    next_trade_date: text(record.next_trade_date),
    source: isRecord(record.source) ? normalizeSource(record.source) : null,
  };
}

export function normalizeOptions(raw: unknown): PracticeOptions {
  const record = isRecord(raw) ? raw : {};
  const defaults = isRecord(record.defaults) ? record.defaults : {};

  const sources = asArray(record.sources)
    .map((item) => {
      if (typeof item === "string") {
        return { code: item, label: item, synthetic: false, limitations: [] };
      }
      const entry = isRecord(item) ? item : {};
      const code = text(entry.code) ?? text(entry.source_code);
      if (!code) {
        return null;
      }
      return {
        code,
        label: text(entry.label) ?? text(entry.name) ?? code,
        synthetic: bool(entry.synthetic),
        limitations: textList(entry.limitations),
      };
    })
    .filter((item): item is PracticeOptions["sources"][number] => item !== null);

  const versions = asArray(record.versions)
    .map((item) => {
      if (typeof item === "string") {
        return {
          version_id: item,
          label: "",
          source_code: "",
          synthetic: false,
          start_date: null,
          end_date: null,
        };
      }
      const entry = isRecord(item) ? item : {};
      const versionId = text(entry.version_id) ?? text(entry.id);
      if (!versionId) {
        return null;
      }
      const scope = parseMaybeJson(entry.scope);
      const sourceCode = text(entry.source_code) ?? text(entry.source) ?? "";
      return {
        version_id: versionId,
        label: text(entry.label) ?? text(entry.name) ?? "",
        source_code: sourceCode,
        synthetic:
          bool(entry.synthetic) || sourceCode === "synthetic" || sourceCode === "tushare-fixture",
        start_date:
          text(entry.start_date) ??
          text(entry.start) ??
          text(scope.start_date) ??
          text(scope.start) ??
          text(entry.window_start),
        end_date:
          text(entry.end_date) ??
          text(entry.end) ??
          text(scope.end_date) ??
          text(scope.end) ??
          text(entry.window_end),
      };
    })
    .filter((item): item is PracticeOptions["versions"][number] => item !== null);

  const symbols = asArray(record.symbols)
    .map((item) => {
      if (typeof item === "string") {
        return { ts_code: item, name: "" };
      }
      const entry = isRecord(item) ? item : {};
      const tsCode = text(entry.ts_code) ?? text(entry.code);
      if (!tsCode) {
        return null;
      }
      return {
        ts_code: tsCode,
        name: text(entry.name) ?? "",
      };
    })
    .filter((item): item is PracticeOptions["symbols"][number] => item !== null);

  return {
    sources,
    versions,
    symbols,
    defaults: {
      start_date: text(defaults.start_date),
      end_date: text(defaults.end_date),
      data_version_id: text(defaults.data_version_id),
      source_code: text(defaults.source_code),
      name: text(defaults.name),
    },
    demo_available: record.demo_available === undefined ? true : bool(record.demo_available),
    limitations: textList(record.limitations),
  };
}

export async function fetchOptions(): Promise<PracticeOptions> {
  const raw = await request<unknown>("/api/practice/options");
  return normalizeOptions(isRecord(raw) && "options" in raw ? raw.options : raw);
}

export async function prepareDemo(): Promise<DemoPreparation> {
  const raw = await request<unknown>("/api/practice/demo", {
    method: "POST",
    body: JSON.stringify({}),
  });
  const record = isRecord(raw) ? raw : {};
  const demoRecord = isRecord(record.demo) ? record.demo : record;
  const example = isRecord(demoRecord.example) ? demoRecord.example : {};
  const optionsRaw = isRecord(demoRecord.options)
    ? demoRecord.options
    : isRecord(record.options)
      ? record.options
      : Array.isArray(record.versions) || Array.isArray(record.sources)
        ? record
        : null;
  const options = optionsRaw ? normalizeOptions(optionsRaw) : null;
  const pick = (...keys: string[]): string | null => {
    for (const key of keys) {
      const value = text(demoRecord[key]) ?? text(example[key]) ?? text(record[key]);
      if (value) {
        return value;
      }
    }
    return null;
  };
  return {
    options,
    startDate: pick("start_date", "example_start_date", "start") ?? options?.defaults.start_date ?? null,
    endDate: pick("end_date", "example_end_date", "end") ?? options?.defaults.end_date ?? null,
    dataVersionId:
      pick("data_version_id", "version_id") ?? options?.defaults.data_version_id ?? null,
    sourceCode: pick("source_code", "source") ?? options?.defaults.source_code ?? null,
    limitations: textList(demoRecord.limitations ?? record.limitations),
  };
}

export async function fetchSessions(
  limit = 50,
  offset = 0,
): Promise<{ sessions: PracticeSummary[]; total: number }> {
  const raw = await request<unknown>(
    `/api/practice/sessions?limit=${encodeURIComponent(String(limit))}&offset=${encodeURIComponent(String(offset))}`,
  );
  const record = isRecord(raw) ? raw : {};
  const sessions = asArray(record.sessions)
    .map(normalizeSummary)
    .filter((item): item is PracticeSummary => item !== null);
  return {
    sessions,
    total: integerOrNull(record.total) ?? sessions.length,
  };
}

export async function createPractice(input: CreatePracticeInput): Promise<PracticeView> {
  const raw = await request<unknown>("/api/practice/sessions", {
    method: "POST",
    body: JSON.stringify(input),
  });
  const record = isRecord(raw) ? raw : {};
  return normalizeSession(record.session ?? raw);
}

export async function fetchSession(practiceId: string): Promise<PracticeView> {
  const raw = await request<unknown>(`/api/practice/sessions/${encodeURIComponent(practiceId)}`);
  const record = isRecord(raw) ? raw : {};
  return normalizeSession(record.session ?? raw);
}

export async function fetchResearchSourceContext(
  practiceId: string,
  expectedRevision: number,
): Promise<ResearchContextAvailability> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/research-context?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
  );
  const record = isRecord(raw) ? raw : {};
  if (record.availability === "available" && isRecord(record.source_context)) {
    return { availability: "available", source_context: record.source_context as unknown as ResearchSourceContext };
  }
  if (record.availability === "supplementation_required") {
    return {
      availability: "supplementation_required",
      reason: typeof record.reason === "string" ? record.reason : "historical_context_incomplete",
      missing_fields: asArray(record.missing_fields).filter((field): field is string => typeof field === "string"),
    };
  }
  throw new Error("练习研究来源上下文响应格式无效。");
}

export async function restartPractice(practiceId: string, idempotencyKey: string): Promise<PracticeView> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/restart`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    },
  );
  const record = isRecord(raw) ? raw : {};
  return normalizeSession(record.session ?? raw);
}

export async function previewOrder(
  practiceId: string,
  input: { ts_code: string; direction: string; planned_shares: number },
): Promise<OrderPreview> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/orders/preview`,
    { method: "POST", body: JSON.stringify(input) },
  );
  const record = isRecord(raw) ? raw : {};
  return (isRecord(record.preview) ? record.preview : record) as OrderPreview;
}

export async function placeOrder(
  practiceId: string,
  input: {
    ts_code: string;
    direction: string;
    planned_shares: number;
    idempotency_key: string;
    expected_revision: number;
  },
): Promise<PracticeView> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/orders`,
    { method: "POST", body: JSON.stringify(input) },
  );
  const record = isRecord(raw) ? raw : {};
  return normalizeSession(record.session ?? raw);
}

export async function advancePractice(
  practiceId: string,
  idempotencyKey: string,
  expectedRevision: number,
): Promise<PracticeView> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/advance`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, expected_revision: expectedRevision }),
    },
  );
  const record = isRecord(raw) ? raw : {};
  return normalizeSession(record.session ?? raw);
}

export async function finishPractice(
  practiceId: string,
  idempotencyKey: string,
  expectedRevision: number,
): Promise<{ session: PracticeView; report: PracticeReport | null }> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/finish`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, expected_revision: expectedRevision }),
    },
  );
  const record = isRecord(raw) ? raw : {};
  const session = normalizeSession(record.session ?? record);
  const normalized = normalizeReport(record.report);
  const report = normalized
    ? {
        ...normalized,
        decisions:
          normalized.decisions.length > 0 ? normalized.decisions : session.decisions,
        summary:
          Object.keys(normalized.summary).length > 0
            ? normalized.summary
            : { as_of: session.revealed_date, status: session.status },
      }
    : null;
  return { session, report };
}

export async function fetchDecisions(practiceId: string): Promise<PracticeDecision[]> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/decisions`,
  );
  const record = isRecord(raw) ? raw : {};
  return normalizeDecisionList(record.decisions);
}

export async function createDecision(
  practiceId: string,
  input: {
    judgment: string;
    evidence: string;
    exit_condition: string;
    decision_date?: string;
    idempotency_key: string;
  },
): Promise<void> {
  await request<unknown>(`/api/practice/sessions/${encodeURIComponent(practiceId)}/decisions`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function fetchMarket(practiceId: string, code: string): Promise<MarketSeries> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/market?code=${encodeURIComponent(code)}`,
  );
  const record = isRecord(raw) ? raw : {};
  const points = asArray(record.points)
    .map((item): MarketPoint | null => {
      const point = isRecord(item) ? item : {};
      const date = text(point.date);
      if (!date) {
        return null;
      }
      return {
        date,
        open: numberOrNull(point.open),
        high: numberOrNull(point.high),
        low: numberOrNull(point.low),
        close: numberOrNull(point.close),
        volume: numberOrNull(point.volume),
      };
    })
    .filter((item): item is MarketPoint => item !== null);
  return {
    code: text(record.code) ?? code,
    as_of: text(record.as_of),
    points,
    limitations: textList(record.limitations),
  };
}

export function normalizeReport(raw: unknown): PracticeReport | null {
  if (!isRecord(raw)) {
    return null;
  }
  if (!("comparison" in raw) && !("summary" in raw) && !("decisions" in raw)) {
    const comparison = normalizeComparison(raw);
    if (!comparison) {
      return null;
    }
    return {
      comparison,
      decisions: [],
      summary: {},
      limitations: textList(raw.limitations),
    };
  }
  return {
    comparison: normalizeComparison(raw.comparison),
    decisions: normalizeDecisionList(raw.decisions),
    summary: isRecord(raw.summary) ? raw.summary : {},
    limitations: textList(raw.limitations),
  };
}

export async function fetchReport(practiceId: string): Promise<PracticeReport | null> {
  const raw = await request<unknown>(
    `/api/practice/sessions/${encodeURIComponent(practiceId)}/report`,
  );
  const record = isRecord(raw) ? raw : {};
  return normalizeReport(record.report ?? record);
}
