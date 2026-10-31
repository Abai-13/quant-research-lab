export interface PracticeSource {
  code: string;
  version_id: string;
  synthetic: boolean;
  limitations: string[];
}

export interface ResearchSourceContext {
  practice_id: string;
  revision: number;
  revealed_date: string;
  data_version_id: string;
  source_code: string;
  source_instance_id: string;
  settings_fingerprint: string;
  algorithm_fingerprint: string;
  fee_fingerprint: string;
  segment_fingerprint: string;
}

export type ResearchContextAvailability =
  | { availability: "available"; source_context: ResearchSourceContext }
  | { availability: "supplementation_required"; reason: string; missing_fields: string[] };

export interface PracticePosition {
  ts_code: string;
  shares: number;
  sellable_shares: number | null;
  total_cost_cents: number | null;
  acquire_date: string | null;
  locked: boolean;
}

export interface PracticeOrder {
  order_id: string;
  trade_date: string;
  ts_code: string;
  direction: string;
  planned_shares: number;
  filled_shares: number | null;
  remaining_shares: number | null;
  status: string;
  reason_code: string | null;
}

export interface PracticeUserState {
  cash_cents: number | null;
  positions: PracticePosition[];
  orders: PracticeOrder[];
}

export interface ComparisonPoint {
  date: string;
  nav_cents: number | null;
  return_bps: number | null;
}

export interface ComparisonRestriction {
  code: string;
  detail: string | null;
}

export interface ComparisonPath {
  key: string;
  label: string;
  nav_cents: number | null;
  return_bps: number | null;
  drawdown_bps: number | null;
  cost_cents: number | null;
  trade_count: number | null;
  turnover_bps: number | null;
  concentration_bps: number | null;
  points: ComparisonPoint[];
  restrictions: ComparisonRestriction[];
}

export interface Comparison {
  as_of: string | null;
  paths: ComparisonPath[];
  contributions: unknown[];
  limitations: string[];
}

export interface PracticeDecision {
  decision_id: string;
  judgment: string;
  evidence: string | null;
  exit_condition: string | null;
  decision_date: string | null;
  recorded_at: string | null;
  correction: boolean;
}

export interface PracticeSettings {
  rebalance_every_n_days?: number;
  hide_system_hints?: boolean;
  [key: string]: unknown;
}

export interface PracticeView {
  practice_id: string;
  name: string;
  status: string;
  revision: number;
  start_date: string;
  end_date: string;
  revealed_date: string | null;
  next_trade_date: string | null;
  settings: PracticeSettings;
  source: PracticeSource;
  user_state: PracticeUserState;
  comparison: Comparison | null;
  decisions: PracticeDecision[];
}

export interface PracticeSummary {
  practice_id: string;
  name: string;
  status: string;
  start_date: string;
  end_date: string;
  revealed_date: string | null;
  next_trade_date: string | null;
  source: PracticeSource | null;
}

export interface PracticeOptionSource {
  code: string;
  label: string;
  synthetic: boolean;
  limitations: string[];
}

export interface PracticeOptionVersion {
  version_id: string;
  label: string;
  source_code: string;
  synthetic: boolean;
  start_date: string | null;
  end_date: string | null;
}

export interface PracticeOptionSymbol {
  ts_code: string;
  name: string;
}

export interface PracticeDefaults {
  start_date: string | null;
  end_date: string | null;
  data_version_id: string | null;
  source_code: string | null;
  name: string | null;
  cash_cents?: number | null;
  watchlist_symbols?: string[];
}

export interface PracticeOptions {
  sources: PracticeOptionSource[];
  versions: PracticeOptionVersion[];
  symbols: PracticeOptionSymbol[];
  defaults: PracticeDefaults;
  demo_available: boolean;
  limitations: string[];
}

export interface DemoPreparation {
  options: PracticeOptions | null;
  startDate: string | null;
  endDate: string | null;
  dataVersionId: string | null;
  sourceCode: string | null;
  limitations: string[];
}

export interface NewPositionInput {
  ts_code: string;
  shares: number;
  total_cost_cents: number | null;
  sellable_shares: number;
  acquire_date: string;
}

export interface CreatePracticeInput {
  name: string;
  start_date: string;
  end_date: string;
  cash_cents: number;
  positions: NewPositionInput[];
  data_version_id: string;
  source_code: string;
  settings: PracticeSettings;
  idempotency_key: string;
}

export type PlanPayload = Omit<CreatePracticeInput, "idempotency_key">;

export interface PracticePlan {
  plan_id: string;
  version: number;
  status: "draft" | "confirmed";
  payload: PlanPayload;
  symbols: string[];
  goal: string;
  practice_id: string | null;
  created_at: string;
  updated_at: string;
  qualification: {
    ready: boolean;
    issues: Array<{ code: string; detail: string }>;
    limitations: string[];
    start_date: string | null;
    end_date: string | null;
  };
}

export interface WorkspaceRow {
  ts_code: string;
  name: string;
  price_cents: number | null;
  price_date: string | null;
  change_bps: number | null;
  change_base_date?: string | null;
  shares: number | null;
  sellable_shares: number | null;
  market_value_cents: number | null;
  pnl_cents: number | null;
  last_agent_action: string | null;
}

export interface PracticeSessionWorkspace {
  practice_id: string;
  revision: number;
  as_of: string | null;
  next_trade_date: string | null;
  source: PracticeSource;
  rows: WorkspaceRow[];
  limitations: string[];
}

export interface OrderPreview {
  ts_code?: string;
  direction?: string;
  planned_shares?: number;
  trade_date?: string | null;
  price_cents?: number | null;
  estimated_price_cents?: number | null;
  gross_cents?: number | null;
  estimated_gross_cents?: number | null;
  fee_cents?: number | null;
  estimated_fee_cents?: number | null;
  cash_delta_cents?: number | null;
  estimated_cash_delta_cents?: number | null;
  remaining_cash_cents?: number | null;
  restrictions?: Array<string | ComparisonRestriction>;
  limitations?: string[];
  warnings?: string[];
  [key: string]: unknown;
}

export interface MarketPoint {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export interface MarketSeries {
  code: string;
  as_of: string | null;
  points: MarketPoint[];
  limitations: string[];
}

export interface PracticeReport {
  comparison: Comparison | null;
  decisions: PracticeDecision[];
  summary: Record<string, unknown>;
  limitations: string[];
}
