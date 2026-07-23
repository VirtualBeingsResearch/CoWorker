export interface ExperimentSummary {
  id: string;
  source_base_url: string;
  imported_at: number;
  branch_count: number;
}

export interface ExperimentDetail {
  experiment: { id: string; source_base_url: string; imported_at: number };
  branches: Branch[];
}

export interface Verdict {
  result: 'pass' | 'fail' | 'unclear';
  score?: number;
  comment?: string;
}

export interface Branch {
  id: string;
  experiment_id: string;
  parent_id: string | null;
  workdir: string;
  control_port: number;
  pid: number | null;
  status: string;
  label: string;
  note: string;
  is_baseline: boolean;
  verdict: Verdict | null;
  overrides: Record<string, unknown>;
  created_at: number;
}

export interface ScenarioEvent {
  content: string;
  participant_id?: string;
  delay_after_seconds?: number;
}

export interface Scenario {
  id: string;
  experiment_id: string;
  name: string;
  events: ScenarioEvent[];
}

export interface TranscriptToolCall {
  id?: string;
  type?: string;
  name?: string;
  arguments?: unknown;
  function?: {
    name?: string;
    arguments?: unknown;
  };
}

export interface TranscriptMessage {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string | unknown[];
  timestamp: string;
  tool_calls?: TranscriptToolCall[];
  tool_call_id?: string;
}

export interface BubbleSummary {
  id: string;
  goal: string;
  status: string;
  cycles_used: number;
  max_cycles: number;
  participant_id: string;
  created_at: string;
  kind: 'goal' | 'subconscious';
}

export interface BranchState {
  status: string;
  auto_paused_reason?: string | null;
  cycle_count?: number;
  current_provider?: string;
  current_model?: string;
  tick?: boolean;
  is_sleeping?: boolean;
  tool_call_counts?: Record<string, number>;
  transcript?: TranscriptMessage[];
  undo_depth?: number;
  system_prompt_override_active?: boolean;
  system_prompt_override_text?: string | null;
  tool_intercepts?: Record<string, string>;
  virtual_connections?: string[];
  outbound_messages?: OutboundMessage[];
  subconscious_enabled?: boolean;
  usage_stats?: unknown;
  active_bubbles?: BubbleSummary[];
  subconscious_pending?: string[];
  last_error?: { type: string; message: string } | null;
}

export interface OutboundMessage {
  participant_id: string;
  message: string;
  conversation_id?: string;
  attachments?: unknown[];
  extra?: Record<string, unknown>;
  timestamp: string;
}

export interface SystemPromptSnapshot {
  base_text: string;
  effective_text: string;
  override_active: boolean;
  override_text: string | null;
}

export interface StepResult {
  ok: boolean;
  error: { type: string; message: string } | null;
  new_messages: TranscriptMessage[];
}

export interface StepNResult extends StepResult {
  completed: number;
  stopped_early: string | null;
}

export interface CompareResult {
  branches: Record<
    string,
    {
      label: string;
      is_baseline: boolean;
      verdict: Verdict | null;
      status: string;
      cycle_count: number;
      transcript: TranscriptMessage[];
    }
  >;
}

export interface DiffResult {
  a: { branch_id: string; system_prompt_override_text: string | null; thinking_md: string };
  b: { branch_id: string; system_prompt_override_text: string | null; thinking_md: string };
  system_prompt_override_differs: boolean;
  thinking_md_differs: boolean;
  config_diff: Record<string, { a: unknown; b: unknown }>;
}
