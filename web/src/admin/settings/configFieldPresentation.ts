export type ConfigFieldEditor =
  | 'default'
  | 'locale'
  | 'fallback-list'
  | 'cors-list'
  | 'participant-list'
  | 'transport-list';

export type ConfigFieldPresentation = {
  editor: ConfigFieldEditor;
  hint?: string;
  placeholder?: string;
  inputType?: 'url';
  minimum?: number;
  maximum?: number;
  step?: number | 'any';
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
  if (path === 'llm.tool_choice_required') {
    return {
      editor: 'default',
      hint: '有工具可用时要求模型至少调用一个；关闭仅用于兼容不支持 required 工具选择的 API。',
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
