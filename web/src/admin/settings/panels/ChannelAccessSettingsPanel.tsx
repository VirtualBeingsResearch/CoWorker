import { ArrowDownToLine, ArrowUpFromLine, Plus, ShieldCheck, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { t } from '../../../i18n/admin';
import type { Json, SettingsPanelProps } from '../types';

type RuleField =
  | 'inbound_allow'
  | 'inbound_deny'
  | 'outbound_allow'
  | 'outbound_deny';

const BUILTIN_CHANNELS = ['stream', 'desktop', 'wecom', 'weixin'];
const EMPTY_RULES: Record<RuleField, string[]> = {
  inbound_allow: [],
  inbound_deny: [],
  outbound_allow: [],
  outbound_deny: [],
};

function rulesFor(value: Json, channel: string): Record<RuleField, string[]> {
  const raw = value[channel] || {};
  return {
    inbound_allow: Array.isArray(raw.inbound_allow) ? raw.inbound_allow : [],
    inbound_deny: Array.isArray(raw.inbound_deny) ? raw.inbound_deny : [],
    outbound_allow: Array.isArray(raw.outbound_allow) ? raw.outbound_allow : [],
    outbound_deny: Array.isArray(raw.outbound_deny) ? raw.outbound_deny : [],
  };
}

function PatternList({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint: string;
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const [candidate, setCandidate] = useState('');
  const add = () => {
    const pattern = candidate.trim();
    if (!pattern || value.includes(pattern)) return;
    onChange([...value, pattern]);
    setCandidate('');
  };
  return <div className="field string-list-field channel-access-list">
    <span>{t(label)}</span>
    <div className="string-list-editor">
      {value.length ? <div className="string-list-items">{value.map((pattern, index) => <div className="string-list-item" key={`${pattern}:${index}`}>
        <code>{pattern}</code>
        <button type="button" onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))} title={t('删除匹配规则')} aria-label={t('删除匹配规则 {{item}}', { item: pattern })}><X size={13} /></button>
      </div>)}</div> : <div className="string-list-empty">{t('未配置规则')}</div>}
      <div className="string-list-add">
        <input aria-label={t(label)} value={candidate} onChange={event => setCandidate(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); add(); } }} placeholder="wecom:single:*" />
        <button type="button" className="ghost mini" disabled={!candidate.trim() || value.includes(candidate.trim())} onClick={add}><Plus size={14} />{t('添加规则')}</button>
      </div>
    </div>
    <small>{t(hint)}</small>
  </div>;
}

export function ChannelAccessSettingsPanel({ value, change }: SettingsPanelProps) {
  const channelNames = useMemo(
    () => [...new Set([...BUILTIN_CHANNELS, ...Object.keys(value)])],
    [value],
  );
  const [selected, setSelected] = useState(channelNames[0] || 'stream');
  const [newChannel, setNewChannel] = useState('');

  useEffect(() => {
    if (!channelNames.includes(selected)) setSelected(channelNames[0] || 'stream');
  }, [channelNames, selected]);

  const rules = rulesFor(value, selected);
  const ruleCount = Object.values(rules).reduce((count, items) => count + items.length, 0);
  const update = (field: RuleField, patterns: string[]) => {
    change(selected, { ...rules, [field]: patterns });
  };
  const addChannel = () => {
    const name = newChannel.trim();
    if (!name || channelNames.includes(name)) return;
    change(name, structuredClone(EMPTY_RULES));
    setSelected(name);
    setNewChannel('');
  };

  return <div className="channel-access-settings">
    <section className="channel-access-overview">
      <ShieldCheck size={24} />
      <div><span>{t('信道访问策略')}</span><h3>{t('按通信地址控制入站与出站')}</h3><p>{t('规则匹配完整 canonical participant_id；拒绝列表优先，允许列表为空时不限制。这里只控制通信地址，不识别真实人员，也不定义谁能唤醒 Agent。')}</p></div>
      <em>{t('保存后立即生效')}</em>
    </section>
    <div className="channel-access-layout">
      <aside className="channel-access-nav">
        {channelNames.map(channel => {
          const count = Object.values(rulesFor(value, channel)).reduce((total, items) => total + items.length, 0);
          return <button type="button" className={channel === selected ? 'active' : ''} onClick={() => setSelected(channel)} key={channel}><span><b>{channel}</b><small>{count ? t('{{count}} 条规则', { count }) : t('未限制')}</small></span><i>{count || '—'}</i></button>;
        })}
        <div className="channel-access-add"><input aria-label={t('扩展信道名称')} value={newChannel} onChange={event => setNewChannel(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addChannel(); } }} placeholder={t('扩展信道名称')} /><button type="button" className="ghost mini" disabled={!newChannel.trim() || channelNames.includes(newChannel.trim())} onClick={addChannel}><Plus size={13} />{t('添加')}</button></div>
      </aside>
      <section className="channel-access-detail">
        <header><div><span>{t('当前信道')}</span><h3>{selected}</h3><p>{ruleCount ? t('已配置 {{count}} 条访问规则', { count: ruleCount }) : t('当前保持默认允许行为')}</p></div>{ruleCount > 0 && <button type="button" className="ghost mini" onClick={() => change(selected, structuredClone(EMPTY_RULES))}>{t('清空本信道规则')}</button>}</header>
        <div className="channel-access-direction">
          <div className="channel-access-direction-head"><ArrowDownToLine size={17} /><span><b>{t('入站')}</b><small>{t('控制哪些 participant 消息可以进入 Coworker。')}</small></span></div>
          <div className="channel-access-grid">
            <PatternList label="入站允许列表" hint="非空时，仅接受匹配项；空列表表示不启用允许限制。" value={rules.inbound_allow} onChange={next => update('inbound_allow', next)} />
            <PatternList label="入站拒绝列表" hint="匹配项始终拒绝，即使同时命中允许列表。" value={rules.inbound_deny} onChange={next => update('inbound_deny', next)} />
          </div>
        </div>
        <div className="channel-access-direction">
          <div className="channel-access-direction-head"><ArrowUpFromLine size={17} /><span><b>{t('出站')}</b><small>{t('控制 Agent 可以发现并发送到哪些 participant。')}</small></span></div>
          <div className="channel-access-grid">
            <PatternList label="出站允许列表" hint="非空时，仅可向匹配项发送；空列表表示不启用允许限制。" value={rules.outbound_allow} onChange={next => update('outbound_allow', next)} />
            <PatternList label="出站拒绝列表" hint="匹配项不会出现在模型连接列表中，也不能直接发送。" value={rules.outbound_deny} onChange={next => update('outbound_deny', next)} />
          </div>
        </div>
      </section>
    </div>
  </div>;
}
