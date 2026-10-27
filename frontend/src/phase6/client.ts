import type { CredentialPublic, DataPreset, ExperimentPlan, OperationView, ResearchContextAvailability, ResearchRecord, ResearchSourceContext } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, cache: "no-store", headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers } });
  } catch { throw new Error("无法连接本地服务。请确认后端已启动后重试。"); }
  const text = await response.text(); let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* handled below */ }
  if (!response.ok) {
    const value = body && typeof body === "object" ? body as Record<string, unknown> : {};
    const reason = typeof value.reason === "string" ? value.reason : "request_failed";
    const detail = typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`;
    throw new Error(`${reason}: ${detail}`);
  }
  if (body === null) throw new Error("服务返回了无法解析的内容。");
  return body as T;
}
const key = () => crypto.randomUUID();
const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const put = (body: unknown): RequestInit => ({ method: "PUT", body: JSON.stringify(body) });

export const getDataPresets = () => request<{ presets: DataPreset[] }>("/api/practice/data/presets");
export const prepareData = (body: Record<string, unknown>) => request<OperationView>("/api/practice/data/prepare", post({ ...body, idempotency_key: key() }));
export const getDataJob = (id: string) => request<OperationView>(`/api/practice/data/jobs/${encodeURIComponent(id)}`);
export const cancelDataJob = (id: string, generation: number) => request<OperationView>(`/api/practice/data/jobs/${encodeURIComponent(id)}/cancel`, post({ expected_generation: generation, idempotency_key: key() }));
export const resumeDataJob = (id: string, generation: number) => request<OperationView>(`/api/practice/data/jobs/${encodeURIComponent(id)}/resume`, post({ expected_generation: generation, idempotency_key: key() }));

function normalizeRecord(value: ResearchRecord): ResearchRecord {
  const body = value.body && typeof value.body === "object" ? value.body : {};
  return { ...body, ...value, goal: value.goal ?? String(body.goal ?? ""), hypothesis: value.hypothesis ?? String(body.hypothesis ?? ""), disconfirming_condition: value.disconfirming_condition ?? String(body.disconfirming_condition ?? ""), source_refs: value.source_refs ?? (Array.isArray(body.source_refs) ? body.source_refs : []), evidence_as_of: value.evidence_as_of ?? (typeof body.evidence_as_of === "string" ? body.evidence_as_of : null), scope: value.scope ?? (typeof body.scope === "string" ? body.scope : undefined) };
}
export const searchResearch = async (query: string, visibility?: { practiceId: string; asOf: string }) => {
  const scope = visibility ? `&practice_id=${encodeURIComponent(visibility.practiceId)}&as_of=${encodeURIComponent(visibility.asOf)}` : "";
  const result = await request<{ records: ResearchRecord[]; total?: number }>(`/api/research/records?limit=50&offset=0${query ? `&q=${encodeURIComponent(query)}` : ""}${scope}`);
  return { ...result, records: result.records.map(normalizeRecord) };
};
export const getResearchSourceContext = async (practiceId: string, expectedRevision: number): Promise<ResearchContextAvailability> => {
  const result = await request<ResearchContextAvailability>(`/api/practice/sessions/${encodeURIComponent(practiceId)}/research-context?expected_revision=${encodeURIComponent(String(expectedRevision))}`);
  if (result.availability === "available" && result.source_context && typeof result.source_context === "object") {
    return { availability: "available", source_context: result.source_context as ResearchSourceContext };
  }
  if (result.availability === "supplementation_required" && Array.isArray(result.missing_fields)) return result;
  throw new Error("服务端来源上下文响应格式无效。");
};
export const getResearchRecord = async (id: string, visibility?: { practiceId: string; asOf: string }) => {
  const scope = visibility ? `?practice_id=${encodeURIComponent(visibility.practiceId)}&as_of=${encodeURIComponent(visibility.asOf)}` : "";
  return normalizeRecord((await request<{ record: ResearchRecord }>(`/api/research/records/${encodeURIComponent(id)}${scope}`)).record);
};
export const reviseResearchRecord = async (id: string, body: Record<string, unknown>) => normalizeRecord((await request<{ revision: ResearchRecord }>(`/api/research/records/${encodeURIComponent(id)}/revisions`, post({ ...body, idempotency_key: key() }))).revision);
export const saveResearchRecord = async (body: Record<string, unknown>) => normalizeRecord((await request<{ record: ResearchRecord }>("/api/research/records", post({ ...body, idempotency_key: key() }))).record);
export const createExperimentPlan = async (body: Record<string, unknown> & { idempotency_key: string }) => (await request<{ plan: ExperimentPlan }>("/api/research/plans", post(body))).plan;
export const startExperiment = async (plan_id: string, plan_revision: number, idempotency_key: string) => (await request<{ operation: OperationView }>("/api/research/experiments", post({ plan_id, plan_revision, idempotency_key }))).operation;
export const listExperiments = async () => request<{ experiments: OperationView[]; total?: number }>("/api/research/experiments?limit=50&offset=0");
export const getExperiment = async (id: string) => { const result = await request<{ operation: OperationView; result?: Record<string, unknown> | null }>(`/api/research/experiments/${encodeURIComponent(id)}`); return { ...result.operation, result: result.result ?? null }; };
export const cancelExperiment = async (id: string, generation: number) => (await request<{ operation: OperationView }>(`/api/research/experiments/${encodeURIComponent(id)}/cancel`, post({ expected_generation: generation, idempotency_key: key() }))).operation;
export const resumeExperiment = async (id: string, generation: number) => (await request<{ operation: OperationView }>(`/api/research/experiments/${encodeURIComponent(id)}/resume`, post({ expected_generation: generation, idempotency_key: key() }))).operation;

export const listCredentials = () => request<{ credentials: CredentialPublic[] }>("/api/credentials");
export const saveCredential = async (slot: string, body: { secret: string; provider: string; target_fingerprint: string | null; storage_mode: "persistent" | "process"; expected_generation: number; idempotency_key: string }) => (await request<{ credential: CredentialPublic }>(`/api/credentials/${encodeURIComponent(slot)}`, put(body))).credential;
export const clearCredential = async (slot: string, generation: number) => (await request<{ credential: CredentialPublic }>(`/api/credentials/${encodeURIComponent(slot)}`, { method: "DELETE", body: JSON.stringify({ expected_generation: generation, idempotency_key: key() }), headers: { "Content-Type": "application/json" } })).credential;
export { key as newIdempotencyKey };
