// ── User ────────────────────────────────────────────────────────────────────
export interface IUser {
  firstname: string;
  lastname: string;
  email: string;
  name: string;
  displayName: string;
}

// ── History ──────────────────────────────────────────────────────────────────
export interface IHistoryEntry {
  id: string;
  header: string;
  history: IConversationMessage[];
  files?: IAttachedFile[];
}

export interface IConversationMessage {
  question: string;
  result: string;
  created_at: string;
}

export interface IAttachedFile {
  file_name: string;
  file_size: number;
  uploaded_at: string;
}

// ── Chat ─────────────────────────────────────────────────────────────────────
export interface IChatBubble {
  id: string;
  role: "user" | "bot" | "error";
  text: string;
  timestamp?: string;
}

// ── Agent Cards ───────────────────────────────────────────────────────────────
export interface IAgent {
  id: string;
  name: string;
  description: string;
  version: string;
  capabilities: string[];
  skills: string[];
  expanded: boolean;
  visibleCapabilities: string[];
  visibleSkills: string[];
}

// ── Observability ─────────────────────────────────────────────────────────────
export interface IMonitorMessage {
  title?: string;
  iflow_display?: string;
  status: string;
  statusText?: string;
  statusState?: string;
  errorMessage?: string;
  log_start?: string;
  updatedAt?: string;
  duration?: string;
  relative_time?: string;
  errorTitle?: string;
  diagnosis?: string;
  recommendation?: string;
  confidence?: string;
  fixSteps?: string[];
  message_guid?: string;
  message_type?: string;
  artifact_id?: string;
  properties?: IMessageProperties;
  // Detail fields flattened on selection
  error_type?: string;
  log_end?: string;
  error_message?: string;
  proposed_fix?: string;
  confidence_display?: string;
  affected_component?: string;
  can_generate_fix?: boolean;
  has_rca?: boolean;
  message_id?: string;
  correlation_id?: string;
  sender?: string;
  receiver?: string;
  iflow_name?: string;
}

export interface IMessageProperties {
  message?: {
    message_id?: string;
    correlation_id?: string;
    sender?: string;
    receiver?: string;
    interface_iflow?: string;
  };
  adapter?: Record<string, string>;
  businessContext?: Record<string, string>;
  artifact?: Record<string, string>;
}

export interface IFilterState {
  statuses: string[];
  types: string[];
  artifacts: string[];
  dateFrom: string;
  dateTo: string;
  idQuery: string;
  searchQuery: string;
}

// ── Smart Monitoring Detail (tab-based) ──────────────────────────────────────
export interface IFieldChange {
  old_field: string;
  new_field: string;
  source?: string;
}

export interface IMessageDetail {
  message_guid: string;
  iflow_id: string;
  iflow_display: string;
  status: string;
  last_updated: string;
  relative_time: string;
  incident_id?: string;
  incident_status?: string;

  error_details: {
    error_message: string;
    error_type: string;
    status: string;
    log_start: string;
    log_end: string;
    last_updated: string;
    raw_error_text: string;
  };
  ai_recommendation: {
    diagnosis: string;
    proposed_fix: string;
    field_changes: IFieldChange[];
    confidence: number;
    confidence_label: string;
    confidence_display: string;
    error_type: string;
    affected_component: string;
    can_generate_fix: boolean;
    fix_status: string;
    fix_summary: string;
  };
  properties: {
    message: Record<string, string | null>;
    adapter: Record<string, string | null>;
    business_context: Record<string, string | null>;
  };
  artifact: {
    name: string;
    artifact_id: string;
    version: string | null;
    package: string | null;
    deployed_on: string;
    deployed_by: string | null;
    runtime_node: string | null;
    status: string;
  };
  attachments: unknown[];
  history: IHistoryTimelineEntry[];
}

export interface IErrorExplanation {
  error_category: string;
  category_label: string;
  summary: string;
  what_happened: string;
  likely_causes: string[];
  recommended_actions: string[];
}

export interface IHistoryTimelineEntry {
  step: string;
  timestamp: string;
  timestamp_raw: string;
  description: string;
  status: string; // completed | failed | pending | in_progress | info
}

export interface IFixPlanStep {
  step_number: number;
  title: string;
  description: string;
  sub_steps: string[];
  note: string | null;
}

export interface IFixPatchResponse {
  incident_id: string;
  message_guid: string;
  iflow_id: string;
  error_type: string;
  summary: string;
  summary_structured: {
    diagnosis: string;
    field_changes: IFieldChange[];
    proposed_fix: string;
  };
  steps: IFixPlanStep[];
  confidence: number;
  confidence_label: string;
  confidence_display: string;
  affected_component: string;
  ready_to_apply: boolean;
  can_apply: boolean;
}

// ── Test Suite ────────────────────────────────────────────────────────────────
export interface ITestExecution {
  http_method?: string;
  message_id?: string;
  message_logs?: string;
  payload?: unknown;
  headers?: Record<string, string>;
}

export interface ITestLog {
  id: string;
  initiatedUser: string;
  initiatedTime: string;
  prompt: string;
  execution: string;
  payload?: unknown;
  operation?: string;
  executions: ITestExecution[];
}

// ── Migration Wizard ──────────────────────────────────────────────────────────
export type FileSource = "CODEFILE" | "ERROR";

export interface IUploadedFile {
  fileId: string;
  fileName: string;
  fileType: string;
  fileSizeKB: string;
  lastModified: string;
  source: FileSource;
}

// ── PIPO ──────────────────────────────────────────────────────────────────────
export interface IPipoDetail {
  name: string;
  issue: string;
}
