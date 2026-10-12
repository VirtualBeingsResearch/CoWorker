export type ConfigFieldEditor =
  | 'default'
  | 'locale'
  | 'choice'
  | 'fallback-list'
  | 'cors-list'
  | 'participant-list'
  | 'transport-list';

export type ConfigChoice = {
  value: string;
  label: string;
};

export type ConfigFieldPresentation = {
  editor: ConfigFieldEditor;
  hint?: string;
  placeholder?: string;
  inputType?: 'url';
  minimum?: number;
  maximum?: number;
  step?: number | 'any';
  choices?: ConfigChoice[];
};

const POSITIVE_INTEGER_FIELDS = new Set([
  'llm.max_tokens',
  'memory.short_term_max_tokens',
  'memory.auto_recall_limit',
  'agent.bubble_max_concurrent',
]);

const NON_NEGATIVE_INTEGER_FIELDS = new Set([
  'agent.idle_sleep_seconds',
  'agent.bubble_timeout_resume_seconds',
  'agent.channel_progress_reply_reminder_seconds',
  'agent.channel_progress_timeout_seconds',
  'relay.auth_epoch',
]);

const INTEGER_FIELDS = new Set([
  'memory.tree_backfill_max_leaves',
  'memory.tree_backfill_concurrency',
  'memory.tree_merge_reach_depth',
  'agent.interaction_log_rotation_bytes',
  'agent.inbox_batch_max',
  'agent.code_hard_timeout',
  'agent.image_max_dimension',
  'agent.subconscious_max_cycles',
]);

const FRACTION_FIELDS = new Set([
  'memory.compress_ratio',
  'memory.tree_spine_cap_fraction',
  'memory.auto_recall_relevance_threshold',
]);

export function configFieldPresentation(
  path: string,
  context: { passiveMode?: boolean } = {},
): ConfigFieldPresentation {
  if (path === 'i18n.locale') {
    return {
      editor: 'locale',
      hint: '保存后需安全重启；不会自动翻译用户内容或历史数据',
    };
  }
  if (path === 'llm.fallbacks') {
    return {
      editor: 'fallback-list',
      hint: '按接棒顺序填写 provider 或 provider/model；留空表示不配置降级链。',
      placeholder: 'provider/model',
    };
  }
  if (path === 'api.cors_origins') {
    return {
      editor: 'cors-list',
      hint: '填写允许访问管理员 API 的完整浏览器 Origin；反向代理域名需要在这里单独加入。',
      placeholder: 'https://coworker.example.com',
    };
  }
  if (path === 'agent.bubble_handoff_transparency_participant_matches') {
    return {
      editor: 'participant-list',
      hint: '支持完整 participant_id 和 glob（例如 weixin:*）。留空表示不按 participant 匹配。',
      placeholder: 'weixin:*',
    };
  }
  if (path === 'agent.bubble_handoff_transparency_stream_transports') {
    return { editor: 'transport-list' };
  }

  if (
    path === 'agent.concurrency_hint_window_seconds'
    || path === 'agent.concurrency_hint_cooldown_seconds'
  ) {
    return {
      editor: 'default',
      step: 'any',
      hint: path === 'agent.concurrency_hint_window_seconds'
        ? '窗口内出现过来信的会话视为同时活跃。'
        : '两次并发提示之间的最小间隔。',
    };
  }
  if (path === 'agent.concurrency_hint_threshold') {
    return {
      editor: 'default',
      minimum: 2,
      step: 1,
      hint: '窗口内未被泡泡接管的会话数上穿该阈值时注入提示。',
    };
  }

  if (path === 'agent.channel_progress_enabled') {
    return {
      editor: 'default',
      hint: '开启后，企业微信与 Telegram 的私聊会立即显示处理中提示，正式回复覆盖同一条；群聊不显示。',
    };
  }

  if (path === 'api.port') {
    return { editor: 'default', minimum: 1, maximum: 65_535, step: 1 };
  }
  if (POSITIVE_INTEGER_FIELDS.has(path)) {
    return {
      editor: 'default',
      minimum: 1,
      step: 1,
      hint: path === 'llm.max_tokens'
        ? '模型单次响应允许生成的最大 token 数'
        : undefined,
    };
  }
  if (NON_NEGATIVE_INTEGER_FIELDS.has(path)) {
    return {
      editor: 'default',
      minimum: 0,
      step: 1,
      hint: path === 'agent.idle_sleep_seconds'
        ? context.passiveMode
          ? 'Passive 模式忽略此间隔；sleep(0) 表示持续等待外部事件。'
          : '主动模式空闲后多久自行唤醒；0 表示立即进入下一轮。'
        : path === 'agent.channel_progress_reply_reminder_seconds'
          ? '超过该秒数仍未 communicate 时向模型注入催促；0 表示只显示处理提示、不催促。'
          : path === 'agent.channel_progress_timeout_seconds'
            ? '超过该秒数仍未回复时，处理中提示会被换成一句中性说明并结束，不会再一直停在「正在思考中…」；0 表示一直保留到你覆盖。建议大于催促秒数。'
            : undefined,
    };
  }
  if (INTEGER_FIELDS.has(path)) {
    return { editor: 'default', step: 1 };
  }
  if (FRACTION_FIELDS.has(path)) {
    return {
      editor: 'default',
      minimum: 0,
      maximum: 1,
      step: 0.01,
    };
  }

  if (path === 'llm.default_model') {
    return {
      editor: 'default',
      hint: 'Provider 连接没有单独指定模型时使用',
    };
  }
  if (path === 'web_search.timeout_seconds') {
    return {
      editor: 'default',
      minimum: 1,
      maximum: 120,
      step: 1,
      hint: '单次搜索请求的超时秒数。',
    };
  }
  if (path === 'web_search.strategy') {
    return {
      editor: 'choice',
      hint: '自动使用第一个已配置的后端；固定则使用下方选择的后端。DDGS 始终可用。',
      choices: [
        { value: 'auto', label: '自动（按已配置后端）' },
        { value: 'fixed', label: '固定后端' },
      ],
    };
  }
  if (path === 'web_search.provider') {
    return {
      editor: 'choice',
      hint: '仅在策略为固定后端时使用。',
      choices: [
        { value: 'ddgs', label: 'DDGS' },
        { value: 'bocha', label: 'Bocha 博查' },
        { value: 'zhipu', label: 'Zhipu 智谱' },
        { value: 'qianfan', label: 'Qianfan 千帆' },
        { value: 'linkai', label: 'LinkAI' },
        { value: 'anysearch', label: 'AnySearch' },
        { value: 'serply', label: 'Serply' },
        { value: 'tavily', label: 'Tavily' },
        { value: 'searxng', label: 'SearXNG' },
        { value: 'keenable', label: 'Keenable' },
      ],
    };
  }
  if (path === 'web_search.ddgs_backend') {
    return {
      editor: 'choice',
      hint: 'DDGS 内部引擎。auto 由库自行选择。',
      choices: ['auto', 'bing', 'brave', 'duckduckgo', 'google', 'grokipedia', 'mojeek', 'startpage', 'yandex', 'yahoo', 'wikipedia']
        .map(value => ({ value, label: value })),
    };
  }
  if (path === 'web_search.zhipu_search_engine') {
    return {
      editor: 'choice',
      choices: [
        { value: 'search_pro', label: 'search_pro' },
        { value: 'search_std', label: 'search_std' },
        { value: 'search_pro_sogou', label: 'search_pro_sogou' },
        { value: 'search_pro_quark', label: 'search_pro_quark' },
      ],
    };
  }
  if (path === 'web_search.zhipu_content_size') {
    return {
      editor: 'choice',
      hint: '控制智谱返回摘要的长度。',
      choices: [
        { value: '', label: '默认' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
      ],
    };
  }
  if (path === 'web_search.tavily_search_depth') {
    return {
      editor: 'choice',
      choices: [
        { value: 'basic', label: 'basic' },
        { value: 'advanced', label: 'advanced' },
      ],
    };
  }
  if (path === 'web_search.anysearch_zone') {
    return {
      editor: 'choice',
      choices: [
        { value: '', label: '默认' },
        { value: 'cn', label: 'cn' },
        { value: 'intl', label: 'intl' },
      ],
    };
  }
  if (path === 'web_search.zhipu_api_key') {
    return { editor: 'default', hint: '留空时复用模型配置中的智谱 API Key。' };
  }
  if (path === 'web_search.anysearch_anonymous' || path === 'web_search.keenable_anonymous') {
    return { editor: 'default', hint: '开启后可以不填密钥，使用对方的匿名额度。' };
  }
  if (path === 'web_search.searxng_url' || path.endsWith('_api_base')) {
    return {
      editor: 'default',
      inputType: 'url',
      hint: path === 'web_search.searxng_url' ? '自托管 SearXNG 的根地址，不需要密钥。' : '留空时使用厂商默认地址。',
    };
  }
  if (path === 'api.public_url') {
    return {
      editor: 'default',
      hint: '反向代理下浏览器实际访问的 HTTP(S) 地址；留空时根据监听地址推导。',
      placeholder: 'https://coworker.example.com',
      inputType: 'url',
    };
  }
  return { editor: 'default' };
}
