export type ProfileInfo = {
  name?: string | null;
  is_initialized?: boolean;
  earliest_log_ts?: string | null;
  readme?: string | null;
  current_location?: string | null;
};

export type BasicStatus = {
  status?: 'not_started' | string;
  is_running?: boolean;
  is_sleeping?: boolean;
  provider?: string;
  model?: string;
  cycle_count?: number;
};

export type IdentityInfo = {
  name?: string;
  role?: string;
  team?: string;
  birth?: string;
  age_days?: number;
  life_story?: string;
};

export type VitalsInfo = {
  status?: 'running' | 'sleeping' | 'idle' | 'not_started' | string;
  activity_state?: 'thinking' | 'communicating' | 'exploring' | 'sleeping' | 'idle' | string;
  activity_label?: string;
  is_running?: boolean;
  is_sleeping?: boolean;
  provider?: string;
  model?: string;
  cycle_count?: number;
  message_count?: number;
  skill_count?: number;
  memory_count?: number;
  inbox_pending?: number;
  milestones?: Array<{
    title?: string;
    detail?: string;
  }>;
  short_term_recent?: Array<{
    role?: string;
    text?: string;
    timestamp?: string | null;
  }>;
};

export type TaskStats = {
  total?: number;
  active?: number;
  pending?: number;
  completed?: number;
};

export type TaskItem = {
  id?: string;
  status?: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
  priority?: string;
  source?: string;
  progress?: string;
};

export type BubbleItem = {
  id?: string;
  goal?: string;
  status?: string;
  cycles?: number;
};

export type FullStatus = {
  status?: 'not_started' | string;
  identity?: IdentityInfo;
  vitals?: VitalsInfo;
  usage_stats?: UsageStats;
  short_term_memory?: ShortTermMemoryInfo;
  task_stats?: TaskStats;
  tasks?: TaskItem[];
  bubbles?: BubbleItem[];
  long_term_memory?: LongTermMemoryInfo;
  tool_usage?: Record<string, number>;
  loaded_skills?: Record<string, number>;
  available_skills?: string[];
  palaces?: PalacesInfo;
  daily_stats?: DailyStats;
  next_alarm?: NextAlarm;
  last_active?: string | null;
  updated_at?: string;
  version?: string;
};

export type UsageModelStats = {
  llm_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  cached_tokens?: number;
  total_tokens?: number;
  cache_rate?: number | null;
};

export type UsageProviderModelStats = UsageModelStats & {
  provider?: string;
  model?: string;
};

export type UsageWindowStats = UsageModelStats & {
  tool_calls?: number;
  thinking_calls?: number;
  thinking_seconds?: number;
  avg_thinking_seconds?: number | null;
  by_model?: Record<string, UsageModelStats>;
  by_provider_model?: Record<string, UsageProviderModelStats>;
  tools?: Record<string, number>;
  by_scope?: Record<string, UsageWindowStats>;
};

export type UsageStats = {
  today?: UsageWindowStats;
  last_7_days?: UsageWindowStats;
  lifetime?: UsageWindowStats;
};

export type ShortTermMemoryInfo = {
  message_count?: number;
  token_estimate?: number;
  recent_messages?: Array<{
    role?: string;
    content?: string;
  }>;
  recent_summaries?: Array<{
    label?: string;
    text?: string;
  }>;
};

export type LongTermMemoryInfo = {
  total?: number;
  total_count?: number;
  categories?: Record<string, number>;
  by_category?: Record<string, number>;
  experience?: number;
  knowledge?: number;
  relationship?: number;
  task?: number;
  general?: number;
  preference?: number;
};

export type PalacesInfo = {
  available?: string[];
  loaded?: string[];
};

export type DailyStats = {
  commits_today?: number;
  bugs_created_today?: number | null;
  interactions_today?: number | null;
};

export type NextAlarm = {
  alarm_id?: string;
  trigger_at?: string;
  message?: string;
  repeat_seconds?: number;
} | null;

// 运行日志事件 —— 契约跟随后端 interactions.jsonl 的结构（经 RuntimeEventCollector 初级处理：
// 脱敏、截断、丢弃噪声类型）。字段名直接对齐后端日志条目，不再做前端侧的重命名。
export type RuntimeLogEvent = {
  // 单调递增主键（后端 InteractionLogger 序号）；作为非工具行的稳定 React key。
  seq?: number;
  ts?: string;
  type?:
    | 'message_in'
    | 'thinking_start'
    | 'llm_response'
    | 'tool_call'
    | 'tool_result'
    | string;
  // message_in
  participant_id?: string;
  source?: string;
  content?: string;
  // thinking_start
  cycle?: number;
  thinking?: boolean;
  // tool_call / tool_result：id = 真实 tool-call id，用于 call↔result 同行配对
  id?: string;
  name?: string;
  // tool_call 透传的脱敏参数（communicate→participant_id/message，get_skill→skill_name）
  arguments?: Record<string, unknown>;
  // tool_result
  is_error?: boolean;
};

