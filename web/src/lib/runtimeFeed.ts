import type { RuntimeLogEvent } from '../api/types';
import { t } from '../i18n/admin';

const EXPLORE_TOOLS = new Set([
  'search_web', 'fetch_url',
  'browser_open', 'browser_screenshot', 'browser_action', 'browser_get_content',
  'browser_close', 'browser_view', 'browser_list_sessions',
  'read_file', 'write_file', 'list_directory', 'find_files', 'grep_files',
  'query_memory', 'get_context',
  'execute_code', 'get_code_result',
  'view_image', 'visual_analyze',
]);

/**
 * 从日志流事件推断 agent 当前活动状态。
 * state: 'thinking' | 'sleeping' | 'communicating' | 'exploring' | 'idle'
 */
export function activityStateFromEvents(events: RuntimeLogEvent[]): { state: string } {
  if (events.length === 0) return { state: 'idle' };

  const resolved = new Set<string>();
  for (const e of events) {
    if (e.type === 'tool_result' && e.id) resolved.add(e.id);
  }

  // 找最新的 thinking_start 和 llm_response 位置
  let lastThinkIdx = -1;
  let lastLlmIdx = -1;
  for (let i = events.length - 1; i >= 0; i--) {
    if (lastThinkIdx === -1 && events[i].type === 'thinking_start') lastThinkIdx = i;
    if (lastLlmIdx === -1 && events[i].type === 'llm_response') lastLlmIdx = i;
    if (lastThinkIdx !== -1 && lastLlmIdx !== -1) break;
  }
  // 有 thinking_start 且之后没有 llm_response → 仍在思考
  if (lastThinkIdx !== -1 && (lastLlmIdx === -1 || lastLlmIdx < lastThinkIdx)) {
    return { state: 'thinking' };
  }

  // 从最新往前找未结算的 tool_call
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== 'tool_call') continue;
    if (e.id && resolved.has(e.id)) continue;
    const name = e.name || '';
    if (name === 'sleep') return { state: 'sleeping' };
    if (name === 'communicate') return { state: 'communicating' };
    if (EXPLORE_TOOLS.has(name)) return { state: 'exploring' };
    return { state: 'thinking' };
  }

  return { state: 'idle' };
}

// 运行日志的「展示行」模型 —— 把 append-only 的后端日志事件（interactions.jsonl，经
// RuntimeEventCollector 初级处理）归并成可变的行：thinking 是长态（thinking_start →
// llm_response 收口的生命周期），tool_call 与 tool_result 按 tool-call id 配对、在同一行
// 原地结算（调用中 → 成功/失败）。communicate / get_skill 从 tool_call 派生专属行。
export type FeedKind =
  | 'msg_in'
  | 'msg_out'
  | 'thinking'
  | 'sleep'
  | 'tool'
  | 'skill_load'
  | 'raw';

export type FeedStatus = 'active' | 'done' | 'pending' | 'ok' | 'err';

export interface FeedRow {
  /** 跨多次派生保持稳定，避免重放入场动画；工具行取 tool-call id，其余取 seq */
  key: string;
  kind: FeedKind;
  status?: FeedStatus;
  ts?: string;
  icon: string;
  tag: string;
  text: string;
  /** 已结算阶段的耗时（由起止日志时间戳计算，单位：毫秒）。 */
  durationMs?: number;
  /** 是否在文末挂呼吸省略号（thinking 进行时） */
  dots?: boolean;
  /** 附加 className（工具家族 t-comm / t-code… 决定图标动效与配色） */
  cls?: string;
  /** Whether this row falls back to the generic tool-call wording. */
  genericTool?: boolean;
}

// 工具 → 专属图标 / 家族（家族决定配色与图标动效）。未命中走默认齿轮。
interface ToolMeta { icon: string; family: string; }
const TOOL_META: Record<string, ToolMeta> = {
  communicate: { icon: '📡', family: 'comm' },
  list_connections: { icon: '📡', family: 'comm' },
  query_memory: { icon: '🗂️', family: 'mem' },
  manage_memory: { icon: '🗂️', family: 'mem' },
  get_context: { icon: '🗂️', family: 'mem' },
  compress_memory: { icon: '🗜️', family: 'mem' },
  clear_short_term_memory: { icon: '🧽', family: 'mem' },
  manage_pinned_context: { icon: '📌', family: 'mem' },
  execute_code: { icon: '⌨️', family: 'code' },
  get_code_result: { icon: '⌨️', family: 'code' },
  kill_code_job: { icon: '🛑', family: 'code' },
  read_file: { icon: '📄', family: 'file' },
  write_file: { icon: '📝', family: 'file' },
  list_directory: { icon: '📁', family: 'file' },
  find_files: { icon: '🔎', family: 'file' },
  grep_files: { icon: '🔎', family: 'file' },
  search_web: { icon: '🌐', family: 'web' },
  fetch_url: { icon: '🌐', family: 'web' },
  browser_open: { icon: '🧭', family: 'web' },
  browser_screenshot: { icon: '🧭', family: 'web' },
  browser_action: { icon: '🧭', family: 'web' },
  browser_get_content: { icon: '🧭', family: 'web' },
  browser_close: { icon: '🧭', family: 'web' },
  browser_view: { icon: '🧭', family: 'web' },
  browser_list_sessions: { icon: '🧭', family: 'web' },
  bubble_spawn: { icon: '🫧', family: 'bubble' },
  bubble_check: { icon: '🫧', family: 'bubble' },
  bubble_send: { icon: '🫧', family: 'bubble' },
  bubble_cancel: { icon: '🫧', family: 'bubble' },
  bubble_list: { icon: '🫧', family: 'bubble' },
  bubble_done: { icon: '🫧', family: 'bubble' },
  set_alarm: { icon: '⏰', family: 'alarm' },
  list_alarms: { icon: '⏰', family: 'alarm' },
  cancel_alarm: { icon: '⏰', family: 'alarm' },
  task_create: { icon: '📋', family: 'task' },
  task_get: { icon: '📋', family: 'task' },
  task_list: { icon: '📋', family: 'task' },
  task_update: { icon: '📋', family: 'task' },
  get_skill: { icon: '📖', family: 'skill' },
  view_image: { icon: '🖼️', family: 'vision' },
  visual_analyze: { icon: '🎞️', family: 'vision' },
  breathe: { icon: '🌬️', family: 'breathe' },
  switch_model: { icon: '🔀', family: 'model' },
  restart_self: { icon: '♻️', family: 'restart' },
};
function toolMeta(name: string): ToolMeta {
  return TOOL_META[name] || { icon: '⚙️', family: 'spin' };
}

function clean(s?: unknown): string {
  return (typeof s === 'string' ? s : s == null ? '' : String(s)).trim();
}

// 前端合成文案（多字段拼接 / JSON 兜底）可能很长，单靠 CSS text-overflow 在 preserve-3d 翻转卡里
// 不一定可靠 —— 故在数据侧也兜底截断并缀省略号。已以 … 结尾（后端截断过的字段）不重复叠加。
const MAX_TEXT = 100;
// 引号内的自由文本（消息/查询）单独的截断预算：比整行预算小，确保 frontend 自身就能截断并缀省略号，
// 不依赖后端是否已截断（否则像 message_in 这种合成短于整行预算的行，整行 cut 永不触发，… 全靠后端）。
const PREVIEW_MAX = 80;
function cut(s: string, n: number = MAX_TEXT): string {
  return s.length <= n ? s : s.slice(0, n).replace(/…$/, '') + '…';
}

/**
 * 后端日志使用 ISO 时间戳；仅在完整的起止配对且时间顺序正确时展示耗时。
 * 这样历史回放窗口刚好截断起点的孤立 result 不会被误标为 0 秒。
 */
function durationBetween(start?: string, end?: string): number | undefined {
  const startedAt = start ? Date.parse(start) : NaN;
  const endedAt = end ? Date.parse(end) : NaN;
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt < startedAt) return undefined;
  return endedAt - startedAt;
}

function recordDuration(row: FeedRow, completedAt?: string): void {
  const durationMs = durationBetween(row.ts, completedAt);
  if (durationMs !== undefined) row.durationMs = durationMs;
}

/** 非工具行的稳定 key：用单调 seq；缺失时退回 ts。 */
function seqKey(e: RuntimeLogEvent): string {
  return e.seq != null ? `s${e.seq}` : `t${e.ts || ''}`;
}
/** 工具行的稳定 key：用 tool-call id（保证 call↔result 同 key 原地结算 + 去重）。 */
function toolKey(e: RuntimeLogEvent): string {
  return e.id || seqKey(e);
}

function msgInMeta(e: RuntimeLogEvent): Pick<FeedRow, 'icon' | 'tag' | 'cls'> {
  const participant = clean(e.participant_id).toLowerCase();
  const source = clean(e.source).toLowerCase();
  if (participant === 'system') return { icon: '⚙️', tag: t('系统消息'), cls: 'm-system' };
  if (source === 'bubble') return { icon: '🫧', tag: t('泡泡来信'), cls: 'm-bubble' };
  if (source === 'ws') return { icon: '🔌', tag: t('连接消息'), cls: 'm-connection' };
  if (source === 'rest' || source === 'api') return { icon: '📨', tag: t('收到消息'), cls: 'm-user' };
  return { icon: '📨', tag: t('收到消息'), cls: 'm-user' };
}

function msgInRow(e: RuntimeLogEvent): FeedRow {
  const who = clean(e.participant_id) || t('未知');
  const preview = cut(clean(e.content), PREVIEW_MAX);
  const meta = msgInMeta(e);
  return { key: seqKey(e), kind: 'msg_in', ts: e.ts, ...meta, text: cut(`${who} · "${preview}"`) };
}

// communicate 工具调用 → 发送回复行（收件人/消息从 tool_call 的脱敏参数派生）
function commRow(e: RuntimeLogEvent): FeedRow {
  const args = e.arguments || {};
  const who = clean(args.participant_id) || t('未知');
  const preview = cut(clean(args.message), PREVIEW_MAX);
  return { key: toolKey(e), kind: 'msg_out', ts: e.ts, icon: '✈️', tag: t('发送回复'), text: cut(`→ ${who} · "${preview}"`) };
}

// get_skill 工具调用 → 加载技能行（技能名从 tool_call 的参数派生）
function skillCallRow(e: RuntimeLogEvent): FeedRow {
  const args = e.arguments || {};
  const name = clean(args.skill_name) || 'skill';
  return { key: toolKey(e), kind: 'skill_load', ts: e.ts, icon: '📖', tag: t('加载技能'), text: t('{{name}} 已挂载', { name }) };
}

// 从后端透传的脱敏摘要参数取一个字段（已截断），缺失时空串。
function arg(e: RuntimeLogEvent, k: string): string {
  return clean((e.arguments || {})[k]);
}

// 工具 → 按种别生成可读摘要 { tag（动词标签）, text（内容）}。用后端透传的摘要参数把
// 「toolname() 调用中」升级成一句话。表外工具回退到通用「工具调用 / name()」（见 toolPendingRow）。
// communicate / get_skill 已有专属行（commRow/skillCallRow），不在此表。
const TOOL_SUMMARY: Record<string, (e: RuntimeLogEvent) => { tag: string; text: string }> = {
  read_file: e => ({ tag: t('读取文件'), text: arg(e, 'path') || t('读取文件') }),
  write_file: e => ({ tag: t('写入文件'), text: arg(e, 'path') || t('写入文件') }),
  list_directory: e => ({ tag: t('浏览目录'), text: arg(e, 'path') || t('当前目录') }),
  find_files: e => ({ tag: t('查找文件'), text: arg(e, 'pattern') || t('查找文件') }),
  grep_files: e => ({ tag: t('搜索内容'), text: [arg(e, 'pattern'), arg(e, 'path')].filter(Boolean).join(' · ') || t('搜索内容') }),
  search_web: e => ({ tag: t('联网搜索'), text: arg(e, 'query') ? `"${cut(arg(e, 'query'), PREVIEW_MAX)}"` : t('联网搜索') }),
  fetch_url: e => ({ tag: t('抓取网页'), text: arg(e, 'url') || t('抓取网页') }),
  query_memory: e => {
    if (arg(e, 'start') || arg(e, 'end')) {
      return { tag: t('回忆时间窗'), text: [arg(e, 'start'), arg(e, 'end')].filter(Boolean).join(' → ') || t('回忆时间窗') };
    }
    return { tag: t('检索记忆'), text: arg(e, 'query') ? `"${cut(arg(e, 'query'), PREVIEW_MAX)}"` : t('查看记忆时间段') };
  },
  manage_memory: e => {
    const act = ({ write: '写入', update: '更新', associate: '打标', delete: '删除' } as Record<string, string>)[arg(e, 'action')] || '整理';
    const memoryAction = t('{{action}}记忆', { action: t(act) });
    return { tag: memoryAction, text: arg(e, 'content') || memoryAction };
  },
  execute_code: e => ({ tag: t('执行代码'), text: arg(e, 'code') || t('执行代码') }),
  task_create: e => ({ tag: t('新建任务'), text: arg(e, 'description') || t('新建任务') }),
  task_update: e => ({ tag: t('更新任务'), text: [arg(e, 'task_id'), arg(e, 'status')].filter(Boolean).join(' → ') || t('更新任务') }),
  set_alarm: e => ({ tag: t('设置提醒'), text: [arg(e, 'trigger_at'), arg(e, 'message')].filter(Boolean).join(' · ') || t('设置提醒') }),
  bubble_spawn: e => ({ tag: t('分裂泡泡'), text: arg(e, 'goal') || t('分裂泡泡') }),
  switch_model: e => ({ tag: t('切换模型'), text: arg(e, 'model_id') || t('切换模型') }),
};

function toolPendingRow(e: RuntimeLogEvent): FeedRow {
  const tool = clean(e.name) || 'tool';
  const meta = toolMeta(tool);
  const sum = TOOL_SUMMARY[tool]?.(e);
  return {
    key: toolKey(e),
    kind: 'tool',
    status: 'pending',
    ts: e.ts,
    icon: meta.icon,
    tag: sum?.tag || t('工具调用'),
    text: cut(sum?.text || t('{{tool}}() 调用中', { tool })),
    cls: `t-${meta.family}`,
    genericTool: !sum,
  };
}

// sleep 工具调用 → 休息长态行（与 thinking 相反的缓慢蓝色呼吸 + z·z·z），由其 tool_result 收口为「已唤醒」
function sleepRow(e: RuntimeLogEvent): FeedRow {
  const secs = clean((e.arguments || {}).seconds);
  return {
    key: toolKey(e),
    kind: 'sleep',
    status: 'active',
    ts: e.ts,
    icon: '💤',
    tag: t('休息中'),
    text: secs
      ? t('休眠 {{seconds}}秒 · 仅保留心跳与轻量监听', { seconds: secs })
      : t('进入低频待机 · 仅保留心跳与轻量监听'),
  };
}

// 收口休息行（sleep 的 tool_result 抵达＝醒来；提前唤醒也走此路）
function concludeSleep(row: FeedRow, wakeContent?: string): void {
  row.status = 'done';
  row.icon = '☀️';
  row.tag = t('已唤醒');
  row.text = wakeContent ? cut(wakeContent) : t('已从休眠中恢复运行');
}

function thinkingActiveRow(e: RuntimeLogEvent): FeedRow {
  const cycle = e.cycle != null ? t('第 {{cycle}} 轮 · ', { cycle: e.cycle }) : '';
  return { key: seqKey(e), kind: 'thinking', status: 'active', ts: e.ts, icon: '🧠', tag: t('思考中'), text: `${cycle}${t('揉合线索、权衡下一步')}`, dots: true };
}

function rawRow(e: RuntimeLogEvent): FeedRow {
  const text = clean(e.content) || clean(e.name) || JSON.stringify(e);
  return { key: seqKey(e), kind: 'raw', ts: e.ts, icon: '·', tag: t('原始事件'), text: cut(text) };
}

/** 收口思考行：可选地用模型这一轮的输出（llm_response.content）作为「思考完成」文案。 */
function concludeThinking(row: FeedRow, conclusion?: string, completedAt?: string): void {
  row.status = 'done';
  row.icon = '💡';
  row.tag = t('思考完成');
  row.text = cut(clean(conclusion) || t('已得出判断'));
  row.dots = false;
  recordDuration(row, completedAt);
}

/** 收口仍处于 active 的思考行（除非本次事件本身就是 thinking_start）。 */
function settleThinking(rows: FeedRow[], keepActive: boolean, completedAt?: string): void {
  if (keepActive) return;
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    if (r.kind === 'thinking' && r.status === 'active') concludeThinking(r, undefined, completedAt);
  }
}

function lastActiveThinking(rows: FeedRow[]): FeedRow | undefined {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].kind === 'thinking' && rows[i].status === 'active') return rows[i];
  }
  return undefined;
}

function resolveTool(row: FeedRow, e: RuntimeLogEvent): void {
  const ok = !e.is_error;
  const preview = clean(e.content);
  row.status = ok ? 'ok' : 'err';
  // 保留工具专属图标 + 调用时的种别文案（读取了哪个文件、搜了什么…），靠配色/动效区分成败。
  // 通用行（无专属 tag）才回落到「工具完成/工具失败」。
  if (row.genericTool) row.tag = ok ? t('工具完成') : t('工具失败');
  if (preview) row.text = cut(`${row.text} · ${preview}`);
  else if (!ok) row.text = `${row.text} · ${t('失败')}`;
}

/**
 * 把后端日志事件流派生成展示行。纯函数、可记忆化（事件流上限 80，整表重算开销可忽略）。
 */
export function deriveFeedRows(events: RuntimeLogEvent[]): FeedRow[] {
  const rows: FeedRow[] = [];

  for (const e of events) {
    const type = e.type || 'raw';

    // —— tool 结果：按 tool-call id 命中对应调用行，原地结算 ——
    if (type === 'tool_result') {
      const ok = !e.is_error;
      const target = rows.find(r => r.key === e.id)
        || [...rows].reverse().find(r => r.kind === 'tool' && r.status === 'pending');
      if (target) {
        if (target.kind === 'msg_out') { if (!ok) target.tag = t('发送失败'); }
        else if (target.kind === 'skill_load') { if (!ok) { target.tag = t('技能加载失败'); target.text = target.text.replace(t('已挂载'), t('加载失败')); } }
        else if (target.kind === 'sleep') concludeSleep(target, clean(e.content));
        else resolveTool(target, e);
        recordDuration(target, e.ts);
        continue;
      }
      // 没有对应调用（如历史回放窗口起点在调用之后）→ 单独成行兜底
      if (clean(e.name) === 'sleep') {
        const slept = sleepRow(e);
        concludeSleep(slept, clean(e.content));
        rows.push(slept);
        continue;
      }
      const standalone = toolPendingRow(e);
      resolveTool(standalone, e);
      rows.push(standalone);
      continue;
    }

    // —— 长态起点：thinking_start ——
    if (type === 'thinking_start') {
      settleThinking(rows, true);
      if (!lastActiveThinking(rows)) rows.push(thinkingActiveRow(e));
      continue;
    }

    // —— 长态终点：llm_response 收口当前思考行（以其内容为完成文案）——
    if (type === 'llm_response') {
      const active = lastActiveThinking(rows);
      if (active) concludeThinking(active, e.content, e.ts);
      else rows.push({ ...thinkingActiveRow(e), status: 'done', icon: '💡', tag: t('思考完成'), text: cut(clean(e.content) || t('已得出判断')), dots: false });
      continue;
    }

    // —— 其余事件：先收口任何悬挂的思考行，再成行 ——
    settleThinking(rows, false, e.ts);

    switch (type) {
      case 'message_in':
        rows.push(msgInRow(e));
        break;
      case 'tool_call': {
        // 按工具名派生专属行：communicate→发送回复、get_skill→加载技能，其余→通用工具行
        const toolName = clean(e.name);
        if (toolName === 'communicate') rows.push(commRow(e));
        else if (toolName === 'get_skill') rows.push(skillCallRow(e));
        else if (toolName === 'sleep') rows.push(sleepRow(e));
        else rows.push(toolPendingRow(e));
        break;
      }
      default:
        rows.push(rawRow(e));
    }
  }

  return rows;
}
