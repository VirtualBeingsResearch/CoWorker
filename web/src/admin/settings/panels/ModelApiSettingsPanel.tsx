import { t } from '../../../i18n/admin';
import type { Json, SettingsPanelProps } from '../types';

function normalizedTokens(value: Json): Json {
  return value.tokens && typeof value.tokens === 'object' && !Array.isArray(value.tokens)
    ? value.tokens
    : {};
}

export function ModelApiSettingsPanel({
  value,
  change,
  secretStatus,
}: SettingsPanelProps) {
  const tokens = normalizedTokens(value);
  const entries = Object.entries(tokens) as Array<[string, Json]>;

  return <div className="settings-panel model-api-panel">
    <label className="switch">
      <input type="checkbox" checked={!!value.enabled} onChange={event => change('enabled', event.target.checked)} />
      <i />
      <span>{t('启用模型接口')}</span>
    </label>
    <small>{t('启用后，OpenAI 兼容客户端可以用 base_url + 令牌与搭档对话；令牌在「人物」页面为具体人物签发，保存后立即生效。')}</small>

    {entries.length ? <div className="telegram-bot-list">{entries.map(([key, entry]) => {
      const status = secretStatus[`model_api.tokens.${key}.token`];
      return <article key={key}>
        <header><span><b>{entry.display_name || key}</b><code>api:{key}</code></span></header>
        <div className="telegram-bot-fields">
          <label><span>{t('显示名称')}</span><span>{entry.display_name || '—'}</span></label>
          <label><span>{t('接入令牌')}</span><small>{status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}</small></label>
          {entry.note ? <label><span>{t('令牌备注')}</span><span>{entry.note}</span></label> : null}
        </div>
      </article>;
    })}</div> : <div className="provider-empty">{t('还没有接入令牌；在「人物」页面选择人物后生成。')}</div>}

    <div className="telegram-bot-fields">
      <label><span>{t('空闲提醒（秒）')}</span><input className="admin-input" type="number" min="10" max="3600" step="1" value={value.nudge_seconds ?? 300} onChange={event => change('nudge_seconds', Number(event.target.value))} /><small>{t('一轮会话持续无输出达到该时长时，提醒搭档汇报进度。')}</small></label>
      <label><span>{t('空闲断开（秒）')}</span><input className="admin-input" type="number" min="60" max="86400" step="1" value={value.timeout_seconds ?? 1200} onChange={event => change('timeout_seconds', Number(event.target.value))} /><small>{t('持续无输出达到该时长时，通知搭档并关闭本次 HTTP 响应。')}</small></label>
    </div>
  </div>;
}
