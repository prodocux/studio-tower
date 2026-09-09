export type MembershipRole = 'owner' | 'admin' | 'coordinator' | 'member';

export type SpaceKind = 'agent_dm' | 'shared_space';

export interface User {
  uid: string;
  email: string;
  display_name: string;
  created_at?: string;
}

export type WorkspaceView = 'chat' | 'files' | 'runs' | 'activity';

export interface ProjectTag {
  id?: string;
  name: string;
  slug: string;
  color: string;
  description?: string;
  archived?: boolean;
  revision?: number;
  updated_at?: string;
}

export interface ActivityEvent {
  event_id: string;
  event_type: string;
  space_id: string;
  project_tag?: string;
  project_tags?: string[];
  resource_type: string;
  resource_id: string;
  summary: string;
  details?: Record<string, any>;
  actor_uid?: string;
  created_at: string;
}

export interface ActivityListResponse {
  items: ActivityEvent[];
  next_cursor?: string | null;
}

export interface Space {
  space_id: string;
  name: string;
  kind: SpaceKind;
  created_by: string;
  created_at: string;
  tags: ProjectTag[];
}

export interface Capabilities {
  can_invite: boolean;
  can_manage_members: boolean;
  can_change_role: boolean;
  can_remove_member: boolean;
  can_transfer_ownership: boolean;
  can_approve_runs: boolean;
  can_manage_tags: boolean;
  can_leave_space: boolean;
}

export interface SpaceContext {
  space: Space;
  current_user_role: MembershipRole;
  member_count: number;
  capabilities: Capabilities;
}

export interface MemberInfo {
  uid: string;
  display_name: string;
  email: string;
  role: MembershipRole;
  joined_at: string;
}

export interface Invite {
  token: string;
  space_id: string;
  created_by: string;
  role: MembershipRole;
  target_email?: string | null;
  max_uses: number;
  used_count: number;
  expires_at?: string | null;
  created_at: string;
  revoked_at?: string | null;
}

export interface InvitePreviewResponse {
  space_name: string;
  role: MembershipRole;
  target_email_masked?: string | null;
  status: 'active';
}

export interface PendingInviteSession {
  token: string;
  issued_at: number;
  target_space_name?: string;
  target_role?: string;
}

export type MessageRole = 'user' | 'agent' | 'system';

export interface Citation {
  citation_id: string;
  index: number;
  file_id: string;
  filename: string;
  generation: number;
  content_hash: string;
  chunk_id: string;
  source_locator: string;
  page_number?: number | null;
  char_start: number;
  char_end: number;
  snippet: string;
  score?: number;
}

export interface ActionProposal {
  action_id: string;
  action_type: string;
  title: string;
  description: string;
  output_format?: 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'csv' | 'json';
  space_id: string;
  project_tag: string;
  user_id: string;
  source_file_ids: string[];
  sources?: Array<{
    file_id: string;
    active_generation: number;
    content_hash: string;
    space_id: string;
  }>;
  generation_version: number;
  expires_at: string;
  key_id?: string;
  estimated_duration_seconds?: number;
  metadata?: Record<string, any>;
}

export interface ActionConfirmationPayload {
  action_id: string;
}

export interface ActionConfirmationResponse {
  status: string;
  action_id: string;
  run_id: string;
  message: Message;
}

export interface MessagePageResponse {
  items: Message[];
  next_cursor?: string | null;
  has_more: boolean;
}

export interface Message {
  message_id: string;
  space_id: string;
  sender_uid: string;
  sender_name: string;
  role: MessageRole;
  content: string;
  project_tag?: string;
  attachment_file_ids?: string[];
  citations?: Citation[];
  client_message_id?: string;
  proposed_action?: ActionProposal | null;
  run_id?: string | null;
  created_at: string;
  isSending?: boolean;
  isFailed?: boolean;
}

export type IngestionStatus =
  | 'pending'
  | 'extracting'
  | 'indexing'
  | 'ready'
  | 'ready_partial'
  | 'needs_ocr'
  | 'failed';

export interface DocumentChunkSummary {
  chunk_id: string;
  file_id: string;
  space_id: string;
  ingestion_version: number;
  ordinal: number;
  page_number?: number | null;
  section_heading?: string | null;
  source_locator: string;
  normalized_text: string;
  char_start: number;
  char_end: number;
  token_count: number;
  content_hash: string;
  contains_formula_like_content: boolean;
  extraction_method: string;
  extractor_version: string;
}

export interface DocumentChunksResponse {
  items: DocumentChunkSummary[];
  cursor: number;
  limit: number;
  total: number;
  has_more: boolean;
  next_cursor?: number | null;
  active_generation: number;
}

export interface FileIngestionStatusResponse {
  file_id: string;
  space_id: string;
  ingestion_status: IngestionStatus;
  active_generation: number;
  ingestion_job_id?: string | null;
  ingestion_version: number;
  chunk_count: number;
  extracted_pages: number;
  ocr_gap_pages: number[];
  has_ocr_gaps: boolean;
  error_code?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface ActivityListResponse {
  items: ActivityEvent[];
  next_cursor?: string | null;
  has_more: boolean;
}

export interface FileRecord {
  file_id: string;
  space_id: string;
  filename: string;
  storage_path: string;
  uploaded_by: string;
  source_type: 'user_upload' | 'pdx_artifact' | 'manifest';
  size_bytes: number;
  sha256: string;
  project_tags: string[];
  created_at: string;
  content_type?: string;
  ingestion_status?: IngestionStatus;
  active_generation?: number;
  ingestion_job_id?: string | null;
  chunk_count?: number;
  extracted_pages?: number;
  ocr_gap_pages?: number[];
  has_ocr_gaps?: boolean;
  ingestion_error_code?: string | null;
  ingestion_error_message?: string | null;
}

export interface ApprovalGate {
  gate_id: string;
  title: string;
  description: string;
  required_role: string;
  status: 'pending' | 'approved' | 'rejected';
  approved_by?: string | null;
  approved_at?: string | null;
  rejection_reason?: string | null;
}

export interface Run {
  run_id: string;
  action_id?: string | null;
  space_id: string;
  project_tag: string;
  status: 'pending' | 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'aborted' | 'queued';
  prompt: string;
  created_by: string;
  failure_code?: string | null;
  is_retryable?: boolean;
  error_summary?: string | null;
  trace_id?: string | null;
  approval_gate?: ApprovalGate | null;
  scene_breakdown?: any;
  telemetry_status?: string;
  telemetry_generation?: number;
  latest_diagnosis_id?: string | null;
  latest_diagnosis_status?: string | null;
  diagnosis_revision?: number;
  telemetry?: {
    duration_ms: number;
    llm_latency_ms: number;
    pdx_exec_ms: number;
    tokens_used?: number | null;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cached_tokens?: number | null;
    estimated_cost_usd?: number | null;
    pricing_version?: string | null;
    model_id?: string | null;
    tool_calls: string[];
    grafana_dashboard_url?: string | null;
    has_real_telemetry?: boolean;
    telemetry_status?: string;
    telemetry_generation?: number;
    ai_engine?: string;
  };
  created_at: string;
}

export interface LineageNode {
  id: string;
  type: 'source' | 'run' | 'gate' | 'artifact' | 'manifest';
  label: string;
  status: string;
  sha256?: string;
  size_bytes?: number;
  uploaded_by?: string;
  trace_id?: string;
  required_role?: string;
  created_at?: string;
}

export interface LineageEdge {
  from: string;
  to: string;
  relation: string;
}

export interface LineageGraph {
  space_id: string;
  tag: string;
  nodes: LineageNode[];
  edges: LineageEdge[];
  grafana?: {
    connected: boolean;
    source?: string;
    trace_count?: number;
    error?: string | null;
    ingest?: string | null;
    ingest_detail?: string | null;
  };
}

export type ToastType = 'success' | 'error' | 'info' | 'warning';

export interface ToastItem {
  id: string;
  type: ToastType;
  message: string;
  durationMs?: number;
}

// Slice E: Telemetry, Waterfall Trace, AI Diagnosis, and Metrics Contracts
export interface SpanWaterfallNode {
  span_id: string;
  parent_span_id?: string | null;
  name: string;
  service_name: string;
  start_time_iso: string;
  end_time_iso: string;
  duration_ms: number;
  offset_ms: number;
  status: 'ok' | 'error' | 'warning' | string;
  attributes: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
}

export interface RunTraceResponse {
  trace_id: string;
  run_id: string;
  space_id: string;
  service_name: string;
  total_spans: number;
  spans: SpanWaterfallNode[];
  grafana_dashboard_url?: string | null;
  has_real_telemetry: boolean;
}

export interface EvidenceSpanSummary {
  span_id: string;
  name: string;
  duration_ms: number;
  status: string;
  error_code?: string | null;
  key_attributes: Record<string, unknown>;
}

export interface DiagnosisRecord {
  diagnosis_id: string;
  space_id: string;
  run_id: string;
  trace_id: string;
  telemetry_generation: number;
  schema_version: number;
  engine: 'gemini_2_flash' | 'rule_based' | string;
  model_id?: string | null;
  diagnostic_status: 'complete' | 'fallback' | 'no_failure_evidence' | 'unavailable' | string;
  faulting_span?: EvidenceSpanSummary | null;
  error_code: string;
  error_summary: string;
  observations: string[];
  likely_causes: string[];
  recommendations: string[];
  evidence_span_ids: string[];
  is_retryable: boolean;
  suggested_action: string;
  confidence: 'high' | 'medium' | 'low' | string;
  grafana_dashboard_url?: string | null;
  has_real_telemetry: boolean;
  is_local_diagnostic: boolean;
  created_at: string;
  expires_at?: string | null;
}

export interface TelemetryVerificationResult {
  run_id: string;
  space_id: string;
  trace_id: string;
  previous_status: string;
  current_status: string;
  telemetry_generation: number;
  matched: boolean;
  throttled: boolean;
  stale_rejected: boolean;
  attempts: number;
}

export interface TelemetryMetricsSummary {
  space_id: string;
  project_tag?: string | null;
  time_window_hours: number;
  rollup_schema_version: number;
  data_status: 'available' | 'warming_up' | 'unavailable' | string;
  cost_data_status: 'available' | 'partial' | 'unavailable' | string;
  sample_count: number;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  success_rate: number;
  latency_percentile_method: string;
  latency_histogram_buckets: number[];
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_percentile_capped: boolean;
  total_tokens_used: number;
  estimated_cost_usd?: number | null;
  pricing_version?: string | null;
  pricing_versions_truncated: boolean;
  currency: string;
  failures_by_code: Record<string, number>;
  generated_at: string;
}

export interface GrafanaSeries {
  name: string;
  points: number[][];
}

export interface GrafanaPanelVisual {
  panel_id: number;
  title: string;
  type: string;
  image_base64?: string | null;
  series: GrafanaSeries[];
  table_rows?: string[][];
  query_error?: string | null;
}

export interface GrafanaDashboardVisual {
  uid: string;
  title: string;
  url: string;
  panels: GrafanaPanelVisual[];
}

export interface GrafanaLineageStep {
  node_id: string;
  title: string;
  stage: string;
  detail?: string;
  event_type?: string;
  resource_id?: string;
  resource_name?: string;
  run_id?: string;
  file_id?: string;
  artifact_id?: string;
  trace_id?: string;
}

export interface GrafanaVisualBoard {
  connected: boolean;
  source?: string | null;
  base_url?: string | null;
  org_name?: string | null;
  datasource_names?: string[];
  dashboard_count?: number;
  ingest?: string | null;
  ingest_detail?: string | null;
  error?: string | null;
  dashboards: GrafanaDashboardVisual[];
  lineage_flow?: GrafanaLineageStep[];
}

export interface WaterfallTarget {
  spaceId: string;
  runId: string;
  triggerId?: string;
  requestGeneration: number;
}
