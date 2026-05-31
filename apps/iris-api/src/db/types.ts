import type { ColumnType, Insertable, Selectable, Updateable } from "kysely";

type Timestamp = ColumnType<Date, Date | string | undefined, Date | string>;
type Json = ColumnType<unknown, unknown, unknown>;

export type OrganizationTable = {
  id: string;
  name: string;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type UserTable = {
  id: string;
  email: string;
  name: string | null;
  first_name: string | null;
  last_name: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type OrganizationMemberTable = {
  organization_id: string;
  user_id: string;
  role: string;
  created_at: Timestamp;
};

export type OrganizationInvitationTable = {
  id: string;
  organization_id: string;
  email: string;
  role: string;
  inviter_user_id: string;
  token_hash: string;
  status: string;
  accepted_user_id: string | null;
  accepted_at: Timestamp | null;
  expires_at: Timestamp;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type DeviceTable = {
  id: string;
  organization_id: string;
  user_id: string;
  kind: string;
  product: string | null;
  model: string | null;
  name: string;
  status: string;
  settings: Json;
  device_serial: string | null;
  firmware_version: string | null;
  hardware_info: Json | null;
  last_seen_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type DevicePairingTokenTable = {
  id: string;
  organization_id: string;
  user_id: string;
  device_id: string;
  token_hash: string;
  expires_at: Timestamp;
  claimed_at: Timestamp | null;
  created_at: Timestamp;
};

export type DeviceCredentialTable = {
  id: string;
  device_id: string;
  token_hash: string;
  revoked_at: Timestamp | null;
  created_at: Timestamp;
};

export type DeviceSecretTable = {
  device_id: string;
  llm_api_key_ciphertext: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type AgentDiscoveryOfferTable = {
  id: string;
  offer_token_hash: string;
  code: string;
  role: string;
  name: string;
  hostname: string | null;
  platform: string | null;
  arch: string | null;
  codex_version: string | null;
  bridge_url: string | null;
  status: string;
  pending_organization_id: string | null;
  pending_user_id: string | null;
  pending_user_name: string | null;
  pending_user_email: string | null;
  device_id: string | null;
  expires_at: Timestamp;
  last_seen_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type CodexThreadTable = {
  id: string;
  organization_id: string;
  user_id: string;
  agent_id: string;
  session_id: string | null;
  source_device_id: string | null;
  codex_thread_id: string | null;
  title: string | null;
  summary: string | null;
  status: string;
  current_run_id: string | null;
  last_activity_at: Timestamp;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type AgentRunTable = {
  id: string;
  organization_id: string;
  user_id: string;
  session_id: string | null;
  source_device_id: string | null;
  agent_id: string;
  thread_id: string | null;
  status: string;
  action: string;
  prompt: string | null;
  context: string | null;
  response_style: string | null;
  request: Json;
  result: Json | null;
  error: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  started_at: Timestamp | null;
  completed_at: Timestamp | null;
};

export type AgentCompletionTable = {
  id: string;
  organization_id: string;
  user_id: string;
  run_id: string;
  session_id: string | null;
  source_device_id: string | null;
  agent_id: string;
  thread_id: string | null;
  delivery: string;
  status: string;
  content: string | null;
  result: Json | null;
  error: string | null;
  delivered_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type AgentApprovalTable = {
  id: string;
  organization_id: string;
  user_id: string;
  run_id: string | null;
  session_id: string | null;
  source_device_id: string | null;
  agent_id: string;
  thread_id: string | null;
  codex_request_id: string | null;
  codex_method: string;
  status: string;
  request: Json;
  response: Json | null;
  error: string | null;
  expires_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  resolved_at: Timestamp | null;
};

export type SpeakerProfileTable = {
  id: string;
  organization_id: string;
  user_id: string;
  display_name: string;
  status: string;
  provider: string;
  sample_count: number;
  model: string | null;
  embedding_ciphertext: string | null;
  enrolled_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type UserMemoryTable = {
  id: string;
  organization_id: string;
  user_id: string;
  source_device_id: string | null;
  source_session_id: string | null;
  kind: string;
  content: string;
  normalized_content: string;
  confidence: string;
  status: string;
  metadata: Json;
  last_used_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type VoiceSessionTable = {
  id: string;
  organization_id: string;
  user_id: string;
  device_id: string;
  source: string;
  room_name: string;
  status: string;
  started_at: Timestamp;
  ended_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type TranscriptSegmentTable = {
  id: string;
  organization_id: string;
  user_id: string;
  device_id: string | null;
  session_id: string;
  source: string;
  text: string;
  words: Json | null;
  is_interim: boolean;
  speaker_label: string | null;
  speaker_user_id: string | null;
  speaker_name: string | null;
  speaker_confidence: number | null;
  emotion_label: string | null;
  emotion_confidence: number | null;
  emotion_model: string | null;
  confidence: number | null;
  started_at: Timestamp;
  ended_at: Timestamp | null;
  created_at: Timestamp;
};

export type SummaryTable = {
  id: string;
  organization_id: string;
  user_id: string;
  type: string;
  title: string;
  summary: string;
  important_points: Json;
  action_items: Json;
  source_segment_ids: Json;
  period_start: Timestamp;
  period_end: Timestamp;
  status: string;
  generated_at: Timestamp | null;
  created_at: Timestamp;
  updated_at: Timestamp;
};

export type EventTokenTable = {
  id: string;
  token_hash: string;
  user_id: string;
  expires_at: Timestamp;
  created_at: Timestamp;
};

export type AuditEventTable = {
  id: string;
  organization_id: string;
  user_id: string | null;
  device_id: string | null;
  type: string;
  data: Json;
  created_at: Timestamp;
};

export type Database = {
  organizations: OrganizationTable;
  users: UserTable;
  organization_members: OrganizationMemberTable;
  organization_invitations: OrganizationInvitationTable;
  devices: DeviceTable;
  device_pairing_tokens: DevicePairingTokenTable;
  device_credentials: DeviceCredentialTable;
  device_secrets: DeviceSecretTable;
  agent_discovery_offers: AgentDiscoveryOfferTable;
  codex_threads: CodexThreadTable;
  agent_runs: AgentRunTable;
  agent_completions: AgentCompletionTable;
  agent_approvals: AgentApprovalTable;
  speaker_profiles: SpeakerProfileTable;
  user_memories: UserMemoryTable;
  voice_sessions: VoiceSessionTable;
  transcript_segments: TranscriptSegmentTable;
  summaries: SummaryTable;
  event_tokens: EventTokenTable;
  audit_events: AuditEventTable;
};

export type DeviceRow = Selectable<DeviceTable>;
export type DeviceInsert = Insertable<DeviceTable>;
export type DeviceUpdate = Updateable<DeviceTable>;
export type CodexThreadRow = Selectable<CodexThreadTable>;
export type AgentRunRow = Selectable<AgentRunTable>;
export type AgentCompletionRow = Selectable<AgentCompletionTable>;
export type AgentApprovalRow = Selectable<AgentApprovalTable>;
export type SpeakerProfileRow = Selectable<SpeakerProfileTable>;
export type UserMemoryRow = Selectable<UserMemoryTable>;
export type TranscriptSegmentRow = Selectable<TranscriptSegmentTable>;
export type VoiceSessionRow = Selectable<VoiceSessionTable>;
export type SummaryRow = Selectable<SummaryTable>;
