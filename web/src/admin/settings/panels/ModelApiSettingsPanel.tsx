import { Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';

import { t } from '../../../i18n/admin';
import type { Json, SettingsPanelProps } from '../types';

const MODEL_API_TOKEN_KEY_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/;

function normalizedTokens(value: Json): Json {
  return value.tokens && typeof value.tokens === 'object' && !Array.isArray(value.tokens)
    ? value.tokens
    : {};
}

export function tokenSecretPath(key: string) {
  return `model_api.tokens.${key}.token`;
}

export function ModelApiSettingsPanel({
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: SettingsPanelProps) {
  const tokens = normalizedTokens(value);
  const entries = Object.entries(tokens) as Array<[string, Json]>;
  const [newKey, setNewKey] = useState('');
  const [newDisplayName, setNewDisplayName] = useState('');
  const keyTaken = Boolean(newKey && tokens[newKey]);
  const keyInvalid = Boolean(newKey) && !MODEL_API_TOKEN_KEY_PATTERN.test(newKey);
  const canAdd = Boolean(newKey) && !keyTaken && !keyInvalid;

  const updateToken = (key: string, field: string, next: unknown) => {
    change('tokens', { ...tokens, [key]: { ...(tokens[key] as Json), [field]: next } });
  };

  const removeToken = (key: string) => {
    const next = { ...tokens };
    delete next[key];
    change('tokens', next);
    const secrets = { ...secretInputs };
    delete secrets[tokenSecretPath(key)];
    setSecretInputs(secrets);
  };

  const addToken = () => {
    const key = newKey.trim();
    if (!canAdd) return;
    change('tokens', {
      ...tokens,
      [key]: { token: '', display_name: newDisplayName.trim() },
    });
    setNewKey('');
    setNewDisplayName('');
  };

  return <div className="settings-panel model-api-panel">
    <label className="switch">
      <input type="checkbox" checked={!!value.enabled} onChange={event => change('enabled', event.target.checked)} />
      <i />
      <span>{t('启用模型接口')}</span>
    </label>
    <small>{t('启用后，OpenAI 兼容客户端可以用 base_url + 令牌与搭档对话；令牌即参与者身份，保存后立即生效。')}</small>

    <section className="telegram-add">
      <div><b>{t('添加接入令牌')}</b><small>{t('令牌名称用作 participant 地址的一部分（如 api:alice）；也可以只填显示名称。')}</small></div>
      <input className="admin-input" value={newKey} onChange={event => setNewKey(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addToken(); } }} aria-label={t('令牌名称')} placeholder="alice" />
      <input className="admin-input" value={newDisplayName} onChange={event => setNewDisplayName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addToken(); } }} aria-label={t('显示名称')} placeholder={t('显示名称')} />
      <button className="ghost" disabled={!canAdd} onClick={addToken}><Plus size={14} />{t('添加令牌')}</button>
      {keyInvalid && <small className="field-error">{t('使用 1–32 位小写字母、数字、下划线或连字符，并以字母开头。')}</small>}
      {keyTaken && <small className="field-error">{t('这个令牌名称已经存在，请换一个。')}</small>}
    </section>

    {entries.length ? <div className="telegram-bot-list">{entries.map(([key, entry]) => {
      const secretPath = tokenSecretPath(key);
      const status = secretStatus[secretPath];
      return <article key={key}>
        <header><span><b>{entry.display_name || key}</b><code>api:{entry.display_name || key}</code></span><button className="danger-icon" title={t('移除令牌')} onClick={() => removeToken(key)}><Trash2 size={15} /></button></header>
        <div className="telegram-bot-fields">
          <label><span>{t('显示名称')}</span><input className="admin-input" value={entry.display_name || ''} maxLength={80} onChange={event => updateToken(key, 'display_name', event.target.value)} placeholder={t('例如 Alice')} /></label>
          <label><span>{t('接入令牌')}</span><input className="admin-input" type="password" value={secretInputs[secretPath] || ''} onChange={event => setSecretInputs({ ...secretInputs, [secretPath]: event.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' }) : t('至少 8 个字符，客户端以 Bearer 方式携带')} /><small>{status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}</small></label>
        </div>
      </article>;
    })}</div> : <div className="provider-empty">{t('还没有接入令牌。添加并保存后，客户端即可通过 /v1/chat/completions 接入。')}</div>}

    <div className="telegram-bot-fields">
      <label><span>{t('空闲提醒（秒）')}</span><input className="admin-input" type="number" min="10" max="3600" step="1" value={value.nudge_seconds ?? 300} onChange={event => change('nudge_seconds', Number(event.target.value))} /><small>{t('一轮会话持续无输出达到该时长时，提醒搭档汇报进度。')}</small></label>
      <label><span>{t('空闲断开（秒）')}</span><input className="admin-input" type="number" min="60" max="86400" step="1" value={value.timeout_seconds ?? 1200} onChange={event => change('timeout_seconds', Number(event.target.value))} /><small>{t('持续无输出达到该时长时，通知搭档并关闭本次 HTTP 响应。')}</small></label>
      <label><span>{t('场景文档单段上限（字符）')}</span><input className="admin-input" type="number" min="500" max="100000" step="100" value={value.scenario_max_chars ?? 6000} onChange={event => change('scenario_max_chars', Number(event.target.value))} /><small>{t('调用方 system prompt 与工具 schema 落盘保存时，单段材料的截断预算。')}</small></label>
    </div>
  </div>;
}
