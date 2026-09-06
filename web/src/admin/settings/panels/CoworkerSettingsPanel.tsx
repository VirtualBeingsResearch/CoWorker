import { Handshake, Plus, RadioTower, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';

import { t } from '../../../i18n/admin';
import {
  COWORKER_PEER_ID_PATTERN,
  COWORKER_RESERVED_PEER_IDS,
} from '../coworkerPeerId';
import type { Json, SettingsPanelProps } from '../types';

function peerTokenPath(peerId: string) {
  return `coworker.peers.${peerId}.token`;
}

export function CoworkerSettingsPanel({
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
  runtime,
}: SettingsPanelProps) {
  const peers = (value.peers || {}) as Record<string, Json>;
  const entries = useMemo(() => Object.entries(peers), [peers]);
  const [peerId, setPeerId] = useState('');
  const normalizedId = peerId.trim();
  const inboundPath = 'coworker.inbound_token';
  const inboundStatus = secretStatus[inboundPath];
  const effectiveSelfId = typeof runtime?.coworker_self_id === 'string' ? runtime.coworker_self_id : '';
  // 配置值与生效值一致时不重复展示；生效值仅在进程运行后存在。
  const showEffectiveSelfId = Boolean(effectiveSelfId) && effectiveSelfId !== String(value.self_id || '').trim();
  const canAdd = (
    COWORKER_PEER_ID_PATTERN.test(normalizedId)
    && !COWORKER_RESERVED_PEER_IDS.has(normalizedId)
    && !peers[normalizedId]
  );

  const updatePeer = (id: string, patch: Json) => {
    change('peers', { ...peers, [id]: { ...peers[id], ...patch } });
  };
  const addPeer = () => {
    if (!canAdd) return;
    change('peers', {
      ...peers,
      [normalizedId]: {
        display_name: '',
        base_url: '',
        token: '',
      },
    });
    setPeerId('');
  };
  const removePeer = (id: string) => {
    if (!confirm(t('移除搭档对端“{{id}}”？对方仍可通过入站宣告被重新学习。', { id }))) return;
    change('peers', Object.fromEntries(Object.entries(peers).filter(([key]) => key !== id)));
    const nextSecrets = { ...secretInputs };
    delete nextSecrets[peerTokenPath(id)];
    setSecretInputs(nextSecrets);
  };

  return <div className="telegram-settings">
    <section className="telegram-overview">
      <div><RadioTower size={23} /><span><small>{t('Coworker 搭档信道')}</small><b>{entries.length ? t('{{count}} 个显式对端', { count: entries.length }) : t('尚未配置对端')}</b><p>{t('只需一侧填写对端 API 地址与通信令牌；首次发信后对端会学习本实例，即可双向回复。')}</p></span></div>
      <em>{t('保存后立即重配')}</em>
    </section>

    <section className="coworker-identity">
      <div><b>{t('本实例身份')}</b><small>{t('对端配置本实例时需要 self_id；带令牌的 GET /status 也会返回 coworker_self_id。')}</small></div>
      <div className="telegram-bot-fields">
        <label><span>{t('本实例 self_id')}</span><input className="admin-input" value={value.self_id || ''} onChange={event => change('self_id', event.target.value)} placeholder={t('留空则首次启动自动生成')} /><small>{t('使用 1–32 位小写字母、数字、下划线或连字符，并以字母开头。control 为保留名。')}</small>{showEffectiveSelfId && <small className="coworker-effective-self-id"><code>{effectiveSelfId}</code>{t('为当前生效的 self_id，对端配置时填写这个。')}</small>}</label>
        <label><span>{t('回呼地址')}</span><input className="admin-input" value={value.self_base_url || ''} onChange={event => change('self_base_url', event.target.value)} placeholder="http://127.0.0.1:8000" /><small>{t('对端回呼本实例的地址；留空则回退 API 公开地址或本机端口。')}</small></label>
        <label><span>{t('搭档入站令牌')}</span><input className="admin-input" type="password" value={secretInputs[inboundPath] || ''} onChange={event => setSecretInputs({ ...secretInputs, [inboundPath]: event.target.value })} placeholder={inboundStatus?.configured ? t('••••••••{{last4}}（留空保留）', { last4: inboundStatus.last4 || '' }) : t('建议设置专用令牌，避免把主通信令牌交给对端')} /><small>{inboundStatus?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: inboundStatus.last4 || '' }) : t('当前未配置')}</small></label>
        <label><span>{t('附件总大小上限（字节）')}</span><input className="admin-input" type="number" min="1" step="1" value={value.max_attachment_bytes ?? 10485760} onChange={event => change('max_attachment_bytes', Number(event.target.value))} /></label>
      </div>
    </section>

    <section className="telegram-add">
      <div><b>{t('添加对端实例')}</b><small>{t('填写对端的 self_id（不是本实例）。可从对方管理端或 GET /status 的 coworker_self_id 复制。')}</small></div>
      <input className="admin-input" value={peerId} onChange={event => setPeerId(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addPeer(); } }} aria-label={t('对端 self_id')} placeholder="ava" />
      <button className="ghost" disabled={!canAdd} onClick={addPeer}><Plus size={14} />{t('添加对端')}</button>
      {normalizedId && !COWORKER_PEER_ID_PATTERN.test(normalizedId) && <small className="field-error">{t('使用 1–32 位小写字母、数字、下划线或连字符，并以字母开头。')}</small>}
      {normalizedId && COWORKER_RESERVED_PEER_IDS.has(normalizedId) && <small className="field-error">{t('control 是连接控制地址，不能当作对端 self_id。')}</small>}
      {normalizedId && COWORKER_PEER_ID_PATTERN.test(normalizedId) && peers[normalizedId] && <small className="field-error">{t('这个 self_id 已经存在，请换一个。')}</small>}
    </section>

    {entries.length ? <div className="telegram-bot-list">{entries.map(([id, peer]) => {
      const secretPath = peerTokenPath(id);
      const status = secretStatus[secretPath];
      return <article key={id}>
        <header><div className="telegram-bot-mark"><Handshake size={19} /></div><span><b>{peer.display_name || id}</b><code>coworker:{id}</code></span><span /><button className="danger-icon" title={t('移除对端')} onClick={() => removePeer(id)}><Trash2 size={15} /></button></header>
        <div className="telegram-bot-fields">
          <label><span>{t('显示名称')}</span><input className="admin-input" value={peer.display_name || ''} maxLength={80} onChange={event => updatePeer(id, { display_name: event.target.value })} placeholder={t('例如 研究组 Bob')} /></label>
          <label><span>{t('对端 API 地址')}</span><input className="admin-input" value={peer.base_url || ''} onChange={event => updatePeer(id, { base_url: event.target.value })} placeholder="http://127.0.0.1:8001" /><small>{t('直连填对端 API 根地址；经 Relay 则填 /i/{instance_id} 实例 URL。')}</small></label>
          <label><span>{t('对端通信令牌')}</span><input className="admin-input" type="password" value={secretInputs[secretPath] || ''} onChange={event => setSecretInputs({ ...secretInputs, [secretPath]: event.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' }) : t('对端的搭档入站令牌或主通信令牌')} /><small>{status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}</small></label>
        </div>
      </article>;
    })}</div> : <div className="provider-empty">{t('还没有显式对端。添加对端后填写 API 地址与令牌，或让搭档用 coworker:control 连接。')}</div>}

    <div className="telegram-note"><b>{t('搭档如何主动连接')}</b><span>{t('已启用的搭档可以用 communicate(participant_id="coworker:control", extra={"action":"connect","peer_id":"对方self_id","base_url":"...","token":"..."}) 把对端写入学习库，随后向 coworker:{self_id} 发信。管理端这里保存的是显式配置，优先于学习记录。')}</span></div>
  </div>;
}
