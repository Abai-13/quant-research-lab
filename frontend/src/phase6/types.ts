export type OperationStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
export type OperationView = {
  operation_id: string; kind: string; status: OperationStatus; attempt: number; generation: number;
  progress: { phase: string; completed_units: number; total_units: number | null };
  checkpoint: { sequence: number; steps: Array<{ step_id: string; state: string; result_digest?: string; reason?: string }>; validation: string; summary?: string; validation_status?: string };
  cumulative_usage: Record<string, unknown>; limits: Record<string, unknown>; resumable: boolean;
  stop_reason: string | null; created_at: string; updated_at: string; result?: PreparedDataset | Record<string, unknown> | null;
};
export type PreparedDataset = {
  source_code: string; synthetic: boolean; data_version_id: string | null; pool_snapshot_id?: string | null;
  symbols: string[]; start_date: string; end_date: string; content_digest: string;
  quality: Record<string, unknown>; eligibility: string | Record<string, unknown>;
  limitations: string[]; cache_hit: boolean;
};
export type DataPreset = { preset_id: string; name: string; start_date: string; end_date: string; symbols: string[]; source_code?: string; limitations?: string[] };
export type ResearchRecord = { record_id: string; revision_id: string; parent_revision_id?: string | null; kind: string; goal: string; hypothesis?: string; disconfirming_condition?: string; source_refs?: unknown[]; evidence_as_of?: string | null; scope?: string; symbols?: string[]; interval?: unknown; versions?: Record<string, unknown>; author_type?: string; created_at: string; limitations?: string[]; attempts?: unknown[]; body?: Record<string, unknown>; revision_history?: unknown[]; revision_history_total?: number };
export type ExperimentPlan = { plan_id: string; plan_revision: number; status: string; source_context?: Record<string, unknown>; training_dates?: string[]; evaluation_dates?: string[]; training_interval?: unknown; evaluation_interval?: unknown; warmup?: number; parameter_name?: string; candidates?: unknown[]; baseline_value?: unknown; primary_metric?: string; risk_constraints?: unknown; selection_rule?: string; budget?: Record<string, unknown>; limitations?: string[]; plan_digest?: string; exposure_snapshot_digest?: string };
export type ResearchSourceContext = {
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
};
export type ResearchContextAvailability =
  | { availability: "available"; source_context: ResearchSourceContext }
  | { availability: "supplementation_required"; reason: string; missing_fields: string[] };
export type CredentialPublic = { slot: string; provider?: string; target_fingerprint?: string | null; state: "missing" | "configured" | "unavailable" | "cleared"; storage_mode: "persistent" | "process" | null; configured: boolean; masked?: string | null; credential_generation: number; origin: "managed" | "external_environment" | "none"; reason?: string | null };
