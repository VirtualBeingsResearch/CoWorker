import { Bot, Plus, RadioTower, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';

import { t } from '../../../i18n/admin';
import {
  defaultWeComDisplayName,
  generateWeComInstanceId,
  WECOM_INSTANCE_ID_PATTERN,
} from '../wecomInstanceId';
import type { Json, SettingsPanelProps } from '../types';

function secretPath(instanceId: string) {
  return `wecom.bots.${instanceId}.secret`;
}

export function WeComSettingsPanel({
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: SettingsPanelProps) {
  const bots = (value.bots || {}) as Record<string, Json>;
  const entries = useMemo(() => Object.entries(bots), [bots]);
  const [instanceId, setInstanceId] = useState(
    () => generateWeComInstanceId(Object.keys(bots)),
  );
  const normalizedId = instanceId.trim();
  const canAdd = WECOM_INSTANCE_ID_PATTERN.test(normalizedId) && !bots[normalizedId];

  const updateBot = (id: string, patch: Json) => {
    change('bots', { ...bots, [id]: { ...bots[id], ...patch } });
  };
  const addBot = () => {
    if (!canAdd) return;
    change('bots', {
      ...bots,
      [normalizedId]: {
        enabled: true,
        bot_id: '',
        secret: '',
        ws_url: '',
      },
    });
    setInstanceId(generateWeComInstanceId([...Object.keys(bots), normalizedId]));
  };
  const removeBot = (id: string) => {
    if (!confirm(t('移除企业微信 Bot 实例“{{id}}”？本地的联系人状态会保留，重新添加同名实例时可继续使用。', { id }))) return;
    change('bots', Object.fromEntries(Object.entries(bots).filter(([key]) => key !== id)));
    const nextSecrets = { ...secretInputs };
    delete nextSecrets[secretPath(id)];
    setSecretInputs(nextSecrets);
  };

  return <div className="telegram-settings">
    <section className="telegram-overview">
      <div><RadioTower size={23} /><span><small>{t('企业微信 Bot 信道')}</small><b>{entries.length ? t('{{count}} 个 Bot 实例', { count: entries.length }) : t('尚未配置 Bot')}</b><p>{t('每个实例独立保存 Bot ID、Secret 与长连接地址，建立一条独立的 WebSocket 连接。')}</p></span></div>
      <em>{t('保存后立即重配')}</em>
    </section>

    <section className="telegram-add">
      <div><b>{t('添加 Bot 实例')}</b><small>{t('已生成可编辑的 4 位 instance_id；也可以自定义。')}</small></div>
      <input className="admin-input" value={instanceId} onChange={event => setInstanceId(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addBot(); } }} aria-label={t('WeCom instance_id')} />
      <button className="ghost" disabled={!canAdd} onClick={addBot}><Plus size={14} />{t('添加实例')}</button>
      {normalizedId && !WECOM_INSTANCE_ID_PATTERN.test(normalizedId) && <small className="field-error">{t('使用 1–32 位小写字母、数字、下划线或连字符，并以字母开头。')}</small>}
      {normalizedId && WECOM_INSTANCE_ID_PATTERN.test(normalizedId) && bots[normalizedId] && <small className="field-error">{t('这个 instance_id 已经存在，请换一个。')}</small>}
    </section>

    {entries.length ? <div className="telegram-bot-list">{entries.map(([id, bot]) => {
      const path = secretPath(id);
      const status = secretStatus[path];
      return <article key={id}>
        <header><div className="telegram-bot-mark"><Bot size={19} /></div><span><b>{defaultWeComDisplayName(id)}</b><code>wecom:{id}:single|group:{'{chat_id}'}</code></span><label className="switch"><input type="checkbox" checked={bot.enabled !== false} onChange={event => updateBot(id, { enabled: event.target.checked })} /><i /><span>{t('启用')}</span></label><button className="danger-icon" title={t('移除 Bot 实例')} onClick={() => removeBot(id)}><Trash2 size={15} /></button></header>
        <div className="telegram-bot-fields">
          <label><span>{t('Bot ID')}</span><input className="admin-input" value={bot.bot_id || ''} onChange={event => updateBot(id, { bot_id: event.target.value })} placeholder={t('企业微信应用 Bot ID')} /></label>
          <label><span>{t('企业微信 Secret')}</span><input className="admin-input" type="password" value={secretInputs[path] || ''} onChange={event => setSecretInputs({ ...secretInputs, [path]: event.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' }) : t('企业微信 Secret')} /><small>{status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}</small></label>
          <label><span>{t('WebSocket 地址')}</span><input className="admin-input" value={bot.ws_url || ''} onChange={event => updateBot(id, { ws_url: event.target.value })} placeholder={t('可选；留空使用默认地址')} /></label>
        </div>
      </article>;
    })}</div> : <div className="provider-empty">{t('还没有企业微信 Bot。添加实例后填写 Bot ID 与 Secret。')}</div>}

    <div className="telegram-note"><b>{t('长连接说明')}</b><span>{t('每个实例维护独立的 WebSocket 连接；被新连接挤下线（踢出）的实例不会自动重连，需重新触发配置。')}</span></div>
  </div>;
}
