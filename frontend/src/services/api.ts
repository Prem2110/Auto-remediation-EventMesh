const _BASE = import.meta.env.VITE_API_BASE ?? "/api";

export const API_PRIMARY =
  import.meta.env.VITE_API_PRIMARY ?? "https://ND-ORBIT.cfapps.us10-001.hana.ondemand.com";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function postForm<T>(url: string, formData: FormData): Promise<T> {
  const response = await fetch(url, { method: "POST", body: formData });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function requestMaybe<T>(url: string, options?: RequestInit): Promise<T | null> {
  try {
    return await request<T>(url, options);
  } catch {
    return null;
  }
}

function normalizeIncidentForUi(incident: Record<string, unknown>): Record<string, unknown> {
  return {
    ...incident,
    iflow_name:
      (incident.iflow_name as string) ||
      (incident.iflow_id as string) ||
      (incident.artifact_id as string) ||
      (incident.integration_flow_name as string) ||
      "",
  };
}

export async function fetchCurrentUser(): Promise<unknown> {
  return request(`/user-api/currentUser`);
}

export async function fetchAllHistory(email: string): Promise<{ history: unknown[] }> {
  return request(`${_BASE}/get_all_history?user_id=${encodeURIComponent(email)}`);
}

export async function sendChatMessage(
  formData: FormData
): Promise<{ response: string; id: string }> {
  return postForm(`${_BASE}/query`, formData);
}

export async function fetchMonitorMessages(): Promise<{ messages: unknown[] }> {
  return request(`${_BASE}/smart-monitoring/messages`);
}

export async function fetchMonitorMessageDetail(guid: string): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/messages/${guid}`);
}

export async function analyzeMessage(guid: string, userId = "user"): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/messages/${guid}/analyze`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function explainError(guid: string, userId = "user"): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/messages/${guid}/explain_error`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function generateFixPatch(guid: string, userId = "user"): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/messages/${guid}/generate_fix_patch`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function applyMessageFix(guid: string, userId = "user", proposedFix?: string): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/messages/${guid}/apply_fix`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId, proposed_fix: proposedFix }),
  });
}

export async function fetchFixStatus(incidentId: string): Promise<unknown> {
  return request(`${_BASE}/smart-monitoring/incidents/${incidentId}/fix_status`);
}

export async function smartMonitoringChat(
  query: string,
  userId = "user",
  messageGuid?: string,
  sessionId?: string,
): Promise<{ answer: string; session_id: string }> {
  return request(`${_BASE}/smart-monitoring/chat`, {
    method: "POST",
    body: JSON.stringify({ query, user_id: userId, message_guid: messageGuid, session_id: sessionId }),
  });
}

export async function fetchTestSuiteLogs(): Promise<{ ts_logs: unknown[] }> {
  return request(`${_BASE}/get_testsuite_logs`);
}

export async function uploadMigrationFiles(
  formData: FormData
): Promise<{ oldCode: string; newCode: string }> {
  if (!formData.has("query")) formData.append("query", "Analyze uploaded migration files and provide migrated code.");
  if (!formData.has("user_id")) formData.append("user_id", "user");
  const res = await postForm<{ response: string }>(`${_BASE}/query`, formData);
  return { oldCode: "", newCode: res.response ?? "" };
}

export async function fetchPipoDetails(): Promise<unknown[]> {
  const data = await request<{ incidents?: unknown[] }>(`${_BASE}/autonomous/incidents?limit=100`);
  const incidents = (data.incidents ?? []) as Record<string, unknown>[];
  return incidents.map((item) => ({
    name: item.iflow_id ?? item.iflow_name ?? item.message_guid ?? "-",
    issue: item.error_message ?? item.status ?? "-",
  }));
}

export async function uploadFile(formData: FormData): Promise<unknown> {
  if (!formData.has("query")) formData.append("query", "Analyze uploaded files.");
  if (!formData.has("user_id")) formData.append("user_id", "user");
  return postForm(`${_BASE}/query`, formData);
}

// Single endpoint — replaces 7 separate dashboard requests with one roundtrip
export async function fetchDashboardAll(): Promise<Record<string, unknown>> {
  return request(`${_BASE}/dashboard/all`);
}

export interface AgentStatus {
  pipeline_running: boolean;
  started_at: string | null;
  agents: Record<string, string>;
  message?: string;
  autonomous_running?: boolean;
  tool_distribution?: Record<string, string[]>;
}

interface AutonomousStatusResponse {
  running: boolean;
  poll_interval_seconds?: number;
}

interface ToolsResponse {
  servers?: Record<string, Array<{ agent_tool_name?: string; mcp_tool_name?: string }>>;
}

export interface McpToolsStatus {
  total: number;
  servers: Record<string, string[]>;
}

export async function fetchMcpTools(): Promise<McpToolsStatus> {
  const tools = await requestMaybe<ToolsResponse>(`${_BASE}/autonomous/tools`);
  const servers: Record<string, string[]> = {};
  let total = 0;
  for (const [server, toolList] of Object.entries(tools?.servers ?? {})) {
    servers[server] = toolList.map(t => t.agent_tool_name ?? t.mcp_tool_name ?? "").filter(Boolean);
    total += servers[server].length;
  }
  return { total, servers };
}

// Fetch tool distribution once — tools don't change after startup.
// Callers should use staleTime: 5+ minutes.
export async function fetchToolDistribution(): Promise<Record<string, string[]>> {
  const tools = await requestMaybe<ToolsResponse>(`${_BASE}/autonomous/tools`);
  const serverEntries = Object.entries(tools?.servers ?? {});
  const findTools = (...keywords: string[]): string[] => {
    const hit = serverEntries.find(([server]) =>
      keywords.some((kw) => server.toLowerCase().includes(kw)),
    );
    if (!hit) return [];
    return hit[1].map((t) => t.agent_tool_name ?? t.mcp_tool_name ?? "").filter(Boolean);
  };
  return {
    observer:   findTools("observer"),
    classifier: findTools("classifier"),
    rca:        findTools("rca"),
    fixer:      findTools("fix"),
    verifier:   findTools("verifier"),
  };
}

export async function fetchPipelineStatus(): Promise<AgentStatus> {
  const autonomous = await request<AutonomousStatusResponse>(`${_BASE}/autonomous/status`);
  const running = autonomous.running;
  const agents = {
    observer: running ? "running" : "idle",
    classifier: running ? "running" : "idle",
    rca: running ? "running" : "idle",
    fixer: running ? "running" : "idle",
    verifier: running ? "running" : "idle",
  };

  return {
    pipeline_running: running,
    started_at: null,
    agents,
    message: running ? "Autonomous pipeline running" : "Autonomous pipeline stopped",
    autonomous_running: running,
  };
}

export async function startPipeline(): Promise<{ message: string; status: AgentStatus }> {
  await request(`${_BASE}/autonomous/start`, { method: "POST" });
  const status = await fetchPipelineStatus();
  return { message: "Pipeline started", status };
}

export async function stopPipeline(): Promise<{ message: string }> {
  await request(`${_BASE}/autonomous/stop`, { method: "POST" });
  return { message: "Pipeline stopped" };
}

export async function searchKnowledge(
  errorMessage: string,
  errorType?: string,
  topK = 3
): Promise<{ query: string; results: unknown[]; count: number }> {
  const data = await request<{ incidents?: unknown[] }>(`${_BASE}/autonomous/incidents?limit=200`);
  const incidents = (data.incidents ?? []) as Record<string, unknown>[];
  const q = errorMessage.toLowerCase();

  const scored = incidents
    .filter((inc) => !errorType || String(inc.error_type ?? "").toLowerCase() === errorType.toLowerCase())
    .map((inc) => {
      const proposedFix = String(inc.proposed_fix ?? "");
      const rootCause = String(inc.root_cause ?? "");
      const errorMsg = String(inc.error_message ?? "");
      const corpus = `${proposedFix} ${rootCause} ${errorMsg}`.toLowerCase();
      const score = q && corpus.includes(q) ? 1 : 0.6;
      return {
        fix_description: proposedFix || rootCause || errorMsg || "No fix description available.",
        similarity_score: score,
        error_type: String(inc.error_type ?? "UNKNOWN"),
        iflow_name: String(inc.iflow_name ?? inc.iflow_id ?? ""),
      };
    })
    .sort((a, b) => b.similarity_score - a.similarity_score)
    .slice(0, topK);

  return { query: errorMessage, results: scored, count: scored.length };
}

export async function fetchTickets(): Promise<{ tickets: unknown[] }> {
  return request(`${_BASE}/autonomous/tickets`);
}

export async function fetchPendingApprovals(): Promise<{ pending: unknown[] }> {
  return request(`${_BASE}/autonomous/pending_approvals`);
}

export async function approveIncident(
  incidentId: string,
  approved: boolean,
  comment = ""
): Promise<unknown> {
  return request(`${_BASE}/autonomous/incidents/${incidentId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approved, comment }),
  });
}

export async function fetchPipelineTrace(
  limit = 20
): Promise<{ incidents: unknown[]; total: number }> {
  const data = await request<{ incidents: unknown[]; total: number }>(
    `${_BASE}/autonomous/incidents?limit=${limit}`,
  );
  const incidents = (data.incidents ?? []).map((inc) => normalizeIncidentForUi(inc as Record<string, unknown>));
  return { incidents, total: data.total ?? incidents.length };
}

export async function fetchIncidents(
  status?: string,
  limit = 50
): Promise<{ incidents: unknown[]; total: number }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  return request(`${_BASE}/autonomous/incidents?${params}`);
}

// ─────────────────────────────────────────────────────────────────────────────
// Pagination APIs for dashboard tables
// ─────────────────────────────────────────────────────────────────────────────

export interface PaginatedMessagesResponse {
  count: number;
  total_count: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  messages: unknown[];
}

// ─────────────────────────────────────────────────────────────────────────────
// AEM / Event Mesh status
// ─────────────────────────────────────────────────────────────────────────────

export interface AemStatusResponse {
  total_incidents: number;
  messages_retrieved: number;
  queue_depth: number;
  stage_counts?: Record<string, number>;
  event_mesh_enabled?: boolean;
  webhook_active?: boolean;
}

export async function fetchAemStatus(): Promise<AemStatusResponse | null> {
  return requestMaybe<AemStatusResponse>(`${_BASE}/aem/status`);
}

export async function fetchAemIncidents(
  limit = 100
): Promise<{ incidents: Record<string, unknown>[] }> {
  const data = await request<{ incidents?: unknown[] }>(
    `${_BASE}/autonomous/incidents?limit=${limit}`
  );
  return {
    incidents: ((data.incidents ?? []) as Record<string, unknown>[]).map(
      normalizeIncidentForUi
    ),
  };
}

export async function fetchFailedMessagesPaginated(
  page = 1,
  pageSize = 20,
  status?: string,
  type?: string,
  id?: string,
  artifacts?: string
): Promise<PaginatedMessagesResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (status) params.set("status", status);
  if (type) params.set("type", type);
  if (id) params.set("id", id);
  if (artifacts) params.set("artifacts", artifacts);

  return request(`${_BASE}/smart-monitoring/messages/paginated?${params}`);
}

export interface PaginatedIncidentsResponse {
  count: number;
  total_count: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  incidents: unknown[];
}

export async function fetchActiveIncidentsPaginated(
  page = 1,
  pageSize = 20,
  status?: string
): Promise<PaginatedIncidentsResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (status) params.set("status", status);

  return request(`${_BASE}/dashboard/incidents/paginated?${params}`);
}
