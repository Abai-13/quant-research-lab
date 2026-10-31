import type { AgentConfigResponse, AgentDisclosure, AgentEvent, AgentLimits, AgentTask } from "./agentTypes";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/agent${path}`, {
      ...init, cache: "no-store",
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
    });
  } catch {
    throw new Error("无法连接 Agent 服务。请确认后端已启动；手动模拟仍可继续。");
  }
  const raw = await response.text();
  let body: unknown = null;
  try { body = raw ? JSON.parse(raw) : null; } catch { /* sanitized below */ }
  if (!response.ok) {
    const record = body && typeof body === "object" ? body as Record<string, unknown> : {};
    throw new AgentApiError(typeof record.detail === "string" ? record.detail : `Agent 请求失败（${response.status}）。`, response.status, typeof record.reason === "string" ? record.reason : null);
  }
  if (body === null) throw new Error("Agent 服务返回了无法解析的内容。");
  return body as T;
}

export class AgentApiError extends Error {
  constructor(message: string, readonly status: number, readonly reason: string | null) { super(message); }
}
function normalizeTask(task: AgentTask): AgentTask {
  const context = task.context && typeof task.context === "object" ? task.context : {};
  const contextPlanId = typeof context.plan_id === "string" ? context.plan_id : undefined;
  const contextPlanVersion = typeof context.plan_version === "number" ? context.plan_version : undefined;
  return {
    ...task,
    plan_id: contextPlanId ?? task.plan_id,
    expected_plan_version: contextPlanVersion ?? task.expected_plan_version,
  };
}
function normalizeTaskResult(result: { tasks: AgentTask[]; total: number }) {
  return { ...result, tasks: result.tasks.map(normalizeTask) };
}
export const getAgentConfig = () => request<AgentConfigResponse>("/config");
export const saveAgentConfig = (body: Record<string, unknown>) => request<AgentConfigResponse>("/config", { method: "PUT", body: JSON.stringify(body) });
export const testAgentConfig = (config_version: string) => request<{ reachable: boolean; tool_supported: boolean; reason: string | null; usage: Record<string, unknown> }>("/config/test", { method: "POST", body: JSON.stringify({ config_version, consent: true, test_tools: true }) });
export const getDisclosure = (practiceId: string | null, revision: number | null, planId: string | null = null, planVersion: number | null = null) => {
  const params = new URLSearchParams();
  if (practiceId) { params.set("practice_id", practiceId); if (revision !== null) params.set("expected_revision", String(revision)); }
  if (planId) { params.set("plan_id", planId); if (planVersion !== null) params.set("expected_plan_version", String(planVersion)); }
  return request<AgentDisclosure>(`/disclosure${params.size ? `?${params}` : ""}`);
};
export const createAgentTask = async (body: { practice_id: string | null; expected_revision: number | null; plan_id?: string | null; expected_plan_version?: number | null; parent_task_id?: string | null; goal: string; config_version: string; consent: true; consent_digest: string; idempotency_key: string; limits?: AgentLimits }) => normalizeTask(await request<AgentTask>("/tasks", { method: "POST", body: JSON.stringify(body) }));
export const getAgentTasks = async (practiceId: string | null, planId: string | null, scope: "setup" | "simulation") => {
  const params = new URLSearchParams({ limit: "20", offset: "0", scope });
  if (practiceId) params.set("practice_id", practiceId);
  if (planId) params.set("plan_id", planId);
  return normalizeTaskResult(await request<{ tasks: AgentTask[]; total: number }>(`/tasks?${params}`));
};
export const getAgentTask = async (id: string) => normalizeTask(await request<AgentTask>(`/tasks/${encodeURIComponent(id)}`));
export const getAgentEvents = (id: string, after: number) => request<{ events: AgentEvent[]; next_seq: number }>(`/tasks/${encodeURIComponent(id)}/events?after_seq=${after}&limit=100`);
export const cancelAgentTask = async (id: string, idempotency_key: string) => normalizeTask(await request<AgentTask>(`/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify({ idempotency_key }) }));
export const resumeAgentTask = async (operationId: string, body: { consent: true; expected_generation: number; config_version: string; consent_digest: string; idempotency_key: string }) => normalizeTask(await request<AgentTask>(`/operations/${encodeURIComponent(operationId)}/resume`, { method: "POST", body: JSON.stringify(body) }));
export const deleteAgentTask = (id: string) => request<{ ok: true }>(`/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
