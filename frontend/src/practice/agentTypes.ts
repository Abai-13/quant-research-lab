export type AgentLimits = { rounds: number; tool_calls: number; timeout_seconds: number; tokens: number };
export type AgentConfig = {
  config_version: string; name: string; protocol: string; base_url: string; model: string;
  key_configured: boolean; key_masked: string | null; allow_loopback_http: boolean;
  trusted_private_https: boolean; limits: AgentLimits;
};
export type AgentConfigResponse = { configured: boolean; config: AgentConfig | null; limits: AgentLimits };
export type AgentDisclosure = {
  config_version: string; target: { base_url: string; model: string; name: string };
  context: Record<string, unknown>; data_categories: string[]; purpose: string;
  possible_cost: boolean; limits: AgentLimits; consent_digest: string;
};
export type AgentUsage = {
  input_tokens: number | null; output_tokens: number | null; total_tokens: number | null;
  model_calls: number; tool_calls: number; elapsed_ms: number; estimated_cost: number | null;
};
export type AgentEvidence = {
  evidence_id?: string; tool_name?: string; as_of?: string | null; data?: unknown;
  version?: unknown; version_id?: string | null; summary?: string; [key: string]: unknown;
};
export type AgentDraft = {
  draft_id: string; kind: "setup" | "trade"; practice_id: string | null; plan_id?: string | null; expected_plan_version?: number | null;
  expected_revision: number | null; revealed_date: string | null; context_fingerprint: string;
  payload: Record<string, unknown>; limitations: string[];
};
export type AgentResult = { summary: string; evidence: AgentEvidence[]; drafts: AgentDraft[]; limitations: string[] };
export type AgentTask = {
  task_id: string; practice_id: string | null; plan_id?: string | null; expected_plan_version?: number | null; scope: "setup" | "simulation"; status: string;
  goal: string; config_version: string; context: Record<string, unknown>; created_at: string;
  updated_at: string; parent_task_id: string | null; stop_reason: string | null; usage: AgentUsage;
  result: AgentResult | null; stale: boolean;
  operation_id?: string | null; generation?: number; resumable?: boolean;
};
export type AgentEvent = {
  seq: number; kind: string; created_at: string; tool_name: string | null;
  summary: string; evidence_ids: string[]; reason: string | null;
};
