import { Bot, Plus, RadioTower, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';

import { t } from '../../../i18n/admin';
import {
  defaultTelegramDisplayName,
  generateTelegramInstanceId,
  TELEGRAM_INSTANCE_ID_PATTERN,
} from '../telegramInstanceId';
import type { Json, SettingsPanelProps } from '../types';

function tokenPath(instanceId: string) {
  return `telegram.bots.${instanceId}.bot_token`;
}

export function TelegramSettingsPanel({
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: SettingsPanelProps) {
  const bots = (value.bots || {}) as Record<string, Json>;
  const entries = useMemo(() => Object.entries(bots), [bots]);
  const [instanceId, setInstanceId] = useState(
    () => generateTelegramInstanceId(Object.keys(bots)),
  );
  const normalizedId = instanceId.trim();
  const canAdd = TELEGRAM_INSTANCE_ID_PATTERN.test(normalizedId) && !bots[normalizedId];

  const updateBot = (id: string, patch: Json) => {
    change('bots', { ...bots, [id]: { ...bots[id], ...patch } });
  };
  const addBot = () => {
    if (!canAdd) return;
    change('bots', {
      ...bots,
      [normalizedId]: {
        enabled: true,
        display_name: defaultTelegramDisplayName(normalizedId),
        bot_token: '',
        api_base_url: 'https://api.telegram.org',
        local_mode: false,
        poll_timeout_seconds: 30,
      },
    });
    setInstanceId(generateTelegramInstanceId([...Object.keys(bots), normalizedId]));
  };
  const removeBot = (id: string) => {
    if (!confirm(t('移除 Telegram Bot 实例“{{id}}”？本地 offset 与联系人状态会保留，重新添加同名实例时可继续使用。', { id }))) return;
    change('bots', Object.fromEntries(Object.entries(bots).filter(([key]) => key !== id)));
    const nextSecrets = { ...secretInputs };
    delete nextSecrets[tokenPath(id)];
    setSecretInputs(nextSecrets);
  };

  return <div className="telegram-settings">
    <section className="telegram-overview">
      <div><RadioTower size={23} /><span><small>{t('Telegram Bot 信道')}</small><b>{entries.length ? t('{{count}} 个 Bot 实例', { count: entries.length }) : t('尚未配置 Bot')}</b><p>{t('每个实例独立保存 Token、长轮询 offset 与已知 chat；同一 chat 可通过多个实例接入。')}</p></span></div>
      <em>{t('保存后立即重配')}</em>
    </section>

    <section className="telegram-add">
      <div><b>{t('添加 Bot 实例')}</b><small>{t('已生成可编辑的 4 位 instance_id 和默认名称；也可以自定义。')}</small></div>
      <input className="admin-input" value={instanceId} onChange={event => setInstanceId(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addBot(); } }} aria-label={t('Telegram instance_id')} />
      <button className="ghost" disabled={!canAdd} onClick={addBot}><Plus size={14} />{t('添加实例')}</button>
      {normalizedId && !TELEGRAM_INSTANCE_ID_PATTERN.test(normalizedId) && <small className="field-error">{t('使用 1–32 位小写字母、数字、下划线或连字符，并以字母开头。')}</small>}
      {normalizedId && TELEGRAM_INSTANCE_ID_PATTERN.test(normalizedId) && bots[normalizedId] && <small className="field-error">{t('这个 instance_id 已经存在，请换一个。')}</small>}
    </section>

    {entries.length ? <div className="telegram-bot-list">{entries.map(([id, bot]) => {
      const secretPath = tokenPath(id);
      const status = secretStatus[secretPath];
      return <article key={id}>
        <header><div className="telegram-bot-mark"><Bot size={19} /></div><span><b>{bot.display_name || id}</b><code>tg:{id}:{'{chat_id}'}</code></span><label className="switch"><input type="checkbox" checked={bot.enabled !== false} onChange={event => updateBot(id, { enabled: event.target.checked })} /><i /><span>{t('启用')}</span></label><button className="danger-icon" title={t('移除 Bot 实例')} onClick={() => removeBot(id)}><Trash2 size={15} /></button></header>
        <div className="telegram-bot-fields">
          <label><span>{t('显示名称')}</span><input className="admin-input" value={bot.display_name || ''} maxLength={80} onChange={event => updateBot(id, { display_name: event.target.value })} placeholder={t('例如 工作群机器人')} /></label>
          <label><span>{t('Bot Token')}</span><input className="admin-input" type="password" value={secretInputs[secretPath] || ''} onChange={event => setSecretInputs({ ...secretInputs, [secretPath]: event.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' }) : t('从 BotFather 获取 Token')} /><small>{status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}</small></label>
          <label><span>{t('机器人 API 地址')}</span><input className="admin-input" value={bot.api_base_url || ''} onChange={event => updateBot(id, { api_base_url: event.target.value })} /></label>
          <label><span>{t('长轮询超时（秒）')}</span><input className="admin-input" type="number" min="1" max="50" step="1" value={bot.poll_timeout_seconds ?? 30} onChange={event => updateBot(id, { poll_timeout_seconds: Number(event.target.value) })} /></label>
          <label className="switch config-switch telegram-local-mode"><input type="checkbox" checked={!!bot.local_mode} onChange={event => updateBot(id, { local_mode: event.target.checked })} /><i /><span><b>{t('自托管机器人 API 服务器')}</b><small>{t('仅当服务以 --local 启动并与 Coworker 共享文件路径时开启；官方 API 或普通代理保持关闭。')}</small></span></label>
        </div>
      </article>;
    })}</div> : <div className="provider-empty">{t('还没有 Telegram Bot。添加实例后填写 BotFather 提供的 Token。')}</div>}

    <div className="telegram-note"><b>{t('群聊接收范围')}</b><span>{t('Bot 默认可能只收到命令、回复和提及；如需读取群内普通消息，请在 BotFather 中关闭该 Bot 的 Privacy Mode。已配置 webhook 的 Bot 不能同时使用长轮询。')}</span></div>
  </div>;
}
