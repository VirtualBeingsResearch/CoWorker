import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlarmClock, ArchiveRestore, Bot, Brain, ChevronLeft, ChevronRight, CircleGauge,
  Check, Clock3, CloudUpload, Database, Download, FileArchive, FileCode2, FileCog, FileText, Fingerprint, FolderOpen, HeartPulse, KeyRound, ListTodo, LogOut,
  MessagesSquare, Orbit, RefreshCw, Save, Search, Settings2, ShieldCheck, SlidersHorizontal,
  Sparkles, TerminalSquare, Trash2, TriangleAlert, Wrench, X, Pencil, Plus, PackageOpen, Rocket, RotateCcw,
} from 'lucide-react';
import './admin.css';
import { AdminLanguageSwitch, t, useAdminI18n } from './i18n/admin';

type Json = Record<string, any>;
type Section = 'overview' | 'memory' | 'models' | 'settings' | 'runtime' | 'identity' | 'content' | 'releases' | 'audit';
type NavGroup = '观察' | '塑形' | '扩展' | '追溯';
type LifeState = 'live' | 'resting' | 'quiet';

const NAV: Array<{ id: Section; label: string; description: string; group: NavGroup; icon: typeof Activity }> = [
  { id: 'overview', label: '生命总览', description: '状态、上下文和当前驻留情况', group: '观察', icon: HeartPulse },
  { id: 'memory', label: '记忆中心', description: '短期上下文、长期召回与并行思考记录', group: '观察', icon: Database },
  { id: 'runtime', label: '运行中心', description: '任务、闹钟、运行账本与维护', group: '观察', icon: Activity },
  { id: 'models', label: '模型编排', description: '主线模型、摘要与失败降级链', group: '塑形', icon: Brain },
  { id: 'settings', label: '运行设置', description: '连接、记忆与循环参数', group: '塑形', icon: Settings2 },
  { id: 'identity', label: '身份档案', description: '姓名、人格、目标和生命经历', group: '塑形', icon: Fingerprint },
  { id: 'content', label: '能力内容', description: 'Skill、Palace 与潜意识模式', group: '扩展', icon: FileCog },
  { id: 'releases', label: '桌面发布', description: '版本、签名产物与更新投放', group: '扩展', icon: PackageOpen },
  { id: 'audit', label: '诊断与审计', description: '事件循环健康与管理员操作记录', group: '追溯', icon: ShieldCheck },
];
const NAV_GROUPS: NavGroup[] = ['观察', '塑形', '扩展', '追溯'];

function sectionFromLocation(): Section {
  const requested = new URLSearchParams(window.location.search).get('section');
  return NAV.some(item => item.id === requested) ? requested as Section : 'overview';
}

function storedToken() { return sessionStorage.getItem('coworker-admin-token') || ''; }

class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function api<T = Json>(path: string, init: RequestInit = {}): Promise<T> {
  const isForm = typeof FormData !== 'undefined' && init.body instanceof FormData;
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init.body && !isForm ? { 'Content-Type': 'application/json' } : {}),
      Authorization: `Bearer ${storedToken()}`,
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || t('请求失败 {{status}}', { status: response.status })), response.status);
  }
  return response.status === 204 ? ({} as T) : response.json();
}

function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    setLoading(true); setError('');
    try { setData(await loader()); } catch (e) { setError(e instanceof Error ? e.message : t('加载失败')); }
    finally { setLoading(false); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { void reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

function Login({ onReady }: { onReady: (name: string) => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    sessionStorage.setItem('coworker-admin-token', token);
    try {
      const result = await api<{ name: string }>('/api/admin/session/verify', { method: 'POST' });
      onReady(result.name || '');
    } catch (e) {
      sessionStorage.removeItem('coworker-admin-token');
      setError(e instanceof Error ? e.message : t('验证失败'));
    } finally { setBusy(false); }
  };
  return <main className="admin-login">
    <AdminLanguageSwitch className="admin-language-toggle-floating" />
    <section className="login-card">
      <div className="login-presence">
        <div className="login-sigil"><Orbit size={34} /><i /><i /><i /></div>
        <div>
          <p className="eyebrow">{t('本地控制台')}</p>
          <h1>{t('进入照看室')}</h1>
          <p className="login-copy">{t('查看生命迹象，调整她的运行方式，并谨慎触碰记忆。')}</p>
        </div>
        <div className="login-life-trace" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
        <div className="login-assurance"><span><i />{t('本地值守')}</span><span>{t('令牌仅保留在当前会话')}</span></div>
      </div>
      <div className="login-access">
        <p className="access-step">{t('访问步骤 01')}</p>
        <div><h2>{t('确认照看权限')}</h2><p>{t('使用管理员令牌开启这次值守会话。')}</p></div>
        <form onSubmit={submit}>
          <label><span>{t('管理员令牌')}</span><div className="token-input"><KeyRound size={17} /><input autoFocus type="password" value={token} onChange={e => setToken(e.target.value)} placeholder={t('输入 ADMIN__TOKEN')} autoComplete="current-password" /></div></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary" disabled={!token || busy}>{busy ? t('正在确认…') : t('进入值守台')}<ChevronRight size={16} /></button>
        </form>
        <a href="/">{t('返回生命体主页')} <ChevronRight size={14} /></a>
      </div>
    </section>
  </main>;
}

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic', openai: 'OpenAI / 兼容服务', deepseek: 'DeepSeek',
  qwen: '通义千问', zhipu: '智谱 GLM', minimax: 'MiniMax',
};
const PROVIDER_DEFAULT_MODELS: Record<string, string> = {
  anthropic: 'claude-sonnet-4-8', openai: 'gpt-5.5', deepseek: 'deepseek-v4-pro',
  qwen: 'qwen3.7-plus', zhipu: 'glm-5.2', minimax: 'MiniMax-M3',
};

function preferredModelFor(providerType: string, models: string[]) {
  const preferred = PROVIDER_DEFAULT_MODELS[providerType];
  return preferred && models.includes(preferred) ? preferred : models[0] || '';
}

function FirstRun({ data, onComplete }: { data: Json; onComplete: () => void }) {
  const catalogs = data.providers || [];
  const initialType = catalogs.some((item: Json) => item.type === 'deepseek') ? 'deepseek' : catalogs[0]?.type || 'openai';
  const [providerType, setProviderType] = useState(initialType);
  const models = catalogs.find((item: Json) => item.type === providerType)?.models || [];
  const preferredModel = preferredModelFor(providerType, models);
  const [model, setModel] = useState(preferredModel);
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [phase, setPhase] = useState<'form' | 'restarting'>('form');

  useEffect(() => {
    const nextModels = catalogs.find((item: Json) => item.type === providerType)?.models || [];
    setModel(preferredModelFor(providerType, nextModels));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerType]);

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError('');
    try {
      await api('/api/admin/bootstrap', { method: 'POST', body: JSON.stringify({ provider_type: providerType, model, api_key: apiKey, base_url: baseUrl, coworker_name: name }) });
      setPhase('restarting');
      const deadline = Date.now() + 90_000;
      const waitUntilReady = async () => {
        while (Date.now() < deadline) {
          await new Promise(resolve => window.setTimeout(resolve, 1500));
          try {
            const status = await api<Json>('/api/admin/bootstrap');
            if (!status.required) { onComplete(); return; }
          } catch { /* Restart temporarily closes the connection. */ }
        }
        setError(t('配置已经保存，但服务仍在重启。请稍后刷新页面。'));
      };
      void waitUntilReady();
    } catch (e) { setError(e instanceof Error ? e.message : t('初始化失败')); }
  };

  return <main className="admin-login admin-bootstrap">
    <AdminLanguageSwitch className="admin-language-toggle-floating" />
    <section className="bootstrap-card">
      <aside className="bootstrap-rail">
        <div className="login-sigil"><Orbit size={32} /><i /><i /><i /></div>
        <p className="eyebrow">{t('初始设置')}</p>
        <h1>{t('接通她的')}<br />{t('第一束信号')}</h1>
        <p>{t('管理员入口已经准备好。再连接一个模型服务，Coworker 就能开始工作。')}</p>
        <ol className="awakening-circuit">
          <li className="done"><span><KeyRound size={16} /></span><div><b>{t('访问凭证')}</b><small>{t('已安全生成并保存')}</small></div></li>
          <li className={phase === 'form' ? 'active' : 'done'}><span><Brain size={16} /></span><div><b>{t('模型连接')}</b><small>{phase === 'form' ? t('等待填写') : t('配置已写入')}</small></div></li>
          <li className={phase === 'restarting' ? 'active' : ''}><span><RefreshCw size={16} /></span><div><b>{t('唤醒运行')}</b><small>{phase === 'restarting' ? t('正在安全重启') : t('完成后自动进行')}</small></div></li>
        </ol>
      </aside>
      <section className="bootstrap-form-stage">
        {phase === 'restarting' ? <div className="bootstrap-restarting" role="status"><div className="restart-orbit"><Orbit size={34} /><i /><i /></div><p className="access-step">{t('设置步骤 03')}</p><h2>{t('正在带着新配置醒来')}</h2><p>{t('页面会在服务恢复后自动进入照看室，不需要重复填写。')}</p>{error && <p className="form-error" role="alert">{error}</p>}</div> : <>
          <div className="bootstrap-heading"><p className="access-step">{t('设置步骤 02')}</p><h2>{t('配置第一个模型连接')}</h2><p>{t('这些值会写入本地管理配置，不需要创建')} <code>.env</code>{t('。')}</p></div>
          <form className="bootstrap-form" onSubmit={submit}>
            <div className="bootstrap-grid">
              <label><span>{t('服务类型')}</span><select value={providerType} onChange={e => setProviderType(e.target.value)}>{catalogs.map((item: Json) => <option value={item.type} key={item.type}>{t(PROVIDER_LABELS[item.type] || item.type)}</option>)}</select></label>
              <label><span>{t('启动模型')}</span><select value={model} onChange={e => setModel(e.target.value)}>{models.map((item: string) => <option value={item} key={item}>{item}</option>)}</select></label>
              <label className="wide"><span>API Key</span><input autoFocus required type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={t('只会保存到本机配置')} autoComplete="new-password" /></label>
              <label className="wide"><span>{t('自定义 Base URL')} <em>{t('可选')}</em></span><input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder={t('使用官方地址时留空')} /></label>
              <label className="wide"><span>{t('给 Coworker 起个名字')} <em>{t('可选')}</em></span><input value={name} onChange={e => setName(e.target.value)} placeholder={t('之后也可以在身份档案中修改')} /></label>
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary" disabled={!apiKey.trim() || !model}>{t('保存并唤醒')} <ChevronRight size={16} /></button>
          </form>
          <p className="bootstrap-footnote"><ShieldCheck size={13} />{t('配置保存在')} <code>data/admin_config.json</code>{t('，API Key 不会回显到页面。')}</p>
        </>}
      </section>
    </section>
  </main>;
}

function Panel({ title, note, action, children, className = '' }: { title: string; note?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`admin-panel ${className}`}>
    <header><div><h2>{t(title)}</h2>{note && <p>{t(note)}</p>}</div>{action}</header>
    {children}
  </section>;
}

function Loading({ error }: { error?: string }) {
  return <div className={error ? 'state-box error' : 'state-box'} role={error ? 'alert' : 'status'}>{!error && <span className="state-pulse" aria-hidden="true"><i /><i /><i /></span>}<span>{t(error || '正在读取生命迹象…')}</span></div>;
}

function Overview({ name }: { name: string }) {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/overview'), []);
  if (loading || !data) return <Loading error={error} />;
  const status = data.status; const counts = data.counts;
  const running = status.is_running;
  const resting = running && Boolean(status.is_sleeping);
  const presenceState = running ? (resting ? 'resting' : 'running') : 'quiet';
  const presenceLabel = t(running ? (resting ? '休息中' : '正在运行') : '未运行');
  const sampledAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return <div className="page-stack">
    <section className={`presence-hero ${presenceState}`}>
      <div className="presence-copy">
        <p className="eyebrow">{t('状态采样')}</p>
        <h1>{name || 'Coworker'}<span className={`live-badge ${presenceState}`}>{presenceLabel}</span></h1>
        <div className="presence-readout">
          <div><span>{t('主线模型')}</span><strong>{status.provider}/{status.model}</strong></div>
          <div><span>{t('生命循环')}</span><strong>{t('第 {{count}} 次', { count: status.cycle_count || 0 })}</strong></div>
          <div><span>{t('本次采样')}</span><strong>{sampledAt}</strong></div>
        </div>
      </div>
      <div className="pulse-organ" aria-label={presenceLabel}>
        <span className="organ-line" aria-hidden="true" />
        {[22, 42, 66, 92, 56, 38, 74, 48, 26].map((h, i) => <i key={i} style={{ '--h': `${h}%`, '--d': `${i * .08}s` } as React.CSSProperties} />)}
        <small>{t('生命信号')} · {presenceLabel}</small>
      </div>
      <button className="icon-btn" onClick={() => void reload()} title={t('刷新生命迹象')} aria-label={t('刷新生命迹象')}><RefreshCw size={16} /></button>
    </section>
    {data.pending_restart && <div className="notice amber"><TriangleAlert size={17} /><span>{t('有配置等待重启后生效。')}</span></div>}
    <div className="vital-grid">
      {[
        [t('活跃任务'), counts.active_tasks, t('{{count}} 项总计', { count: counts.tasks }), ListTodo],
        [t('运行 Bubble'), counts.active_bubbles, t('并行思考分支'), Orbit],
        [t('长期记忆'), counts.long_term_memories, t('可语义检索'), Database],
        [t('短期上下文'), counts.short_term_messages, t('{{count}} 个树节点', { count: data.memory.tree_nodes }), MessagesSquare],
        [t('待触发闹钟'), counts.alarms, t('后台守候中'), AlarmClock],
      ].map(([label, value, note, Icon]: any) => <article className="vital" key={label}><Icon size={18} /><span>{t(label)}</span><strong>{Number(value).toLocaleString()}</strong><small>{t(note)}</small></article>)}
    </div>
    <div className="two-col">
      <Panel title="上下文水位" note="短期消息与记忆树的当前结构">
        <div className="memory-meter"><div><span>{t('消息')}</span><b>{data.memory.messages}</b></div><div><span>{t('容量')}</span><b>{Number(data.memory.max_tokens).toLocaleString()} token</b></div><div><span>{t('回溯')}</span><b>{data.memory.backfill?.running ? `${data.memory.backfill.done}/${data.memory.backfill.total}` : t('空闲')}</b></div></div>
      </Panel>
      <Panel title="进程驻留" note="当前实例的连续运行时间">
        <div className="runtime-clock"><CircleGauge size={34} /><div><b>{new Date(status.started_at).toLocaleString()}</b><span>{t('本轮启动时间')}</span></div></div>
      </Panel>
    </div>
  </div>;
}

function Models() {
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>('/api/admin/model'), []);
  const [switchTo, setSwitchTo] = useState({ provider: '', model_id: '' });
  const [draft, setDraft] = useState<Json | null>(null);
  useEffect(() => { if (data) { setDraft(JSON.parse(JSON.stringify(data))); setSwitchTo({ provider: data.active.provider || '', model_id: data.active.model || '' }); } }, [data]);
  const save = async () => {
    if (!draft) return;
    const next = await api<Json>('/api/admin/model', { method: 'PATCH', body: JSON.stringify({ summary: draft.summary, fallbacks: draft.fallbacks, vision: draft.vision }) });
    setData(next); setDraft(next);
  };
  const switchModel = async () => {
    const next = await api<Json>('/api/admin/model/switch', { method: 'POST', body: JSON.stringify(switchTo) });
    setData(next); setDraft(next); setSwitchTo({ provider: '', model_id: '' });
  };
  if (loading || !draft) return <Loading error={error} />;
  const set = (path: string, value: any) => setDraft((old: Json) => { const n = structuredClone(old); const [a, b] = path.split('.'); n[a][b] = value; return n; });
  return <div className="page-stack">
    <Panel title="主线模型" note="切换立即生效，正在执行的单次调用不会被中断。">
      <div className="active-model"><Bot size={28} /><div><span>{t('当前接棒者')}</span><strong>{draft.active.provider}/{draft.active.model}</strong></div></div>
      <div className="inline-form"><select value={switchTo.provider} onChange={e => setSwitchTo({ ...switchTo, provider: e.target.value })}><option value="">{t('选择 Provider')}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select><input value={switchTo.model_id} onChange={e => setSwitchTo({ ...switchTo, model_id: e.target.value })} placeholder={t('模型 ID（留空使用默认）')} /><button className="primary" disabled={!switchTo.provider} onClick={() => void switchModel()}>{t('切换模型')}</button></div>
    </Panel>
    <div className="two-col">
      <Panel title="摘要与压缩" note="控制上下文压缩时使用的模型。">
        <div className="field-grid"><Field label="Provider" hint="留空时跟随主线模型"><select value={draft.summary.provider} onChange={e => set('summary.provider', e.target.value)}><option value="">{t('跟随主线（{{provider}}）', { provider: draft.active.provider })}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select></Field><Field label="模型" hint="留空时跟随主线模型"><input value={draft.summary.model} onChange={e => set('summary.model', e.target.value)} placeholder={draft.active.model} /></Field><label className="switch"><input type="checkbox" checked={draft.summary.thinking} onChange={e => set('summary.thinking', e.target.checked)} /><i /><span>{t('启用 Thinking')}</span></label></div>
      </Panel>
      <Panel title="视觉理解" note="为纯文本主模型提供图片分析能力。">
        <div className="field-grid"><Field label="Provider"><select value={draft.vision.provider} onChange={e => set('vision.provider', e.target.value)}><option value="">{t('关闭')}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select></Field><Field label="模型"><input value={draft.vision.model} onChange={e => set('vision.model', e.target.value)} /></Field><label className="switch"><input type="checkbox" checked={draft.vision.thinking} onChange={e => set('vision.thinking', e.target.checked)} /><i /><span>{t('启用 Thinking')}</span></label></div>
      </Panel>
    </div>
    <Panel title="失败降级链" note="每行填写 provider 或 provider/model，按从上到下的顺序接棒。">
      <textarea className="code-area short" value={(draft.fallbacks || []).join('\n')} onChange={e => setDraft({ ...draft, fallbacks: e.target.value.split('\n').map(x => x.trim()).filter(Boolean) })} />
      <div className="panel-actions"><button className="primary" onClick={() => void save()}><Save size={15} />{t('保存并热更新')}</button><button className="ghost" onClick={() => void reload()}>{t('放弃修改')}</button></div>
    </Panel>
  </div>;
}

function Field({ label, children, hint, hot = false }: { label: string; children: ReactNode; hint?: string; hot?: boolean }) { return <label className="field"><span>{t(label)}{hot && <em className="effect-badge hot">{t('立即生效')}</em>}</span>{children}{hint && <small>{t(hint)}</small>}</label>; }

const GROUP_LABELS: Record<string, string> = { llm: '模型与 Provider', i18n: '运行时语言', memory: '记忆系统', agent: 'Agent 循环', api: 'API 服务', wecom: '企业微信', desktop_updates: '桌面更新', admin: '管理端' };
const HIDDEN_CONFIG = new Set(['admin.token', 'desktop_updates.admin_token']);
const LLM_MODEL_ORCHESTRATION_FIELDS = new Set(['summary_provider', 'summary_model', 'summary_thinking', 'fallbacks', 'vision_provider', 'vision_model', 'vision_thinking']);
const CONFIG_LABELS: Record<string, string> = {
  'llm.default_provider': '启动时使用的 Provider',
  'llm.default_model': '启动时使用的模型',
  'llm.max_tokens': '单次输出上限',
  'i18n.locale': '模型与运行时语言',
};

function Settings() {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/config'), []);
  const [draft, setDraft] = useState<Json | null>(null);
  const [group, setGroup] = useState('llm');
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');
  useEffect(() => { if (data) setDraft(structuredClone(data.config)); }, [data]);
  if (loading || !data || !draft) return <Loading error={error} />;
  const effectiveProviders = data.effective_providers || [];
  const externalProviders = effectiveProviders.filter((provider: Json) => !provider.managed);
  const groups = Object.keys(draft).filter(k => GROUP_LABELS[k]);
  const change = (key: string, value: any) => setDraft({ ...draft, [group]: { ...draft[group], [key]: value } });
  const changeProvider = (index: number, key: string, value: string) => {
    const providers = [...(draft.llm.managed_providers || [])];
    providers[index] = { ...providers[index], [key]: value };
    setDraft({ ...draft, llm: { ...draft.llm, managed_providers: providers } });
  };
  const save = async () => {
    const secrets = Object.fromEntries(Object.entries(secretInputs).filter(([, v]) => v !== ''));
    const result = await api<Json>('/api/admin/config', { method: 'PATCH', body: JSON.stringify({ changes: { [group]: draft[group] }, secrets }) });
    const hotCount = result.applied_now?.length || 0; const restartCount = result.requires_restart?.length || 0;
    const savedMessage = hotCount && restartCount
      ? t('已保存：{{hot}} 项立即生效，{{restart}} 项等待重启。', { hot: hotCount, restart: restartCount })
      : hotCount
        ? t('已保存，{{count}} 项修改已立即生效。', { count: hotCount })
        : restartCount
          ? t('已保存，{{count}} 项修改将在安全重启后生效。', { count: restartCount })
          : t('配置没有变化。');
    setSecretInputs({}); setMessage(savedMessage); await reload();
  };
  const isHot = (path: string) => (data.hot_reloadable || []).some((item: string) => path === item || path.startsWith(`${item}.`));
  const adminToken = data.secret_status['admin.token'];
  const fallbackToken = data.secret_status['desktop_updates.admin_token'];
  const activeAdminToken = adminToken?.configured ? adminToken : fallbackToken;
  const providerSource = data.sources?.providers ? t('、{{source}}', { source: data.sources.providers }) : '';
  const configNote = t('有效配置来自 {{env}}{{providers}}，并由 {{override}} 覆盖。', { env: '.env', providers: providerSource, override: data.override_path });
  return <div className="settings-layout">
    <nav className="subnav">{groups.map(k => <button className={group === k ? 'active' : ''} onClick={() => setGroup(k)} key={k}>{t(GROUP_LABELS[k])}<ChevronRight size={14} /></button>)}</nav>
    <Panel title={GROUP_LABELS[group]} note={configNote} className="config-panel">
      {data.pending_restart && <div className="notice amber"><TriangleAlert size={16} />{t('存在等待重启的修改')}</div>}
      {group === 'admin' ? <div className="admin-settings-status">
        <section className={`admin-security-hero ${activeAdminToken?.configured ? 'ready' : 'missing'}`}><div className="security-seal"><ShieldCheck size={27} /><i /></div><div><span>{t('保护状态')}</span><h3>{t(activeAdminToken?.configured ? '管理端访问已受保护' : '管理端令牌尚未配置')}</h3><p>{activeAdminToken?.configured ? t('当前令牌已加载，仅显示尾号 {{last4}}。完整值不会发送到浏览器。', { last4: activeAdminToken.last4 }) : t('请在启动环境中设置 ADMIN__TOKEN，然后重启 Coworker。')}</p></div><b>{t(activeAdminToken?.configured ? '已启用' : '未启用')}</b></section>
        <div className="admin-setting-cards"><article><KeyRound size={18} /><div><span>{t('令牌来源')}</span><b>{adminToken?.configured ? 'ADMIN__TOKEN' : fallbackToken?.configured ? 'DESKTOP_UPDATES__ADMIN_TOKEN' : t('未配置')}</b><small>{t('令牌只能通过启动配置轮换，管理页不会回显或覆盖。')}</small></div></article><article><FileCog size={18} /><div><span>{t('配置覆盖文件')}</span><code>{data.override_path}</code><small>{t('其他设置在这里持久化；管理员令牌不写入普通表单。')}</small></div></article><article><RefreshCw size={18} /><div><span>{t('配置生效状态')}</span><b>{t(data.pending_restart ? '等待安全重启' : '当前配置已加载')}</b><small>{t(data.pending_restart ? '保存的修改会在下一次安全重启后生效。' : '当前没有等待重启的管理端修改。')}</small></div></article><article><Fingerprint size={18} /><div><span>{t('浏览器会话')}</span><b>{t('仅当前标签会话')}</b><small>{t('令牌保存在 sessionStorage，关闭标签页后不会长期留存。')}</small></div></article></div>
        <div className="admin-security-note"><TriangleAlert size={16} /><p><b>{t('如何轮换管理员令牌')}</b><span>{t('修改部署环境中的')} <code>ADMIN__TOKEN</code>{t('，再执行安全重启。旧会话会在重启后失效。')}</span></p></div>
      </div> : <>{group === 'llm' && <div className="llm-config-overview"><div className="llm-config-copy"><Brain size={22} /><div><span>{t('启动配置')}</span><h3>{t('启动默认值与服务连接')}</h3><p>{t('这里决定 Coworker 重启时先连接哪个模型服务。运行中的模型切换、摘要模型和降级链请在“模型编排”页面调整。')}</p></div></div><div className="llm-config-facts"><span><b>{t(draft.llm.default_provider || '未设置')}</b>{t('启动 Provider')}</span><span><b>{t(draft.llm.default_model || '使用 Provider 默认值')}</b>{t('启动模型')}</span><span><b>{effectiveProviders.length}</b>{t('个可用连接')}</span></div></div>}<div className="config-fields">{group === 'llm' && <div className="config-section-heading"><div><b>{t('启动默认值')}</b><small>{t('只在进程启动时读取；修改后需要安全重启。')}</small></div></div>}{group === 'i18n' && <div className="config-section-heading"><div><b>{t('实例级运行时语言')}</b><small>{t('控制系统 Prompt、工具说明和系统通知；与本页界面语言相互独立。修改后需要安全重启。')}</small></div></div>}{Object.entries(draft[group] || {}).map(([key, value]) => {
        const path = `${group}.${key}`;
        if (HIDDEN_CONFIG.has(path) || key === 'config_file' || path.endsWith('runtime_config_file')) return null;
        if (group === 'llm' && (key === 'providers_file' || LLM_MODEL_ORCHESTRATION_FIELDS.has(key) || /_(api_key|base_url)$/.test(key))) return null;
        if (key === 'managed_providers' && Array.isArray(value)) return <div className="provider-editor" key={key}>
          <div className="provider-editor-head"><div><b>{t('Provider 连接')} <em className="effect-badge hot">{t('修改后立即生效')}</em></b><small>{t('一个连接代表一套模型服务地址、接口协议和访问密钥。正在执行的单次调用不受影响，下一次调用使用新连接。')}</small></div><button className="ghost mini" onClick={() => change('managed_providers', [...value, { name: '', type: 'openai', api_key: '', base_url: '', default_model: '' }])}><Plus size={14} />{t('添加连接')}</button></div>
          <div className="provider-source-note"><Database size={16} /><p><b>{t('配置来源彼此独立')}</b><span><code>.env</code> {t('和')} <code>providers.json</code>{t('中的连接只读展示；下方只编辑管理端覆盖，不会复制或接管外部密钥。')}</span></p></div>
          {externalProviders.length > 0 && <div className="provider-effective"><b>{t('外部有效连接（只读）')}</b>{externalProviders.map((provider: Json) => <span key={provider.name}><strong>{provider.name}</strong><code>{provider.type}</code><small>{provider.base_url || t('协议默认地址')}</small></span>)}</div>}
          {value.length ? value.map((provider: Json, index: number) => {
            const secretPath = `llm.managed_providers.${index}.api_key`;
            const status = data.secret_status[secretPath];
            return <article className="provider-row" key={index}>
              <Field label="连接名称" hint="在模型编排中引用的名称"><input value={provider.name || ''} onChange={e => changeProvider(index, 'name', e.target.value)} placeholder={t('例如 openai-work')} /></Field>
              <Field label="接口协议"><select value={provider.type || 'openai'} onChange={e => changeProvider(index, 'type', e.target.value)}>{['openai', 'anthropic', 'deepseek', 'qwen', 'zhipu', 'minimax'].map(type => <option key={type}>{type}</option>)}</select></Field>
              <Field label="服务地址（Base URL）"><input value={provider.base_url || ''} onChange={e => changeProvider(index, 'base_url', e.target.value)} placeholder={t('留空使用协议默认地址')} /></Field>
              <Field label="默认模型" hint="调用未指定模型时使用"><input value={provider.default_model || ''} onChange={e => changeProvider(index, 'default_model', e.target.value)} placeholder={t('可留空')} /></Field>
              <Field label="API Key" hint={status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 }) : t('当前未配置')}><input type="password" value={secretInputs[secretPath] || ''} onChange={e => setSecretInputs({ ...secretInputs, [secretPath]: e.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 }) : t('输入 API Key')} /></Field>
              <button className="danger-icon provider-remove" title={t('移除 Provider')} onClick={() => { change('managed_providers', value.filter((_: unknown, i: number) => i !== index)); setSecretInputs({}); }}><Trash2 size={15} /></button>
            </article>;
          }) : <div className="provider-empty">{t('还没有可用的 Provider 连接。点击“添加连接”配置模型服务。')}</div>}
        </div>;
        if (path === 'llm.default_provider') { const providerNames = Array.from(new Set([...effectiveProviders, ...(draft.llm.managed_providers || [])].map((provider: Json) => provider.name).filter(Boolean))); return <Field key={key} label={CONFIG_LABELS[path]} hint="Coworker 启动后首先使用的连接"><select value={String(value)} onChange={e => change(key, e.target.value)}>{!providerNames.includes(value) && <option value={String(value)}>{String(value)}</option>}{providerNames.map((name: string) => <option key={name}>{name}</option>)}</select></Field>; }
        if (path === 'i18n.locale') return <Field key={key} label={CONFIG_LABELS[path]} hint="保存后需安全重启；不会自动翻译用户内容或历史数据"><select value={String(value)} onChange={e => change(key, e.target.value)}><option value="zh-CN">简体中文 (zh-CN)</option><option value="en">English (en)</option></select></Field>;
        if (data.secret_status[path]) { const status = data.secret_status[path]; return <Field key={key} hot={isHot(path)} label={CONFIG_LABELS[path] || humanize(key)} hint={status.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 }) : t('当前未配置')}><input type="password" value={secretInputs[path] || ''} onChange={e => setSecretInputs({ ...secretInputs, [path]: e.target.value })} placeholder={status.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 }) : t('输入新值')} /></Field>; }
        if (typeof value === 'boolean') return <label className="switch config-switch" key={key}><input type="checkbox" checked={value} onChange={e => change(key, e.target.checked)} /><i /><span>{t(CONFIG_LABELS[path] || humanize(key))}{isHot(path) && <em className="effect-badge hot">{t('立即生效')}</em>}</span></label>;
        if (typeof value === 'number') return <Field key={key} hot={isHot(path)} label={CONFIG_LABELS[path] || humanize(key)} hint={path === 'llm.max_tokens' ? '模型单次响应允许生成的最大 token 数' : undefined}><input type="number" value={value} step="any" onChange={e => change(key, Number(e.target.value))} /></Field>;
        if (typeof value === 'string') return <Field key={key} hot={isHot(path)} label={CONFIG_LABELS[path] || humanize(key)} hint={path === 'llm.default_model' ? 'Provider 连接没有单独指定模型时使用' : undefined}><input value={value} onChange={e => change(key, e.target.value)} /></Field>;
        return <Field key={key} hot={isHot(path)} label={CONFIG_LABELS[path] || humanize(key)} hint="JSON 结构"><textarea className="code-area compact" value={JSON.stringify(value, null, 2)} onChange={e => { try { change(key, JSON.parse(e.target.value)); } catch { /* keep last valid */ } }} /></Field>;
      })}</div>
      {message && <div className="notice success">{message}</div>}
      <div className="panel-actions"><button className="primary" onClick={() => void save()}><Save size={15} />{t('保存覆盖')}</button><button className="ghost" onClick={() => { setDraft(structuredClone(data.config)); setSecretInputs({}); }}>{t('重置本页')}</button></div></>}
    </Panel>
  </div>;
}

function humanize(text: string) { return text.replace(/_/g, ' ').replace(/\b\w/g, (m: string) => m.toUpperCase()); }

const TASK_STATUS: Record<string, string> = { pending: '待处理', in_progress: '进行中', completed: '已完成' };

function timeFromNow(value: string) {
  const delta = new Date(value).getTime() - Date.now();
  const abs = Math.abs(delta);
  const units: Array<[number, string]> = [[86_400_000, '天'], [3_600_000, '小时'], [60_000, '分钟']];
  const [size, label] = units.find(([unitSize]) => abs >= unitSize) || [1000, '秒'];
  const amount = Math.max(1, Math.round(abs / size));
  const values = { amount, unit: t(label) };
  return delta >= 0 ? t('{{amount}} {{unit}}后', values) : t('已过 {{amount}} {{unit}}', values);
}

function repeatLabel(seconds?: number | null) {
  if (!seconds) return t('仅一次');
  if (seconds % 86400 === 0) return t('每 {{amount}} 天', { amount: seconds / 86400 });
  if (seconds % 3600 === 0) return t('每 {{amount}} 小时', { amount: seconds / 3600 });
  if (seconds % 60 === 0) return t('每 {{amount}} 分钟', { amount: seconds / 60 });
  return t('每 {{amount}} 秒', { amount: seconds });
}

function Runtime({ coworkerName }: { coworkerName: string }) {
  const [tab, setTab] = useState<'tasks' | 'alarms' | 'logs' | 'maintenance'>('tasks');
  return <div className="page-stack"><div className="tabbar">{[
    ['tasks', '任务'], ['alarms', '闹钟'], ['logs', '运行日志'], ['maintenance', '维护'],
  ].map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id as 'tasks' | 'alarms' | 'logs' | 'maintenance')}>{t(label)}</button>)}</div>
    {tab === 'tasks' && <Tasks />}{tab === 'alarms' && <Alarms />}{tab === 'logs' && <Logs />}{tab === 'maintenance' && <Maintenance name={coworkerName} />}
  </div>;
}

function MemoryCenter({ coworkerName }: { coworkerName: string }) {
  const [tab, setTab] = useState<'short' | 'long' | 'thoughts'>('short');
  return <div className="page-stack memory-center">
    <div className="tabbar memory-tabs">
      <button className={tab === 'short' ? 'active' : ''} onClick={() => setTab('short')}><MessagesSquare size={14} />{t('短期记忆')}</button>
      <button className={tab === 'long' ? 'active' : ''} onClick={() => setTab('long')}><Database size={14} />{t('长期记忆')}</button>
      <button className={tab === 'thoughts' ? 'active' : ''} onClick={() => setTab('thoughts')}><Orbit size={14} />{t('并行思考记录')}</button>
    </div>
    {tab === 'short' ? <ShortTermMemoryView coworkerName={coworkerName} /> : tab === 'long' ? <Memories /> : <Bubbles coworkerName={coworkerName} />}
  </div>;
}

const MEMORY_ROLE: Record<string, string> = { user: '消息', assistant: '搭档', system: '系统', tool: '工具结果' };
const MEMORY_SOURCE: Record<string, string> = {
  file: '文件投递', rest: 'REST API', websocket: 'WebSocket', wecom: '企业微信',
  coworker_desktop: '桌面端', codex: 'Codex', bubble: '气泡', alarm: '闹钟提醒',
  code_job: '代码任务', task_reminder: '任务提醒', system: '系统', '并行思考': '并行思考',
  system_recovery: '系统恢复', system_error: '系统错误', skill_warning: '技能提醒',
  tick: '自主循环', model_switch: '模型切换', auto_recall: '自动回忆',
  recent_activity_auto_recall: '近期回忆', compress_memory: '记忆压缩', sleep_interrupt: '唤醒消息',
};

function memorySourceName(source: unknown) {
  const names = String(source || '').split(' + ').map(item => MEMORY_SOURCE[item]).filter((item): item is string => Boolean(item));
  return names.length ? names.map(item => t(item)).join(' + ') : t('消息');
}

function memoryContentText(content: unknown) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return String(content ?? '');
  return content.map(block => {
    if (!block || typeof block !== 'object') return String(block);
    const item = block as Json;
    const kind = String(item.type || t('结构化内容'));
    if (['text', 'input_text', 'output_text'].includes(kind)) return String(item.text ?? '');
    const filename = item.filename || item._filename || item.name;
    return '[' + kind + (filename ? ' · ' + filename : '') + ']';
  }).join('\n');
}

function memoryDetailText(value: unknown) {
  if (Array.isArray(value)) return memoryContentText(value);
  if (value && typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value ?? '');
}

function memoryPreview(message: Json) {
  const toolCalls = message.tool_calls || [];
  const fallback = toolCalls.length
    ? t('调用 {{names}}', { names: toolCalls.map((call: Json) => call.name).join(t('、')) })
    : t('无可预览内容');
  return (memoryContentText(message.content).trim() || String(message.reasoning_content || '').trim() || fallback).replace(/\s+/g, ' ');
}

function MemoryMessage({ message, index, defaultOpen = false, coworkerName = '' }: { message: Json; index: number; defaultOpen?: boolean; coworkerName?: string }) {
  const role = message.role === 'assistant' && coworkerName && coworkerName.toLowerCase() !== 'coworker'
    ? coworkerName
    : message.role === 'user' ? memorySourceName(message.source) : t(MEMORY_ROLE[message.role] || message.role);
  const usage = message.role === 'assistant' && message.usage
    ? t(' · 输入 {{input}} / 输出 {{output}} token', { input: Number(message.usage.input_tokens || 0).toLocaleString(), output: Number(message.usage.output_tokens || 0).toLocaleString() })
    : '';
  const sourceName = memorySourceName(message.source);
  const summaryState = message.pin_id
    ? t('固定')
    : message.tool_calls?.length
      ? t('{{count}} 个工具调用', { count: message.tool_calls.length })
      : message.stop_reason || '';
  return <details className={'short-message role-' + message.role} open={defaultOpen}>
    <summary><span className="message-index">{String(index + 1).padStart(2, '0')}</span><span className="message-summary-copy"><b>{role}</b><small>{new Date(message.timestamp).toLocaleString()}{' · '}{sourceName}{usage}</small><em className="message-preview">{memoryPreview(message)}</em></span><i>{summaryState}</i></summary>
    <div className="short-message-body"><pre>{memoryContentText(message.content)}</pre>{message.reasoning_content && <section className="message-reasoning"><b><Brain size={12} />{t('思考')}</b><pre>{message.reasoning_content}</pre></section>}{message.tool_calls?.length > 0 && <section className="message-tool-section"><b><Wrench size={12} />{t('工具调用')}</b><div className="message-tools">{message.tool_calls.map((call: Json) => <details className="tool-exchange" key={call.id || call.name} open><summary><span><Wrench size={11} />{call.name}</span><small>{'result' in call ? t('已返回') : t('等待结果')}</small></summary><div><label>{t('参数')}</label><pre>{memoryDetailText(call.arguments)}</pre><label>{t('结果')}</label><pre>{'result' in call ? memoryDetailText(call.result) : t('尚未返回结果')}</pre></div></details>)}</div></section>}{message.recalled_memory_ids?.length > 0 && <p>{t('召回长期记忆：')} {message.recalled_memory_ids.join(' · ')}</p>}{message.tool_call_id && <p>{t('工具调用 ID：')} {message.tool_call_id}</p>}</div>
  </details>;
}

function MemoryTreeNode({ node, depth = 0 }: { node: Json; depth?: number }) {
  const children = node.children || [];
  return <details className="short-tree-node" open={depth === 0} style={{ '--indent': Math.min(depth, 3) * 7 + 'px' } as React.CSSProperties}>
    <summary>
      <span className="tree-level">L{node.level}</span>
      <span className="tree-node-copy"><b>{new Date(node.t_start).toLocaleString()} → {new Date(node.t_end).toLocaleString()}</b><small>{t('{{count}} 条消息', { count: node.msg_count })}{' · '}{Number(node.token_estimate).toLocaleString()} token{' · '}{node.token_count_source === 'exact' ? t('精确摘要计数') : t('估算摘要计数')}</small></span>
      <span className={node.raw_available ? 'raw-state' : 'raw-state summary-only'}>{node.raw_available ? t('原文可达') : t('仅摘要')}</span>
    </summary>
    <div className="tree-node-detail"><p>{node.summary}</p>{children.length > 0 && <div className="tree-children">{children.map((child: Json, childIndex: number) => <MemoryTreeNode node={child} depth={depth + 1} key={child.t_start + '-' + child.level + '-' + childIndex} />)}</div>}</div>
  </details>;
}

function ShortTermMemoryView({ coworkerName }: { coworkerName: string }) {
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>('/api/admin/memory/short-term'), []);
  const [maxLeaves, setMaxLeaves] = useState(64);
  const [pinDraft, setPinDraft] = useState({ label: '', content: '' });
  const [pinSaving, setPinSaving] = useState(false);
  const [pinError, setPinError] = useState('');
  const [pinMessage, setPinMessage] = useState('');
  const [actionError, setActionError] = useState('');
  const [actionMessage, setActionMessage] = useState('');
  useEffect(() => {
    if (!data?.backfill?.running) return;
    const timer = window.setInterval(() => { void api<Json>('/api/admin/memory/short-term').then(setData).catch(() => undefined); }, 1500);
    return () => window.clearInterval(timer);
  }, [data?.backfill?.running, setData]);
  if (loading || !data) return <Loading error={error} />;
  const water = data.token_watermark;
  const ratio = Math.max(0, Number(water.ratio || 0));
  const percent = Math.round(ratio * 100);
  const measured = water.measured_at ? new Date(water.measured_at).toLocaleString() : t('当前读取');
  const startBackfill = async () => {
    setActionError(''); setActionMessage('');
    try {
      await api('/api/admin/memory/backfill?max_leaves=' + Math.max(1, Math.min(512, maxLeaves)), { method: 'POST' });
      setActionMessage(t('记忆树回溯已开始')); await reload();
    } catch (error) { setActionError(error instanceof Error ? error.message : t('回溯启动失败')); }
  };
  const addPin = async (event: FormEvent) => {
    event.preventDefault(); setPinError(''); setPinMessage(''); setPinSaving(true);
    try {
      await api('/api/admin/memory/pinned', { method: 'POST', body: JSON.stringify(pinDraft) });
      setPinDraft({ label: '', content: '' }); setPinMessage(t('固定上下文已添加')); await reload();
    } catch (error) { setPinError(error instanceof Error ? error.message : t('固定上下文添加失败')); }
    finally { setPinSaving(false); }
  };
  const removePin = async (item: Json) => {
    if (!confirm(t('删除固定上下文“{{label}}”？', { label: item.label }))) return;
    setPinError(''); setPinMessage('');
    try { await api('/api/admin/memory/pinned/' + encodeURIComponent(item.pin_id), { method: 'DELETE' }); setPinMessage(t('固定上下文已删除')); await reload(); }
    catch (error) { setPinError(error instanceof Error ? error.message : t('固定上下文删除失败')); }
  };
  return <div className="page-stack short-memory-page">
    <section className="short-watermark">
      <div className="watermark-reading">
        <div className="watermark-orbit" style={{ '--water': Math.min(100, percent) + '%' } as React.CSSProperties}><span><b>{percent}%</b><small>{t('上下文水位')}</small></span></div>
        <div><p className="eyebrow">{t('最近一次模型输入')}</p><h2>{Number(water.tokens).toLocaleString()} <small>/ {Number(water.capacity).toLocaleString()} token</small></h2><div className="watermark-track"><i style={{ width: Math.min(100, percent) + '%' }} /></div><p>{water.source === 'provider' ? t('Provider 精确值') : t('本地估算值')}{' · '}{measured}</p></div>
      </div>
      <div className="watermark-facts">
        <span><small>{t('采样模型')}</small><b>{water.provider}/{water.model}</b></span>
        <span><small>{t('当前短期估算')}</small><b>{Number(water.estimated_short_term_tokens).toLocaleString()} token</b></span>
        <span><small>{t('消息 / 脊柱 / 固定项')}</small><b>{data.stats.message_count} / {data.stats.tree_node_count} / {data.stats.pinned_count}</b></span>
        <p><ShieldCheck size={13} />{t('精确值包含系统提示与工具定义；短期估算用于判断压缩水位。')}</p>
      </div>
      <button className="icon-btn watermark-refresh" onClick={() => void reload()} title={t('刷新短期记忆')} aria-label={t('刷新短期记忆')}><RefreshCw size={16} /></button>
    </section>

    <div className="short-memory-grid">
      <Panel title="记忆脊柱" note="越老的记忆层级越高；展开节点可向下查看保留的细节。" className="short-tree-panel">
        <div className="short-tree">{data.tree.nodes.length ? data.tree.nodes.map((node: Json, treeIndex: number) => <MemoryTreeNode node={node} key={node.t_start + '-' + node.level + '-' + treeIndex} />) : <Empty text="记忆树还是空的；上下文压缩后会在这里形成时间脊柱。" />}</div>
      </Panel>
      <Panel title="当前消息尾部" note="这些消息会按顺序直接进入下一次主线思考。" className="short-tail-panel">
        <div className="short-message-list">{data.messages.length ? data.messages.map((message: Json, messageIndex: number) => <MemoryMessage message={message} index={messageIndex} defaultOpen={messageIndex >= data.messages.length - 3} coworkerName={coworkerName} key={message.timestamp + '-' + message.index} />) : <Empty text="当前没有短期消息；新的输入会从这里开始累积。" />}</div>
      </Panel>
    </div>

    <Panel title="固定上下文" note="固定项会在缺失时重新注入主线，避免关键资料被压缩带走。">
      {(pinError || pinMessage) && <div className={'notice ' + (pinError ? 'error' : 'success')}>{pinError || pinMessage}</div>}
      <form className="pin-compose" onSubmit={addPin}><input required maxLength={80} value={pinDraft.label} onChange={event => setPinDraft({ ...pinDraft, label: event.target.value })} placeholder={t('标题，例如：项目约定')} /><textarea required value={pinDraft.content} onChange={event => setPinDraft({ ...pinDraft, content: event.target.value })} placeholder={t('需要始终保留在上下文里的内容')} /><button className="primary" disabled={pinSaving}><Plus size={14} />{pinSaving ? t('添加中…') : t('添加固定项')}</button></form>
      <div className="pinned-context-list">{data.pinned_items.length ? data.pinned_items.map((item: Json) => <details key={item.pin_id}><summary><Fingerprint size={15} /><span><b>{item.label}</b><small>{item.pin_id}{' · '}{new Date(item.created_at).toLocaleString()}</small></span><button type="button" className="icon-btn pin-delete" title={t('删除固定上下文')} aria-label={t('删除固定上下文 {{label}}', { label: item.label })} onClick={event => { event.preventDefault(); void removePin(item); }}><Trash2 size={14} /></button></summary><pre>{item.content}</pre>{item.file_path && <p><FileText size={12} />{t('跟随文件：')} {item.file_path}</p>}</details>) : <Empty text="当前没有固定上下文；可以从上方添加。" />}</div>
    </Panel>

    <Panel title="记忆维护" note="压缩会调用模型；回溯在后台从持久日志重建时间脊柱。">
      {(actionError || actionMessage) && <div className={'notice ' + (actionError ? 'error' : 'success')}>{actionError || actionMessage}</div>}
      <div className="danger-list memory-maintenance">
        <DangerAction title="全量压缩短期记忆" description="把当前主线消息压缩进记忆树，释放上下文空间。执行期间会产生模型调用。" button="开始压缩" name={coworkerName} onConfirm={async () => { await api('/api/admin/memory/compress', { method: 'POST', body: JSON.stringify({ confirm_name: coworkerName || '未命名' }) }); await reload(); }} />
        <article className="danger-card mild"><ArchiveRestore size={20} /><div><b>{t('回溯记忆树')}</b><p>{data.backfill.running ? t('正在重建：{{done}}/{{total}}', { done: data.backfill.done, total: data.backfill.total || '—' }) : t('从持久日志后台重建多尺度记忆树，不阻塞主循环。')}</p></div><input className="tiny-input" aria-label={t('最多回溯叶子数')} type="number" min="1" max="512" value={maxLeaves} onChange={event => setMaxLeaves(Number(event.target.value))} /><button className="ghost" disabled={data.backfill.running} onClick={() => void startBackfill()}>{data.backfill.running ? t('回溯中…') : t('开始回溯')}</button></article>
      </div>
    </Panel>
  </div>;
}

function Tasks() {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/tasks'), []);
  const [draft, setDraft] = useState({ description: '', details: '' });
  const [filter, setFilter] = useState('active');
  const [editing, setEditing] = useState<Json | null>(null);
  const create = async () => {
    await api('/api/admin/tasks', { method: 'POST', body: JSON.stringify(draft) });
    setDraft({ description: '', details: '' });
    await reload();
  };
  if (loading || !data) return <Loading error={error} />;
  const counts = data.tasks.reduce((acc: Json, task: Json) => ({ ...acc, [task.status]: (acc[task.status] || 0) + 1 }), {});
  const visible = data.tasks.filter((task: Json) => filter === 'all' || (filter === 'active' ? task.status !== 'completed' : task.status === filter));
  const saveEdit = async () => {
    if (!editing) return;
    await api('/api/admin/tasks/' + editing.id, { method: 'PATCH', body: JSON.stringify(editing) });
    setEditing(null);
    await reload();
  };
  const filters = [
    ['active', t('进行中 {{count}}', { count: Number(counts.pending || 0) + Number(counts.in_progress || 0) })],
    ['completed', t('已完成 {{count}}', { count: counts.completed || 0 })],
    ['all', t('全部 {{count}}', { count: data.tasks.length })],
  ];
  return <Panel title="任务板" note="任务说明与执行细节会和 Coworker 的 task 工具实时共享。">
    <div className="task-compose"><input value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} onKeyDown={event => { if (event.key === 'Enter' && draft.description.trim()) void create(); }} placeholder={t('要完成什么？')} /><textarea value={draft.details} onChange={event => setDraft({ ...draft, details: event.target.value })} placeholder={t('补充执行细节（可选）')} /><button className="primary" disabled={!draft.description.trim()} onClick={() => void create()}><Plus size={15} />{t('添加任务')}</button></div>
    <div className="list-toolbar"><div className="task-filters">{filters.map(([id, label]) => <button key={id} className={filter === id ? 'active' : ''} onClick={() => setFilter(id)}>{label}</button>)}</div><button className="icon-btn" onClick={() => void reload()} title={t('刷新任务')} aria-label={t('刷新任务')}><RefreshCw size={15} /></button></div>
    <div className="record-list">{visible.length ? visible.map((task: Json) => <article className={'record task-record ' + task.status} key={task.id}><div className="record-main"><span className={'status-pill ' + task.status}>{t(TASK_STATUS[task.status] || task.status)}</span><b>{task.description}</b>{task.details && <p className="record-details">{task.details}</p>}<small>{t('更新于 {{time}}', { time: new Date(task.updated_at).toLocaleString() })}{' · '}{task.id}</small></div><div className="row-actions"><select aria-label={t('更新任务“{{description}}”的状态', { description: task.description })} value={task.status} onChange={async event => { await api('/api/admin/tasks/' + task.id, { method: 'PATCH', body: JSON.stringify({ description: task.description, details: task.details || '', status: event.target.value }) }); await reload(); }}>{Object.entries(TASK_STATUS).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select><button className="icon-btn" title={t('编辑任务')} aria-label={t('编辑任务')} onClick={() => setEditing({ ...task })}><Pencil size={15} /></button><button className="danger-icon" title={t('删除任务')} aria-label={t('删除任务“{{description}}”', { description: task.description })} onClick={async () => { if (confirm(t('删除任务“{{description}}”？', { description: task.description }))) { await api('/api/admin/tasks/' + task.id, { method: 'DELETE' }); await reload(); } }}><Trash2 size={15} /></button></div></article>) : <Empty text={data.tasks.length ? '这个分类里没有任务。' : '还没有任务，先写下第一件要推进的事。'} />}</div>
    {editing && <div className="modal-layer"><div className="confirm-modal task-modal"><ListTodo size={24} /><h3>{t('编辑任务')}</h3><Field label="任务描述"><input autoFocus value={editing.description} onChange={event => setEditing({ ...editing, description: event.target.value })} /></Field><Field label="执行细节"><textarea value={editing.details || ''} onChange={event => setEditing({ ...editing, details: event.target.value })} placeholder={t('记录计划、进度或下一步')} /></Field><Field label="状态"><select value={editing.status} onChange={event => setEditing({ ...editing, status: event.target.value })}>{Object.entries(TASK_STATUS).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select></Field><div className="panel-actions"><button className="ghost" onClick={() => setEditing(null)}>{t('取消')}</button><button className="primary" disabled={!editing.description.trim()} onClick={() => void saveEdit()}><Check size={15} />{t('保存任务')}</button></div></div></div>}
  </Panel>;
}

function Bubbles({ coworkerName }: { coworkerName: string }) {
  const [scope, setScope] = useState<'bubbles' | 'subconscious'>('bubbles');
  const basePath = scope === 'bubbles' ? '/api/admin/bubbles' : '/api/admin/subconscious';
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>(basePath + '?limit=50'), [scope]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState('');
  useEffect(() => setMoreError(''), [scope]);
  if (loading || !data) return <Loading error={error} />;
  const loadMore = async () => {
    setLoadingMore(true); setMoreError('');
    try {
      const next = await api<Json>(basePath + '?limit=50&offset=' + data.bubbles.length);
      setData({ ...next, bubbles: [...data.bubbles, ...(next.bubbles || [])] });
    } catch (error) { setMoreError(error instanceof Error ? error.message : t('更多历史记录加载失败')); }
    finally { setLoadingMore(false); }
  };
  return <Panel title="并行思考记录" note="查看主动 Bubble 和潜意识已落盘的完整思考轨迹。"><div className="list-toolbar"><div className="task-filters"><button className={scope === 'bubbles' ? 'active' : ''} onClick={() => setScope('bubbles')}>{t('主动 Bubble')}</button><button className={scope === 'subconscious' ? 'active' : ''} onClick={() => setScope('subconscious')}>{t('潜意识')}</button></div><button className="icon-btn" onClick={() => void reload()} title={t('刷新思考记录')} aria-label={t('刷新思考记录')}><RefreshCw size={15} /></button></div><div className="bubble-list">{data.bubbles.length ? data.bubbles.map((bubble: Json) => <BubbleRecord bubble={bubble} reload={reload} scope={scope} coworkerName={coworkerName} key={bubble.log_id || bubble.id} />) : <Empty text={scope === 'bubbles' ? '当前没有 Bubble 记录。' : '当前没有潜意识记录。'} />}</div>{moreError && <div className="notice error">{moreError}</div>}{data.has_more && <button className="bubble-load-more ghost" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? t('加载中…') : t('加载更多（已显示 {{shown}}/{{total}}）', { shown: data.bubbles.length, total: data.total })}</button>}</Panel>;
}

const BUBBLE_STATUS: Record<string, string> = {
  running: '运行中', done: '完成', error: '失败', cancelled: '已取消', timeout: '超时',
};

function bubbleHistoryMessages(events: Json[]) {
  const results = new Map(events.filter(event => event.type === 'tool_result').map(event => [event.id, event]));
  return events.flatMap((event, index) => {
    const common = { timestamp: event.ts, index, source: '并行思考' };
    if (event.type === 'tool_call' || event.type === 'tool_result') return [];
    if (event.type === 'message_in') return [{ ...common, role: event.participant_id === 'system' ? 'system' : 'user', source: event.source || '并行思考', content: event.content }];
    if (event.type === 'thinking_start') return [{ ...common, role: 'system', content: t('第 {{count}} 轮开始{{mode}}', { count: Number(event.cycle || 0) + 1, mode: event.thinking === false ? t('（快速模式）') : '' }) }];
    if (event.type === 'llm_response') return [{
      ...common, role: 'assistant', source: event.model || '并行思考', content: event.content || '', reasoning_content: event.reasoning_content, usage: event.usage,
      stop_reason: event.stop_reason,
      tool_calls: (event.tool_calls || []).map((call: Json) => {
        const result = results.get(call.id);
        return result ? { ...call, result: result.content } : call;
      }),
    }];
    if (event.__meta__) return [{ ...common, role: 'system', content: t('并行思考结束\n状态：{{status}}\n目标：{{goal}}', { status: t(BUBBLE_STATUS[event.status] || event.status || '未知'), goal: event.goal || t('未记录') }) }];
    if (event.type === 'bubble_snapshot') return [{ ...common, role: 'system', content: [
      t('状态：{{status}}', { status: t(BUBBLE_STATUS[event.status] || event.status || '未知') }),
      t('目标：{{goal}}', { goal: event.goal || t('未记录') }),
      event.result && t('结论：{{result}}', { result: event.result }),
      event.error && t('错误：{{error}}', { error: event.error }),
      event.content,
    ].filter(Boolean).join('\n') }];
    const { type, ts, seq, ...detail } = event;
    return [{ ...common, role: 'system', source: type || '并行思考', content: memoryDetailText(detail) }];
  });
}

function BubbleRecord({ bubble, reload, scope, coworkerName }: { bubble: Json; reload: () => Promise<void>; scope: 'bubbles' | 'subconscious'; coworkerName: string }) {
  const [open, setOpen] = useState(false);
  const [events, setEvents] = useState<Json[] | null>(null);
  const [historyError, setHistoryError] = useState('');
  const messages = useMemo(() => events ? bubbleHistoryMessages(events) : null, [events]);
  const loadHistory = async () => {
    const next = !open; setOpen(next);
    if (!next || events) return;
    setHistoryError('');
    try { const result = await api<Json>('/api/admin/' + scope + '/' + encodeURIComponent(bubble.log_id || bubble.id) + '/history'); setEvents(result.events || []); }
    catch (error) { setHistoryError(error instanceof Error ? error.message : t('历史记录加载失败')); }
  };
  const model = [bubble.provider, bubble.model].filter(Boolean).join('/') || t('模型未记录');
  const createdAt = bubble.created_at ? t(' · {{time}}', { time: new Date(bubble.created_at).toLocaleString() }) : '';
  return <article className={'bubble-record ' + (open ? 'open' : '')}>
    <div className="bubble-record-head">
      <div className="record-main">
        <div className="bubble-record-tags">
          <span className={'status-pill ' + bubble.status}>{t(BUBBLE_STATUS[bubble.status] || bubble.status)}</span>
          {bubble.mode && <span className="bubble-mode">{bubble.mode}</span>}
          {bubble.handoff_transparency && <span className="bubble-handoff-tag"><ShieldCheck size={11} />{t('透明转交')}</span>}
        </div>
        <b className="bubble-record-title" title={bubble.goal}>{bubble.goal}</b>
        {(bubble.participant_id || bubble.conversation_id || bubble.resume_count) && <div className="bubble-record-routing">
          {bubble.participant_id && <span title={bubble.participant_id}><MessagesSquare size={11} />{t('对象')}<code>{bubble.participant_id}</code></span>}
          {bubble.conversation_id && <span title={bubble.conversation_id}>{t('会话')}<code>{bubble.conversation_id}</code></span>}
          {bubble.resume_count > 0 && <span><RotateCcw size={11} />{t('续跑 {{count}} 次', { count: bubble.resume_count })}</span>}
        </div>}
        <small className="bubble-record-meta">{t('ID {{id}} · {{model}} · 执行 {{cycles}} 轮 · {{seconds}} 秒', { id: bubble.id, model, cycles: bubble.cycles_used, seconds: Math.round(bubble.elapsed_seconds || 0) })}{createdAt}</small>
      </div>
      <div className="row-actions">
        <button className="ghost mini" aria-expanded={open} onClick={() => void loadHistory()}>{open ? t('收起记录') : t('查看记录')}</button>
        {scope === 'bubbles' && bubble.status === 'running' && <button className="danger-outline" onClick={async () => { if (confirm(t('取消 Bubble {{id}}？已完成的局部结果会保留。', { id: bubble.id }))) { await api('/api/admin/bubbles/' + bubble.id + '/cancel', { method: 'POST' }); await reload(); } }}>{t('取消')}</button>}
      </div>
    </div>
    {open && <div className="bubble-history">{historyError ? <div className="notice error">{historyError}</div> : messages ? <div className="short-message-list">{messages.map((message, index) => <MemoryMessage message={message} index={index} defaultOpen={index >= messages.length - 3} coworkerName={coworkerName} key={message.timestamp + '-' + message.index} />)}</div> : <div className="bubble-history-loading">{t('正在读取历史记录…')}</div>}</div>}
  </article>;
}

function Memories() {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<Json[]>([]);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState('');
  const [editText, setEditText] = useState('');
  const [editTags, setEditTags] = useState('');
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [lastQuery, setLastQuery] = useState('');
  const [saving, setSaving] = useState(false);
  const search = async () => {
    const query = q.trim(); if (!query || loading) return;
    setLoading(true); setError(''); setEditing('');
    try {
      const result = await api<Json>('/api/admin/memories?q=' + encodeURIComponent(query));
      setItems(result.memories || []);
      setLastQuery(query);
      setSearched(true);
    } catch (error) { setError(error instanceof Error ? error.message : t('检索失败')); }
    finally { setLoading(false); }
  };
  const saveMemory = async (item: Json) => {
    const content = editText.trim(); if (!content || saving) return;
    const tags = [...new Set(editTags.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
    setSaving(true); setError('');
    try {
      await api('/api/admin/memories/' + item.id, { method: 'PATCH', body: JSON.stringify({ content, tags }) });
      setItems(current => current.map(entry => entry.id === item.id ? { ...entry, content, tags } : entry));
      setEditing(''); setEditText(''); setEditTags('');
    } catch (error) { setError(error instanceof Error ? error.message : t('保存失败')); }
    finally { setSaving(false); }
  };
  const examples = [t('最近的重要决定'), t('对我的工作偏好'), t('尚未完成的约定')];
  return <Panel title="长期记忆" note="用一段自然语言，找出 Coworker 可能在未来主动想起的内容。" className="memory-panel">
    <div className="memory-search-stage">
      <div className="memory-search-mark" aria-hidden="true"><Brain size={22} /><i /><i /></div>
      <div className="memory-search-copy"><span>{t('语义召回')}</span><h3>{t('她记得什么？')}</h3><p>{t('不必输入精确关键词，可以描述一件事、一个人或某次决定。')}</p></div>
      <div className="memory-query">
        <Search size={18} aria-hidden="true" />
        <input aria-label={t('搜索长期记忆')} value={q} onChange={event => setQ(event.target.value)} onKeyDown={event => event.key === 'Enter' && void search()} placeholder={t('例如：我们对发布节奏做过什么决定？')} />
        {q && <button className="memory-query-clear" aria-label={t('清空搜索')} title={t('清空')} onClick={() => setQ('')}><X size={14} /></button>}
        <button className="memory-query-submit" disabled={!q.trim() || loading} onClick={() => void search()}>{loading ? t('正在召回…') : t('召回记忆')}<ChevronRight size={15} /></button>
      </div>
      <div className="memory-examples"><span>{t('试着搜索')}</span>{examples.map(example => <button key={example} onClick={() => setQ(example)}>{example}</button>)}</div>
    </div>
    {error && <div className="notice error memory-notice">{error}</div>}
    {searched && <div className="memory-result-head"><div><SlidersHorizontal size={14} /><span>{t('与“{{query}}”相关的记忆', { query: lastQuery })}</span></div><b>{t('{{count}} 条结果', { count: items.length })}</b></div>}
    {loading ? <div className="memory-recalling" role="status"><span className="state-pulse" aria-hidden="true"><i /><i /><i /></span><span>{t('正在沿着语义线索寻找记忆…')}</span></div> : <div className="memory-results">{items.map((item, index) => {
      const score = item.score == null ? null : Math.max(0, Math.min(100, Math.round(item.score * 100)));
      const isEditing = editing === item.id;
      return <article key={item.id} className={isEditing ? 'editing' : ''}>
        <div className="memory-rank" aria-hidden="true">{String(index + 1).padStart(2, '0')}</div>
        <div className="memory-card-body">
          <header><span>{t(item.category || '未分类')}</span>{score != null ? <div className="memory-score" title={t('语义相关度 {{score}}%', { score })}><i><b style={{ width: score + '%' }} /></i><small>{t('{{score}}% 相关', { score })}</small></div> : <small className="memory-id">{item.id}</small>}</header>
          {isEditing ? <div className="memory-editor"><label><span>{t('记忆内容')}</span><textarea autoFocus className="memory-edit" value={editText} onChange={event => setEditText(event.target.value)} /></label><label><span>{t('标签')}</span><input className="memory-tag-edit" value={editTags} onChange={event => setEditTags(event.target.value)} placeholder={t('多个标签用逗号分隔')} /></label></div> : <p>{item.content}</p>}
          <footer><div className="memory-tags">{(item.tags || []).map((tag: string) => <i key={tag}>{tag}</i>)}{!(item.tags || []).length && <span>{t('无标签')}</span>}</div><div className="memory-actions">{isEditing ? <><button className="ghost mini" onClick={() => { setEditing(''); setEditText(''); setEditTags(''); }}>{t('取消')}</button><button className="primary mini" disabled={!editText.trim() || saving} onClick={() => void saveMemory(item)}>{saving ? t('保存中…') : t('保存修改')}</button></> : <button className="ghost mini" onClick={() => { setEditing(item.id); setEditText(item.content); setEditTags((item.tags || []).join(', ')); }}><Pencil size={13} />{t('编辑')}</button>}<button className="danger-icon" title={t('删除这条记忆')} aria-label={t('删除记忆：{{content}}', { content: item.content.slice(0, 40) })} onClick={async () => { if (confirm(t('删除这条记忆？\n\n{{content}}', { content: item.content.slice(0, 100) }))) { try { await api('/api/admin/memories/' + item.id, { method: 'DELETE' }); setItems(current => current.filter(entry => entry.id !== item.id)); } catch (error) { setError(error instanceof Error ? error.message : t('删除失败')); } } }}><Trash2 size={14} /></button></div></footer>
        </div>
      </article>;
    })}</div>}
    {!loading && !searched && <div className="memory-empty"><Orbit size={24} /><b>{t('从一个模糊线索开始')}</b><p>{t('长期记忆按含义检索。描述得越具体，排在前面的内容通常越接近你想找的那件事。')}</p></div>}
    {!loading && searched && !items.length && <div className="memory-empty searched"><Search size={24} /><b>{t('没有找到相近的记忆')}</b><p>{t('换一种说法，或加入人物、项目和时间等线索后再试一次。')}</p></div>}
    <p className="memory-footnote"><ShieldCheck size={13} />{t('编辑会修正未来的回忆内容；删除后无法从这里恢复。')}</p>
  </Panel>;
}

function Alarms() {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/alarms'), []);
  const [draft, setDraft] = useState({ trigger_at: '', message: '', repeat_seconds: '' });
  if (loading || !data) return <Loading error={error} />;
  const create = async () => {
    await api('/api/admin/alarms', { method: 'POST', body: JSON.stringify({ message: draft.message, trigger_at: new Date(draft.trigger_at).toISOString(), repeat_seconds: draft.repeat_seconds ? Number(draft.repeat_seconds) : null }) });
    setDraft({ trigger_at: '', message: '', repeat_seconds: '' });
    await reload();
  };
  const alarms = [...data.alarms].sort((a: Json, b: Json) => new Date(a.trigger_at).getTime() - new Date(b.trigger_at).getTime());
  const summary = alarms.length
    ? t('正在守候 {{count}} 个提醒，最近一个 {{time}}', { count: alarms.length, time: timeFromNow(alarms[0].trigger_at) })
    : t('当前没有待触发提醒');
  return <Panel title="闹钟与守候" note="时间按本地时区输入；到点后提醒会进入 Coworker 的 inbox。">
    <div className="alarm-compose"><Field label="提醒时间"><input type="datetime-local" value={draft.trigger_at} min={new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} onChange={event => setDraft({ ...draft, trigger_at: event.target.value })} /></Field><Field label="重复"><select value={draft.repeat_seconds} onChange={event => setDraft({ ...draft, repeat_seconds: event.target.value })}><option value="">{t('仅一次')}</option><option value="3600">{t('每小时')}</option><option value="86400">{t('每天')}</option><option value="604800">{t('每周')}</option></select></Field><Field label="提醒内容"><input value={draft.message} onChange={event => setDraft({ ...draft, message: event.target.value })} onKeyDown={event => { if (event.key === 'Enter' && draft.trigger_at && draft.message.trim()) void create(); }} placeholder={t('到点要提醒什么？')} /></Field><button className="primary" disabled={!draft.trigger_at || !draft.message.trim()} onClick={() => void create()}><AlarmClock size={15} />{t('设定闹钟')}</button></div>
    <div className="alarm-summary"><Clock3 size={16} /><span>{summary}</span><button className="icon-btn" onClick={() => void reload()} title={t('刷新闹钟')} aria-label={t('刷新闹钟')}><RefreshCw size={14} /></button></div>
    <div className="record-list alarm-list">{alarms.length ? alarms.map((alarm: Json) => <article className="record alarm-record" key={alarm.id}><div className="alarm-time"><strong>{new Date(alarm.trigger_at).toLocaleDateString([], { month: 'short', day: 'numeric' })}</strong><b>{new Date(alarm.trigger_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</b></div><div className="record-main"><span className="alarm-due">{timeFromNow(alarm.trigger_at)}</span><b>{alarm.message}</b><small>{repeatLabel(alarm.repeat_seconds)}{' · '}{alarm.id}</small></div><button className="danger-icon" title={t('取消闹钟')} aria-label={t('取消闹钟“{{message}}”', { message: alarm.message })} onClick={async () => { if (confirm(t('取消闹钟“{{message}}”？', { message: alarm.message }))) { await api('/api/admin/alarms/' + alarm.id, { method: 'DELETE' }); await reload(); } }}><X size={15} /></button></article>) : <Empty text="还没有闹钟，设定一个需要按时记起的提醒。" />}</div>
  </Panel>;
}

function Logs() {
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [type, setType] = useState('');
  const [seqStartDraft, setSeqStartDraft] = useState('');
  const [seqEndDraft, setSeqEndDraft] = useState('');
  const [seqStart, setSeqStart] = useState('');
  const [seqEnd, setSeqEnd] = useState('');
  const [sequenceError, setSequenceError] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [newerCursors, setNewerCursors] = useState<Array<string | null>>([]);
  const [page, setPage] = useState<Json | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const [details, setDetails] = useState<Record<number, Json>>({});
  const [detailError, setDetailError] = useState('');
  const requestVersion = useRef(0);
  const detailVersion = useRef(0);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 320);
    return () => window.clearTimeout(timer);
  }, [query]);

  const applySequenceRange = () => {
    const normalize = (value: string) => value.trim().replace(/^0+(?=\d)/, '');
    const start = normalize(seqStartDraft);
    const end = normalize(seqEndDraft);
    if ((start && !/^\d+$/.test(start)) || (end && !/^\d+$/.test(end))) {
      setSequenceError(t('序列号必须是非负整数。'));
      return;
    }
    if (start && end && (start.length > end.length || (start.length === end.length && start > end))) {
      setSequenceError(t('起始序列不能大于结束序列。'));
      return;
    }
    setSeqStartDraft(start);
    setSeqEndDraft(end);
    setSeqStart(start);
    setSeqEnd(end);
    setSequenceError('');
  };

  useEffect(() => {
    setCursor(null);
    setNewerCursors([]);
    setOpenSeq(null);
    setDetails({});
    setDetailError('');
  }, [type, debouncedQuery, seqStart, seqEnd]);

  useEffect(() => {
    const version = ++requestVersion.current;
    const params = new URLSearchParams({ limit: '100' });
    if (type) params.set('event_type', type);
    if (debouncedQuery) params.set('q', debouncedQuery);
    if (seqStart) params.set('seq_start', seqStart);
    if (seqEnd) params.set('seq_end', seqEnd);
    if (cursor) params.set('cursor', cursor);
    setLoading(true);
    setError('');
    void api<Json>('/api/admin/interactions?' + params.toString())
      .then(result => {
        if (version !== requestVersion.current) return;
        setPage(result);
        setOpenSeq(null);
        setDetails({});
        setDetailError('');
      })
      .catch(reason => {
        if (version !== requestVersion.current) return;
        setError(reason instanceof Error ? reason.message : t('历史记录加载失败'));
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
  }, [cursor, debouncedQuery, refreshKey, seqEnd, seqStart, type]);

  const showOlder = () => {
    const next = typeof page?.next_cursor === 'string' ? page.next_cursor : null;
    if (!next || loading) return;
    setNewerCursors(items => [...items, cursor]);
    setCursor(next);
  };
  const showNewer = () => {
    const previous = newerCursors[newerCursors.length - 1];
    if (previous === undefined || loading) return;
    setNewerCursors(items => items.slice(0, -1));
    setCursor(previous);
  };
  const toggleDetail = async (event: Json) => {
    const seq = Number(event.seq);
    if (!Number.isInteger(seq) || seq < 0) return;
    if (openSeq === seq) {
      setOpenSeq(null);
      return;
    }
    setOpenSeq(seq);
    setDetailError('');
    if (details[seq]) return;
    const version = ++detailVersion.current;
    try {
      const result = await api<Json>('/api/admin/interactions/' + seq);
      if (version !== detailVersion.current) return;
      setDetails(current => ({ ...current, [seq]: result }));
    } catch (reason) {
      if (version !== detailVersion.current) return;
      setDetailError(reason instanceof Error ? reason.message : t('日志详情加载失败'));
    }
  };
  const events = [...(page?.events || [])].reverse();
  const hasOlder = Boolean(page?.next_cursor);
  const loadedLabel = page?.has_more
    ? t('本页 {{count}} 条，可继续向更早的记录回溯。', { count: events.length })
    : t('本页 {{count}} 条，已到最早记录。', { count: events.length });
  const sequenceScope = seqStart && seqEnd
    ? t('序列 {{start}} 至 {{end}}', { start: seqStart, end: seqEnd })
    : seqStart
      ? t('序列从 {{start}} 起', { start: seqStart })
      : seqEnd
        ? t('序列截至 {{end}}', { end: seqEnd })
        : '';
  const sequenceSummary = page?.sequence;
  const sequenceTotal = Number(sequenceSummary?.total);
  const sequenceFirst = Number(sequenceSummary?.first);
  const sequenceLatest = Number(sequenceSummary?.latest);
  const lifetimeSequenceLabel = page && Number.isInteger(sequenceTotal) && sequenceTotal > 0
    && Number.isInteger(sequenceFirst) && Number.isInteger(sequenceLatest)
    ? t('总序列 {{count}} · #{{first}}–#{{latest}}', {
      count: sequenceTotal.toLocaleString(), first: sequenceFirst.toLocaleString(), latest: sequenceLatest.toLocaleString(),
    })
    : page ? t('总序列 0') : '';
  const searchContinuation = events.length === 0 && hasOlder && (Boolean(type) || Boolean(debouncedQuery) || Boolean(sequenceScope));
  const continuationLabel = sequenceScope
    ? t('继续查看范围内更早记录')
    : searchContinuation
      ? t('继续搜索更早日志')
      : t('查看更早记录');
  return <Panel
    title="生命全史日志"
    note="按序列范围直接定位 interactions.jsonl 与轮转分片；每次只加载一个轻量页面。"
    action={<form className="log-filters history-log-filters" onSubmit={event => { event.preventDefault(); applySequenceRange(); }}>
      <select aria-label={t('筛选事件类型')} value={type} onChange={event => setType(event.target.value)}>
        <option value="">{t('全部事件')}</option>
        <option>message_in</option><option>thinking_start</option><option>llm_response</option><option>tool_call</option><option>tool_result</option><option>system_prompt</option><option>summary_llm_response</option><option>vision_llm_response</option><option>mem0_llm_response</option><option>subconscious_done</option>
      </select>
      <input aria-label={t('过滤日志内容')} value={query} onChange={event => setQuery(event.target.value)} placeholder={t('过滤内容')} />
      <div className="sequence-range" aria-label={t('序列范围')}>
        <label><span>{t('起始序列')}</span><input aria-label={t('起始序列')} type="number" min="0" step="1" inputMode="numeric" value={seqStartDraft} onChange={event => { setSeqStartDraft(event.target.value); setSequenceError(''); }} placeholder="0" /></label>
        <span className="sequence-separator" aria-hidden="true">–</span>
        <label><span>{t('结束序列')}</span><input aria-label={t('结束序列')} type="number" min="0" step="1" inputMode="numeric" value={seqEndDraft} onChange={event => { setSeqEndDraft(event.target.value); setSequenceError(''); }} placeholder={t('当前')} /></label>
        <button className="ghost mini sequence-locate" type="submit">{t('定位序列')}</button>
      </div>
      <button className="icon-btn" type="button" aria-label={t('刷新生命全史日志')} title={t('刷新生命全史日志')} onClick={() => setRefreshKey(value => value + 1)}><RefreshCw size={15} /></button>
    </form>}
  >
    {sequenceError && <div className="notice error history-sequence-error">{sequenceError}</div>}
    <div className="history-navigator">
      <div className="history-position"><span className={cursor ? 'history-marker earlier' : 'history-marker'}><Clock3 size={15} /></span><div><b>{cursor ? t('正在回溯更早的记录') : sequenceScope ? t('已定位到指定序列') : t('最新记录')}</b><div className="history-detail-line"><small>{sequenceScope ? sequenceScope + ' · ' + loadedLabel : loadedLabel}</small>{lifetimeSequenceLabel && <span className="history-sequence-total">{lifetimeSequenceLabel}</span>}</div></div></div>
      <div className="history-actions"><button className="ghost mini" disabled={!newerCursors.length || loading} onClick={showNewer}><ChevronRight size={14} />{t('较新')}</button><button className="ghost mini" disabled={!hasOlder || loading} onClick={showOlder}><ChevronLeft size={14} />{continuationLabel}</button></div>
    </div>
    {loading && !page ? <Loading error={error} /> : error ? <Loading error={error} /> : <div className="log-table lifecycle-log-table"><div className="log-head" aria-hidden="true"><b>{t('时间')}</b><b>{t('事件')}</b><b>{t('内容')}</b></div>{events.length ? events.map((event: Json) => {
      const seq = Number(event.seq);
      const isOpen = openSeq === seq;
      const detail = Number.isInteger(seq) ? details[seq] : null;
      const meta = Object.entries(event.meta || {}).map(([key, value]) => key + ': ' + value).join(' · ');
      return <article key={String(event.seq) + '-' + event.type} className={isOpen ? 'open' : ''}><time>{event.ts ? new Date(event.ts).toLocaleString() : '—'}</time><span className={'event-type ' + event.type}>{event.type}</span><div className="interaction-row-copy"><code title={event.preview}>{event.preview}</code>{meta && <small>{meta}</small>}</div>{Number.isInteger(seq) && <button className="ghost mini interaction-detail-toggle" aria-expanded={isOpen} onClick={() => void toggleDetail(event)}>{isOpen ? t('收起') : t('详情')}</button>}{isOpen && <div className="interaction-detail">{detailError ? <p className="notice error">{detailError}</p> : detail ? <><pre>{JSON.stringify(detail.entry, null, 2)}</pre>{detail.truncated && <small>{t('为了保持页面流畅，这条超长记录已在详情中截断。')}</small>}</> : <div className="bubble-history-loading">{t('正在读取日志详情…')}</div>}</div>}</article>;
    }) : <div className="history-empty"><Empty text={searchContinuation ? '这个扫描窗口里没有符合条件的记录；继续向更早的日志查找。' : '这里还没有交互日志。'} /></div>}</div>}
  </Panel>;
}

function DangerAction({ title, description, button, name, onConfirm }: { title: string; description: string; button: string; name: string; onConfirm: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [done, setDone] = useState('');
  const displayExpected = name || t('未命名');
  return <article className="danger-card"><TriangleAlert size={20} /><div><b>{t(title)}</b><p>{t(description)}</p>{done && <small>{done}</small>}</div><button className="danger-outline" onClick={() => setOpen(true)}>{t(button)}</button>{open && <div className="modal-layer"><div className="confirm-modal"><TriangleAlert size={28} /><h3>{t(title)}</h3><p>{t(description)}</p><Field label={t('输入“{{name}}”以确认', { name: displayExpected })}><input autoFocus value={typed} onChange={event => setTyped(event.target.value)} /></Field><div className="panel-actions"><button className="ghost" onClick={() => { setOpen(false); setTyped(''); }}>{t('取消')}</button><button className="danger-solid" disabled={typed !== displayExpected} onClick={async () => { await onConfirm(); setOpen(false); setTyped(''); setDone(t('操作已提交')); }}>{t(button)}</button></div></div></div>}</article>;
}

function Maintenance({ name }: { name: string }) {
  const backups = useLoad(() => api<Json>('/api/admin/backups'), []);
  return <div className="page-stack"><Panel title="应急备份" note="摘要恢复会把备份压缩后注入 inbox；完整恢复会替换当前短期上下文。"><div className="record-list">{backups.data?.backups?.length ? backups.data.backups.map((backup: Json) => <article className="record" key={backup.filename}><div><b>{backup.filename}</b><small>{backup.timestamp ? new Date(backup.timestamp).toLocaleString() : t('时间未知')}{' · '}{t('{{count}} 条消息', { count: backup.message_count ?? '—' })}</small></div><div className="row-actions"><button className="ghost" onClick={async () => { if (confirm(t('以摘要方式吸收备份 {{filename}}？', { filename: backup.filename }))) { await api('/api/admin/backups/restore', { method: 'POST', body: JSON.stringify({ filename: backup.filename, mode: 'summarize' }) }); } }}>{t('摘要恢复')}</button><BackupFullRestore filename={backup.filename} name={name} /></div></article>) : <Empty text="当前没有应急备份。" />}</div></Panel><Panel title="维护舱" note="重启会改变运行状态，因此需要明确确认。"><div className="danger-list"><DangerAction title="安全重启 Coworker" description="保存完整短期快照并重启进程。正在运行的 Bubble 会被取消，页面连接会短暂断开。" button="安全重启" name={name} onConfirm={() => api('/api/admin/restart', { method: 'POST', body: JSON.stringify({ confirm_name: name || '未命名' }) })} /></div></Panel></div>;
}

function BackupFullRestore({ filename, name }: { filename: string; name: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const expected = name || '未命名';
  const displayExpected = name || t('未命名');
  return <><button className="danger-outline" onClick={() => setOpen(true)}>{t('完整恢复')}</button>{open && <div className="modal-layer"><div className="confirm-modal"><TriangleAlert size={28} /><h3>{t('完整恢复备份')}</h3><p>{t('用 {{filename}} 替换当前短期上下文；现有上下文会被覆盖。', { filename })}</p><Field label={t('输入“{{name}}”以确认', { name: displayExpected })}><input autoFocus value={typed} onChange={event => setTyped(event.target.value)} /></Field><div className="panel-actions"><button className="ghost" onClick={() => setOpen(false)}>{t('取消')}</button><button className="danger-solid" disabled={typed !== displayExpected} onClick={async () => { await api('/api/admin/backups/restore', { method: 'POST', body: JSON.stringify({ filename, mode: 'full', confirm_name: expected }) }); setOpen(false); }}>{t('完整恢复')}</button></div></div></div>}</>;
}

function Identity({ onName }: { onName: (name: string) => void }) {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/identity'), []);
  const [draft, setDraft] = useState<Json | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (data) setDraft({ ...data }); }, [data]);
  if (loading || !draft) return <Loading error={error} />;
  const save = async () => {
    const result = await api<Json>('/api/admin/identity', { method: 'PUT', body: JSON.stringify(draft) });
    onName(result.name || '');
    setSaved(true);
    await reload();
  };
  return <Panel title="身份档案" note="修改会直接写入身份文件，并从下一次思考起进入系统提示。"><div className="identity-form"><Field label="姓名"><input value={draft.name || ''} onChange={event => setDraft({ ...draft, name: event.target.value })} /></Field><Field label="现居地"><input value={draft.current_location || ''} onChange={event => setDraft({ ...draft, current_location: event.target.value })} /></Field><Field label="人格"><textarea value={draft.personality || ''} onChange={event => setDraft({ ...draft, personality: event.target.value })} /></Field><Field label="当前目标"><textarea value={draft.goals || ''} onChange={event => setDraft({ ...draft, goals: event.target.value })} /></Field><Field label="人生经历"><textarea className="tall" value={draft.life_story || ''} onChange={event => setDraft({ ...draft, life_story: event.target.value })} /></Field></div>{saved && <div className="notice success">{t('身份档案已更新。')}</div>}<div className="panel-actions"><button className="primary" onClick={() => void save()}><Save size={15} />{t('保存档案')}</button></div></Panel>;
}

type ContentKind = 'skills' | 'palaces' | 'subconscious';
const CONTENT_KIND: Record<ContentKind, { label: string; filename: string; description: string }> = {
  skills: { label: 'Skill', filename: 'SKILL.md', description: '可调用的工作方法与操作流程' },
  palaces: { label: 'Palace', filename: 'PALACE.md', description: '按情境挂载的领域知识入口' },
  subconscious: { label: '潜意识', filename: 'MODE.md', description: '后台触发的观察与思考模式' },
};

const CONTENT_SOURCE_GUIDE: Record<ContentKind, { required: string; tip: string }> = {
  skills: { required: 'name · description', tip: '正文写清触发条件、执行步骤和完成标准' },
  palaces: { required: 'name · when_to_attach', tip: '技能与标签使用 YAML 数组，例如 [product, testing]' },
  subconscious: { required: 'name · trigger · purpose', tip: 'trigger 可用 periodic、garden、cold_floor 或 manual' },
};

function contentTemplate(kind: ContentKind) {
  if (kind === 'palaces') return '---\nname: \nwhen_to_attach: \ncritical_skills: []\nrelated_skills: []\nmemory_tags: []\n---\n\n# ' + t('领域说明') + '\n\n';
  if (kind === 'subconscious') return '---\nname: \nenabled: true\ntrigger: periodic\ncontext_builder: short_term\nevery_n_cycles: 40\nevery_seconds: 1800\nevery_n_tool_calls: 0\nmax_cycles: 5\ngoal: \npurpose: \n---\n\n# ' + t('思考方式') + '\n\n';
  return '---\nname: \ndescription: \nversion: 1.0.0\n---\n\n# ' + t('使用说明') + '\n\n';
}

function draftMeta(raw: string) {
  const parts = raw.startsWith('---') ? raw.split('---', 3) : [];
  const frontmatter = parts[1] || '';
  const read = (key: string) => frontmatter.match(new RegExp(`^${key}:\\s*(.*)$`, 'm'))?.[1]?.trim().replace(/^['"]|['"]$/g, '') || '';
  const description = read('description'); const whenToAttach = read('when_to_attach'); const purpose = read('purpose'); const goal = read('goal');
  return { name: read('name'), description, whenToAttach, purpose, trigger: read('trigger'), summary: description || whenToAttach || purpose || goal, lines: raw ? raw.split('\n').length : 0 };
}

function ContentManager() {
  const [kind, setKind] = useState<ContentKind>('skills');
  const [selected, setSelected] = useState('');
  const [raw, setRaw] = useState('');
  const [originalRaw, setOriginalRaw] = useState('');
  const [newId, setNewId] = useState('');
  const [query, setQuery] = useState('');
  const [message, setMessage] = useState('');
  const [actionError, setActionError] = useState('');
  const [activeFile, setActiveFile] = useState('');
  const [fileList, setFileList] = useState<Json[]>([]);
  const [newFile, setNewFile] = useState('');
  const [addingFile, setAddingFile] = useState(false);
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/content/' + kind), [kind]);
  const dirty = raw !== originalRaw;
  const meta = useMemo(() => draftMeta(raw), [raw]);
  const items = useMemo(() => (data?.items || []).filter((item: Json) => (item.id + ' ' + item.name + ' ' + item.summary).toLowerCase().includes(query.trim().toLowerCase())), [data, query]);
  const kindLabel = t(CONTENT_KIND[kind].label);
  const kindDescription = t(CONTENT_KIND[kind].description);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  const canLeave = () => !dirty || confirm(t('当前内容尚未保存，确定放弃修改？'));
  const choose = (id: string) => {
    if (id === selected || !canLeave()) return;
    const item = data?.items.find((entry: Json) => entry.id === id);
    const primary = CONTENT_KIND[kind].filename;
    setSelected(id); setNewId(''); setActiveFile(primary); setFileList(item?.files || []); setRaw(item?.raw || ''); setOriginalRaw(item?.raw || ''); setMessage(''); setActionError(''); setAddingFile(false);
  };
  const changeKind = (next: ContentKind) => {
    if (next === kind || !canLeave()) return;
    setKind(next); setSelected(''); setNewId(''); setActiveFile(''); setFileList([]); setRaw(''); setOriginalRaw(''); setQuery(''); setMessage(''); setActionError('');
  };
  const startNew = () => {
    if (!canLeave()) return;
    setSelected(''); setNewId(''); setActiveFile(CONTENT_KIND[kind].filename); setFileList([]); setRaw(contentTemplate(kind)); setOriginalRaw(''); setMessage(''); setActionError('');
  };
  const reloadFiles = async (id = selected) => {
    if (!id) return;
    const result = await api<Json>('/api/admin/content/' + kind + '/' + encodeURIComponent(id) + '/files');
    setFileList(result.files || []);
  };
  const selectFile = async (path: string) => {
    if (!selected || path === activeFile || !canLeave()) return;
    setActionError(''); setMessage('');
    try {
      if (path === CONTENT_KIND[kind].filename) {
        const item = data?.items.find((entry: Json) => entry.id === selected);
        setRaw(item?.raw || ''); setOriginalRaw(item?.raw || '');
      } else {
        const result = await api<Json>('/api/admin/content/' + kind + '/' + encodeURIComponent(selected) + '/files/' + path.split('/').map(encodeURIComponent).join('/'));
        setRaw(result.content || ''); setOriginalRaw(result.content || '');
      }
      setActiveFile(path);
    } catch (error) { setActionError(error instanceof Error ? error.message : t('文件读取失败')); }
  };
  const save = async () => {
    const id = selected || newId.trim(); if (!id) return;
    setActionError(''); setMessage('');
    try {
      const isPrimary = !activeFile || activeFile === CONTENT_KIND[kind].filename;
      const path = isPrimary
        ? '/api/admin/content/' + kind + '/' + encodeURIComponent(id)
        : '/api/admin/content/' + kind + '/' + encodeURIComponent(id) + '/files/' + activeFile.split('/').map(encodeURIComponent).join('/');
      await api(path, { method: 'PUT', body: JSON.stringify(isPrimary ? { raw } : { content: raw }) });
      setSelected(id); setNewId(''); setActiveFile(activeFile || CONTENT_KIND[kind].filename); setOriginalRaw(raw); setMessage(t(isPrimary ? '已保存并重新加载，新的能力定义现在已生效。' : '文件已保存到能力目录。')); await reload(); await reloadFiles(id);
    } catch (error) { setActionError(error instanceof Error ? error.message : t('保存失败')); }
  };
  const createFile = async () => {
    const path = newFile.trim().replace(/\\/g, '/');
    if (!selected || !path || !canLeave()) return;
    setActionError('');
    try {
      await api('/api/admin/content/' + kind + '/' + encodeURIComponent(selected) + '/files/' + path.split('/').map(encodeURIComponent).join('/'), { method: 'PUT', body: JSON.stringify({ content: '' }) });
      await reloadFiles(); setNewFile(''); setAddingFile(false); setActiveFile(path); setRaw(''); setOriginalRaw(''); setMessage(t('文件已创建，可以开始编辑。'));
    } catch (error) { setActionError(error instanceof Error ? error.message : t('文件创建失败')); }
  };
  const deleteFile = async () => {
    if (!selected || !activeFile || activeFile === CONTENT_KIND[kind].filename) return;
    if (!confirm(t('删除 {{path}}？', { path: selected + '/' + activeFile }))) return;
    await api('/api/admin/content/' + kind + '/' + encodeURIComponent(selected) + '/files/' + activeFile.split('/').map(encodeURIComponent).join('/'), { method: 'DELETE' });
    await reloadFiles();
    const item = data?.items.find((entry: Json) => entry.id === selected);
    const primary = CONTENT_KIND[kind].filename;
    setActiveFile(primary); setRaw(item?.raw || ''); setOriginalRaw(item?.raw || ''); setMessage(t('文件已删除。'));
  };
  const activeItem = data?.items.find((item: Json) => item.id === selected);
  const hasDraft = Boolean(raw || selected || newId);
  const idValid = /^[A-Za-z0-9._-]+$/.test(newId.trim());
  const idTaken = Boolean(data?.items?.some((item: Json) => String(item.id).toLowerCase() === newId.trim().toLowerCase()));
  const requiredSourceReady = kind === 'skills' ? meta.description : kind === 'palaces' ? meta.whenToAttach : meta.trigger && meta.purpose;
  const newSourceReady = Boolean(idValid && !idTaken && meta.name && requiredSourceReady);
  const editorTitle = selected
    ? activeItem?.name || selected
    : hasDraft
      ? t('新建 {{label}}', { label: kindLabel })
      : t('选择一项能力内容');
  const editorNote = hasDraft
    ? activeFile && activeFile !== CONTENT_KIND[kind].filename
      ? t('正在编辑 {{file}}', { file: activeFile })
      : meta.summary || kindDescription
    : t('从左侧选择现有内容，或创建一项新的能力定义。');

  return <div className="content-workspace">
    <section className="capability-strip">
      {(Object.entries(CONTENT_KIND) as Array<[ContentKind, typeof CONTENT_KIND.skills]>).map(([id, info]) => <button className={kind === id ? 'active' : ''} key={id} onClick={() => changeKind(id)}><span>{t(info.label)}</span><b>{id === kind ? data?.items?.length ?? '—' : ''}</b><small>{t(info.description)}</small></button>)}
    </section>
    <div className="content-layout">
      <aside className="content-index">
        <div className="content-index-head"><div><span>{t('{{label}} 能力目录', { label: kindLabel })}</span><b>{t('{{count}} 项能力', { count: data?.items?.length || 0 })}</b></div><button className="icon-btn" title={t('刷新能力目录')} aria-label={t('刷新能力目录')} onClick={() => void reload()}><RefreshCw size={14} /></button></div>
        <label className="content-search"><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('搜索名称或用途')} /></label>
        {loading ? <Loading /> : error ? <Loading error={error} /> : <div className="content-items">{items.length ? items.map((item: Json) => <button className={selected === item.id ? 'active' : ''} key={item.id} onClick={() => choose(item.id)}><span className={'content-health ' + (item.valid ? 'valid' : 'invalid')} /><span className="content-item-copy"><b>{item.name || item.id}</b><small>{item.summary || item.warning || t('尚未填写用途说明')}</small></span>{item.metadata?.protected && <ShieldCheck size={13} />}</button>) : <div className="content-no-result">{query ? t('没有匹配的能力内容') : t('这个分类还是空的')}</div>}</div>}
        <button className="new-content" onClick={startNew}><Plus size={14} />{t('新建 {{label}}', { label: kindLabel })}</button>
      </aside>
      {selected && <aside className="content-files"><header><div><FolderOpen size={15} /><span>{selected}</span></div><button className="icon-btn" title={t('新建文件')} aria-label={t('新建文件')} onClick={() => setAddingFile(!addingFile)}><Plus size={14} /></button></header>{addingFile && <div className="file-create"><input autoFocus value={newFile} onChange={event => setNewFile(event.target.value)} onKeyDown={event => event.key === 'Enter' && void createFile()} placeholder="scripts/check.py" /><button disabled={!newFile.trim()} onClick={() => void createFile()}><Check size={13} /></button></div>}<div className="file-tree">{fileList.map(file => <button key={file.path} className={activeFile === file.path ? 'active' : ''} disabled={!file.editable} title={file.editable ? file.path : t('该文件不支持在线编辑')} onClick={() => void selectFile(file.path)}>{file.primary ? <FileText size={14} /> : <FileCode2 size={14} />}<span><b>{file.path}</b><small>{file.editable ? Number(file.size_bytes).toLocaleString() + ' B' : t('仅展示')}</small></span>{file.primary && <i>{t('主')}</i>}</button>)}</div><footer>{t('仅编辑 UTF-8 文本，不会执行脚本')}</footer></aside>}
      <Panel title={editorTitle} note={editorNote} className="content-editor">
        {hasDraft ? <>
          {!selected && <section className="source-create-card"><div className="source-create-mark"><FileCode2 size={20} /></div><div><span>{t('新建源定义')}</span><h3>{t('创建 {{label}}', { label: kindLabel })}</h3><p>{t('模板已准备好。填写目录 ID，然后直接编辑定义文件。')}</p></div><label><span>{t('目录 ID')}</span><input autoFocus className={newId && (!idValid || idTaken) ? 'invalid' : ''} value={newId} onChange={event => { setNewId(event.target.value); setMessage(''); }} placeholder={kind === 'skills' ? 'release-check' : kind === 'palaces' ? 'product-testing' : 'architecture-review'} /><small>{idTaken ? t('这个 ID 已存在') : !newId || idValid ? t('字母、数字、点、短横线或下划线') : t('ID 含有不支持的字符')}</small></label></section>}
          {activeItem && !activeItem.valid && <div className="notice error"><TriangleAlert size={16} />{activeItem.warning}</div>}
          <div className="source-workbench">
            <div className="source-toolbar"><div className="editor-file"><span className={'source-status ' + (dirty ? 'dirty' : '')} title={dirty ? t('有未保存修改') : t('内容已同步')} /><FileText size={14} /><code><b>{selected || newId || t('目录-id')}</b><i>/</i>{activeFile || CONTENT_KIND[kind].filename}</code></div><div className="source-readout"><span>YAML + MD</span><span>UTF-8</span><b>{t('{{count}} 行', { count: meta.lines })}</b><b>{new Blob([raw]).size.toLocaleString()} B</b></div></div>
            <div className="source-schema"><span>{t('必填字段')}</span><code>{CONTENT_SOURCE_GUIDE[kind].required}</code><i /> <p>{t(CONTENT_SOURCE_GUIDE[kind].tip)}</p><kbd>Ctrl S</kbd></div>
            <textarea className="source-editor" value={raw} onChange={event => { setRaw(event.target.value); setMessage(''); }} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if ((selected || newSourceReady) && raw.trim()) void save(); } }} spellCheck={false} aria-label={t('能力内容源码')} />
          </div>
          {actionError && <div className="notice error"><TriangleAlert size={16} />{actionError}</div>}{message && <div className="notice success"><Check size={16} />{message}</div>}
          <div className="panel-actions"><span className={'save-state ' + (dirty ? 'dirty' : '')}>{selected ? (dirty ? t('有未保存修改') : t('内容已同步')) : newSourceReady ? t('定义已就绪') : !idValid || idTaken ? t('填写有效的目录 ID') : t('补全源码中的必填字段')}</span><button className="primary" disabled={selected ? !dirty : (!newSourceReady || !dirty)} onClick={() => void save()}><Save size={15} />{selected ? (activeFile && activeFile !== CONTENT_KIND[kind].filename ? t('保存文件') : t('保存并加载')) : t('创建并加载')}</button>{selected && activeFile !== CONTENT_KIND[kind].filename && <button className="danger-outline" onClick={() => void deleteFile()}><Trash2 size={14} />{t('删除文件')}</button>}{selected && activeFile === CONTENT_KIND[kind].filename && <button className="danger-outline" onClick={async () => { if (confirm(t('删除 {{kind}}/{{id}} 整个能力目录？其中的 scripts、references 和其他附属文件也会一并删除。', { kind, id: selected }))) { await api('/api/admin/content/' + kind + '/' + encodeURIComponent(selected), { method: 'DELETE' }); setSelected(''); setActiveFile(''); setFileList([]); setRaw(''); setOriginalRaw(''); setMessage(''); await reload(); } }}><Trash2 size={14} />{t('删除能力')}</button>}</div>
        </> : <div className="content-welcome"><div className="welcome-orbit"><Sparkles size={28} /><i /><i /></div><h3>{t('{{label}} 能力目录', { label: kindLabel })}</h3><p>{kindDescription}{t('。选择左侧条目查看源码与预览。')}</p><button className="ghost" onClick={startNew}><Plus size={14} />{t('创建第一项内容')}</button></div>}
      </Panel>
    </div>
  </div>;
}

const DESKTOP_PLATFORMS = [
  'windows-x86_64', 'windows-i686', 'windows-aarch64', 'windows-armv7',
  'darwin-x86_64', 'darwin-i686', 'darwin-aarch64', 'darwin-armv7',
  'linux-x86_64', 'linux-i686', 'linux-aarch64', 'linux-armv7',
] as const;
type DesktopPlatform = typeof DESKTOP_PLATFORMS[number];
type DesktopAssetKind = 'updater' | 'installer';
type DesktopAsset = { file: string; signature: string; kind: DesktopAssetKind; size: number; uploaded_at: string };
type DesktopReleaseSummary = { version: string; notes: string; pub_date: string; published: boolean; platforms: string[]; installers: string[]; created_at: string; updated_at: string };
type DesktopRelease = Omit<DesktopReleaseSummary, 'platforms' | 'installers'> & { platforms: Record<string, DesktopAsset>; installers: Record<string, DesktopAsset> };
type DesktopReleaseList = { latest_version: string | null; releases: DesktopReleaseSummary[] };
type QueuedReleaseFile = { id: string; file: File; entryName: string; archiveName: string };
type UploadState = { status: 'idle' | 'uploading' | 'success' | 'error'; error?: string };
type PendingReleaseAsset = {
  id: string; file: QueuedReleaseFile; signatureFile?: QueuedReleaseFile; platform: string; kind: DesktopAssetKind;
  duplicate: boolean; error: string; state: UploadState;
};

const MAX_ZIP_ENTRIES = 128;
const MAX_ZIP_EXPANDED_BYTES = 512 * 1024 * 1024;
let queuedReleaseFileId = 0;

function formatBytes(value = 0) {
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB']; let size = value / 1024; let unit = units[0];
  for (let i = 1; i < units.length && size >= 1024; i += 1) { size /= 1024; unit = units[i]; }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${unit}`;
}

function releaseFileName(value: string) { return value.split(/[\\/]/).filter(Boolean).pop() || ''; }
function isReleaseZip(name: string) { return /\.zip$/i.test(name); }
function isReleaseSignature(name: string) { return /\.sig$/i.test(name); }
function stripSignature(name: string) { return name.replace(/\.sig$/i, ''); }
function isReleaseArtifact(name: string) { return isReleaseSignature(name) || /\.app\.tar\.gz$/i.test(name) || /\.(exe|dmg|appimage|deb|rpm|msi)$/i.test(name); }
function zipU16(view: DataView, offset: number) { return view.getUint16(offset, true); }
function zipU32(view: DataView, offset: number) { return view.getUint32(offset, true); }

function zipEndOffset(view: DataView) {
  const minimum = Math.max(0, view.byteLength - 22 - 0xffff);
  for (let offset = view.byteLength - 22; offset >= minimum; offset -= 1) {
    if (zipU32(view, offset) === 0x06054b50) return offset;
  }
  return -1;
}

function zipTimestamp(date: number, time: number) {
  const value = new Date(((date >> 9) & 0x7f) + 1980, ((date >> 5) & 0x0f) - 1, date & 0x1f, (time >> 11) & 0x1f, (time >> 5) & 0x3f, (time & 0x1f) * 2).getTime();
  return Number.isNaN(value) ? Date.now() : value;
}

async function inflateZip(bytes: ArrayBuffer, expectedSize: number, remaining: number) {
  if (typeof DecompressionStream === 'undefined') throw new Error(t('当前浏览器不支持解压 deflate ZIP，请先解压后上传散文件。'));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw' as CompressionFormat));
  const reader = stream.getReader(); const chunks: Uint8Array[] = []; let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > expectedSize || total > remaining) { await reader.cancel(); throw new Error(t('ZIP 解压大小超过声明值或 512 MiB 限制。')); }
    chunks.push(value);
  }
  if (total !== expectedSize) throw new Error(t('ZIP 条目大小与目录记录不一致。'));
  const output = new Uint8Array(total); let offset = 0;
  chunks.forEach(chunk => { output.set(chunk, offset); offset += chunk.byteLength; });
  return output.buffer;
}

async function extractReleaseZip(file: File): Promise<QueuedReleaseFile[]> {
  const buffer = await file.arrayBuffer(); const view = new DataView(buffer); const end = zipEndOffset(view);
  if (end < 0) throw new Error(t('不是有效的 ZIP 文件。'));
  const entryCount = zipU16(view, end + 10); let offset = zipU32(view, end + 16); let relevantCount = 0; let expandedBytes = 0;
  const extracted: QueuedReleaseFile[] = [];
  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > view.byteLength || zipU32(view, offset) !== 0x02014b50) throw new Error(t('ZIP 中央目录结构异常。'));
    const flags = zipU16(view, offset + 8); const method = zipU16(view, offset + 10); const modTime = zipU16(view, offset + 12); const modDate = zipU16(view, offset + 14);
    const compressedSize = zipU32(view, offset + 20); const uncompressedSize = zipU32(view, offset + 24); const nameLength = zipU16(view, offset + 28);
    const extraLength = zipU16(view, offset + 30); const commentLength = zipU16(view, offset + 32); const localOffset = zipU32(view, offset + 42);
    const entryName = new TextDecoder('utf-8').decode(new Uint8Array(buffer, offset + 46, nameLength));
    offset += 46 + nameLength + extraLength + commentLength;
    if (!entryName || entryName.endsWith('/') || !isReleaseArtifact(entryName)) continue;
    relevantCount += 1; expandedBytes += uncompressedSize;
    if (relevantCount > MAX_ZIP_ENTRIES) throw new Error(t('相关发布文件超过 {{count}} 个。', { count: MAX_ZIP_ENTRIES }));
    if (expandedBytes > MAX_ZIP_EXPANDED_BYTES) throw new Error(t('相关发布文件解压后超过 512 MiB。'));
    if (flags & 0x0001) throw new Error(t('{{name}} 已加密，浏览器无法读取。', { name: entryName }));
    if (compressedSize === 0xffffffff || uncompressedSize === 0xffffffff) throw new Error(t('{{name}} 使用 Zip64，当前页面不支持。', { name: entryName }));
    if (![0, 8].includes(method)) throw new Error(t('{{name}} 使用了不支持的压缩方法 {{method}}。', { name: entryName, method }));
    if (localOffset + 30 > view.byteLength || zipU32(view, localOffset) !== 0x04034b50) throw new Error(t('{{name}} 的本地文件头异常。', { name: entryName }));
    const dataOffset = localOffset + 30 + zipU16(view, localOffset + 26) + zipU16(view, localOffset + 28);
    if (dataOffset + compressedSize > buffer.byteLength) throw new Error(t('{{name}} 的压缩数据不完整。', { name: entryName }));
    const compressed = buffer.slice(dataOffset, dataOffset + compressedSize);
    const content = method === 0 ? compressed : await inflateZip(compressed, uncompressedSize, MAX_ZIP_EXPANDED_BYTES - (expandedBytes - uncompressedSize));
    if (content.byteLength !== uncompressedSize) throw new Error(t('{{name}} 的文件大小不正确。', { name: entryName }));
    extracted.push({
      id: `release-file-${++queuedReleaseFileId}`,
      file: new File([content], releaseFileName(entryName), { type: 'application/octet-stream', lastModified: zipTimestamp(modDate, modTime) }),
      entryName, archiveName: file.name,
    });
  }
  if (!extracted.length) throw new Error(t('ZIP 中没有识别到桌面发布产物。'));
  return extracted;
}

async function expandReleaseFiles(files: File[]) {
  const expanded: QueuedReleaseFile[] = []; const errors: string[] = [];
  for (const file of files) {
    if (!isReleaseZip(file.name)) {
      expanded.push({ id: `release-file-${++queuedReleaseFileId}`, file, entryName: file.name, archiveName: '' });
      continue;
    }
    try { expanded.push(...await extractReleaseZip(file)); }
    catch (error) { errors.push(t('{{file}}：{{error}}', { file: file.name, error: error instanceof Error ? error.message : t('解压失败') })); }
  }
  return { expanded, errors };
}

function releaseFileContext(file: QueuedReleaseFile) { return [file.file.name, file.entryName, file.archiveName].filter(Boolean).join(' '); }

function inferReleasePlatform(file: QueuedReleaseFile): string {
  const context = releaseFileContext(file).toLowerCase(); const compact = context.replace(/[\s._-]+/g, '');
  const arm64 = /aarch64|arm64|applesilicon/.test(compact); const armv7 = /armv7|armhf/.test(compact); const x86 = /i686|x86(?!64)/.test(compact);
  const arch = arm64 ? 'aarch64' : armv7 ? 'armv7' : x86 ? 'i686' : 'x86_64';
  const name = file.file.name;
  if (/\.dmg$/i.test(name) || /\.app\.tar\.gz$/i.test(name) || /darwin|macos|appledarwin/.test(compact)) return `darwin-${arch}`;
  if (/\.(deb|rpm|appimage)$/i.test(name) || /linux/.test(compact)) return `linux-${arch}`;
  if (/\.(exe|msi)$/i.test(name) || /windows|win32|win64|nsis|setup/.test(compact)) return `windows-${arch}`;
  return '';
}

function inferReleaseKind(file: QueuedReleaseFile): DesktopAssetKind { return /\.(dmg|deb|rpm|msi)$/i.test(file.file.name) ? 'installer' : 'updater'; }

function releaseMatchKeys(file: QueuedReleaseFile) {
  const names = [file.entryName, file.file.name].filter(Boolean).map(stripSignature);
  return Array.from(new Set(names.flatMap(name => file.archiveName ? [`${file.archiveName}\n${name}`, name] : [name]).map(value => value.toLowerCase())));
}

function releaseVersions(files: QueuedReleaseFile[]) {
  const versions = new Set<string>(); const re = /(?:^|[^0-9A-Za-z])v?((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?=$|[^0-9A-Za-z])/g;
  files.forEach(file => { let match: RegExpExecArray | null; const context = releaseFileContext(file); while ((match = re.exec(context)) !== null) versions.add(match[1]); });
  return Array.from(versions).sort();
}

function buildPendingReleaseAssets(files: QueuedReleaseFile[], overrides: Record<string, Partial<Pick<PendingReleaseAsset, 'platform' | 'kind'>>>, states: Record<string, UploadState>) {
  const signatures = files.filter(file => isReleaseSignature(file.file.name)); const usedSignatures = new Set<string>();
  const signatureMap = new Map<string, QueuedReleaseFile[]>();
  signatures.forEach(file => releaseMatchKeys(file).forEach(key => signatureMap.set(key, [...(signatureMap.get(key) || []), file])));
  const rows = files.filter(file => !isReleaseSignature(file.file.name)).map(file => {
    let signatureFile: QueuedReleaseFile | undefined;
    for (const key of releaseMatchKeys(file)) { const matches = signatureMap.get(key) || []; if (matches.length === 1) { signatureFile = matches[0]; break; } }
    if (signatureFile) usedSignatures.add(signatureFile.id);
    return {
      id: file.id, file, signatureFile,
      platform: overrides[file.id]?.platform ?? inferReleasePlatform(file),
      kind: overrides[file.id]?.kind ?? inferReleaseKind(file),
      duplicate: false, error: '', state: states[file.id] || { status: 'idle' as const },
    };
  });
  const counts = new Map<string, number>();
  rows.forEach(row => { if (row.platform) { const key = `${row.kind}:${row.platform}`; counts.set(key, (counts.get(key) || 0) + 1); } });
  rows.forEach(row => {
    row.duplicate = Boolean(row.platform && (counts.get(`${row.kind}:${row.platform}`) || 0) > 1);
    row.error = !row.file.file.size ? t('文件为空') : !row.platform ? t('无法识别平台') : row.duplicate ? t('同类型平台重复') : row.kind === 'updater' && !row.signatureFile ? t('缺少同名 .sig') : '';
  });
  return { rows, orphanSignatures: signatures.filter(file => !usedSignatures.has(file.id)) };
}

function ReleaseAssetLane({ version, title, note, assets }: { version: string; title: string; note: string; assets: Record<string, DesktopAsset> }) {
  const entries = Object.entries(assets || {});
  return <section className="release-asset-lane"><header><div><b>{t(title)}</b><small>{t(note)}</small></div><span>{entries.length}</span></header>{entries.length ? <div>{entries.map(([platform, asset]) => <article key={platform}><div className="asset-platform"><i /> <span>{platform}</span></div><div className="asset-file"><b title={asset.file}>{asset.file}</b><small>{formatBytes(asset.size)}{' · '}{asset.uploaded_at ? new Date(asset.uploaded_at).toLocaleString() : t('时间未知')}</small></div><a href={'/api/desktop-updates/assets/' + encodeURIComponent(version) + '/' + encodeURIComponent(asset.file)} title={t('下载 {{file}}', { file: asset.file })}><Download size={14} /></a></article>)}</div> : <p className="release-lane-empty">{t('还没有这类产物')}</p>}</section>;
}

function DesktopReleases() {
  const releases = useLoad(() => api<DesktopReleaseList>('/api/desktop-updates/releases'), []);
  const [selectedVersion, setSelectedVersion] = useState('');
  const [detail, setDetail] = useState<DesktopRelease | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [creating, setCreating] = useState(false);
  const [newVersion, setNewVersion] = useState('');
  const [newNotes, setNewNotes] = useState('');
  const [creatingBusy, setCreatingBusy] = useState(false);
  const [queued, setQueued] = useState<QueuedReleaseFile[]>([]);
  const [overrides, setOverrides] = useState<Record<string, Partial<Pick<PendingReleaseAsset, 'platform' | 'kind'>>>>({});
  const [uploadStates, setUploadStates] = useState<Record<string, UploadState>>({});
  const [parsing, setParsing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [actionError, setActionError] = useState('');
  const [message, setMessage] = useState('');
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>([]);
  const [latestPlatforms, setLatestPlatforms] = useState<string[]>([]);

  const openRelease = useCallback(async (version: string) => {
    setCreating(false); setSelectedVersion(version); setDetailLoading(true); setDetailError(''); setActionError(''); setMessage('');
    setQueued([]); setOverrides({}); setUploadStates({});
    try { setDetail(await api<DesktopRelease>('/api/desktop-updates/releases/' + encodeURIComponent(version))); }
    catch (error) { setDetail(null); setDetailError(error instanceof Error ? error.message : t('版本读取失败')); }
    finally { setDetailLoading(false); }
  }, []);

  useEffect(() => {
    if (!releases.data || creating || selectedVersion) return;
    const initial = releases.data.latest_version || releases.data.releases[0]?.version;
    if (initial) void openRelease(initial); else setCreating(true);
  }, [releases.data, creating, selectedVersion, openRelease]);

  const latestSummary = useMemo(() => releases.data?.releases.find(item => item.version === releases.data?.latest_version), [releases.data]);
  const latestPlatformKey = latestSummary?.platforms.join('|') || '';
  useEffect(() => {
    let active = true;
    const latest = releases.data?.latest_version;
    if (!latest || !latestSummary) { setLatestPlatforms([]); return; }
    void Promise.all(latestSummary.platforms.map(async platform => {
      const [target, arch] = platform.split('-', 2);
      try {
        const result = await api<Json>('/api/desktop-updates/' + target + '/' + arch + '/0.0.0');
        return result.version === latest ? platform : '';
      } catch { return ''; }
    })).then(items => { if (active) setLatestPlatforms(items.filter(Boolean)); });
    return () => { active = false; };
  }, [releases.data?.latest_version, latestPlatformKey, latestSummary]);

  const readyPlatforms = useMemo(() => detail ? Object.entries(detail.platforms || {}).filter(([, asset]) => asset.file && asset.signature).map(([platform]) => platform).sort() : [], [detail]);
  const readyKey = readyPlatforms.join('|');
  useEffect(() => { setSelectedPlatforms(readyPlatforms); }, [detail?.version, readyKey]);

  const pending = useMemo(() => buildPendingReleaseAssets(queued, overrides, uploadStates), [queued, overrides, uploadStates]);
  const versions = useMemo(() => releaseVersions(queued), [queued]);
  const versionError = versions.length > 1
    ? t('文件中识别到多个版本：{{versions}}', { versions: versions.join(t('、')) })
    : versions.length === 1 && detail && versions[0] !== detail.version
      ? t('文件版本 {{fileVersion}} 与当前版本 {{currentVersion}} 不一致', { fileVersion: versions[0], currentVersion: detail.version })
      : '';
  const activeRows = pending.rows.filter(row => row.state.status !== 'success');
  const uploadBlocked = Boolean(versionError || activeRows.some(row => row.error));

  const addFiles = async (files: File[]) => {
    if (!files.length) return;
    setParsing(true); setActionError(''); setMessage('');
    const result = await expandReleaseFiles(files);
    if (result.expanded.length) setQueued(current => [...current, ...result.expanded]);
    if (result.errors.length) setActionError(result.errors.join('\n'));
    setParsing(false);
  };

  const removeQueued = (id: string) => {
    setQueued(current => current.filter(file => file.id !== id));
    setOverrides(current => { const next = { ...current }; delete next[id]; return next; });
    setUploadStates(current => { const next = { ...current }; delete next[id]; return next; });
  };

  const uploadOne = async (row: PendingReleaseAsset) => {
    if (!detail || row.error) return false;
    setUploadStates(current => ({ ...current, [row.id]: { status: 'uploading' } }));
    try {
      const form = new FormData();
      form.set('platform', row.platform);
      form.set('kind', row.kind);
      form.set('file', row.file.file);
      form.set('signature', row.kind === 'updater' && row.signatureFile ? (await row.signatureFile.file.text()).trim() : '');
      const updated = await api<DesktopRelease>('/api/desktop-updates/releases/' + encodeURIComponent(detail.version) + '/assets', { method: 'POST', body: form });
      setDetail(updated); setUploadStates(current => ({ ...current, [row.id]: { status: 'success' } }));
      return true;
    } catch (error) {
      const errorText = error instanceof Error ? error.message : t('上传失败');
      setUploadStates(current => ({ ...current, [row.id]: { status: 'error', error: errorText } }));
      return false;
    }
  };

  const uploadAll = async () => {
    if (!detail || uploadBlocked || !activeRows.length) return;
    setUploading(true); setActionError(''); setMessage('');
    let succeeded = 0;
    for (const row of activeRows) if (await uploadOne(row)) succeeded += 1;
    await releases.reload(); setUploading(false);
    setMessage(succeeded === activeRows.length
      ? t('已上传 {{count}} 个产物。', { count: succeeded })
      : t('已上传 {{succeeded}}/{{total}} 个产物，失败项可直接重试。', { succeeded, total: activeRows.length }));
  };

  const createRelease = async (event: FormEvent) => {
    event.preventDefault();
    const version = newVersion.trim().replace(/^v/, '');
    if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
      setActionError(t('版本号必须是 SemVer，例如 0.3.0。'));
      return;
    }
    setCreatingBusy(true); setActionError(''); setMessage('');
    try {
      await api('/api/desktop-updates/releases', { method: 'POST', body: JSON.stringify({ version, notes: newNotes }) });
      await releases.reload(); setNewVersion(''); setNewNotes(''); await openRelease(version); setMessage(t('版本草稿已创建，可以上传产物。'));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await releases.reload(); await openRelease(version); setMessage(t('这个版本已经存在，已打开原有版本且没有覆盖说明。'));
      } else setActionError(error instanceof Error ? error.message : t('版本创建失败'));
    } finally { setCreatingBusy(false); }
  };

  const publish = async (action: 'publish' | 'rollback') => {
    if (!detail || !selectedPlatforms.length) return;
    const isRollback = action === 'rollback';
    const platforms = selectedPlatforms.join(t('、'));
    const prompt = isRollback
      ? t('将 latest 指向 {{version}} 的这些平台：{{platforms}}？\n\n这不会把已经升级的客户端强制降级。', { version: detail.version, platforms })
      : t('发布 {{version}} 到这些平台：{{platforms}}？', { version: detail.version, platforms });
    if (!confirm(prompt)) return;
    setActionError(''); setMessage('');
    try {
      await api('/api/desktop-updates/releases/' + encodeURIComponent(detail.version) + '/' + action, { method: 'POST', body: JSON.stringify({ platforms: selectedPlatforms }) });
      await releases.reload(); setMessage(isRollback ? t('latest 已切换到 {{version}}。', { version: detail.version }) : t('{{version}} 已发布。', { version: detail.version }));
    } catch (error) { setActionError(error instanceof Error ? error.message : t('发布失败')); }
  };

  const releaseItems = releases.data?.releases || [];
  const latest = releases.data?.latest_version;
  const stateLabel = (version: string, published: boolean) => version === latest ? t('当前 latest') : published ? t('曾发布') : t('草稿');
  const heroTitle = latest ? t('v{{version}} 正在投放', { version: latest }) : t('还没有桌面更新');
  const heroNote = latestSummary?.notes || (latest ? t('当前版本已进入自动更新通道。') : t('创建版本并上传签名产物后，从这里开启第一次投放。'));
  return <div className="release-page page-stack">
    <section className={'release-hero ' + (latest ? 'ready' : 'empty')}>
      <div className="release-signal"><Rocket size={25} /><i /><i /></div>
      <div><p className="eyebrow">{t('桌面更新投放')}</p><h2>{heroTitle}</h2><p>{heroNote}</p></div>
      <div className="release-hero-platforms"><span>{t('已投放平台')}</span><div>{latestPlatforms.length ? latestPlatforms.map(platform => <b key={platform}>{platform}</b>) : <small>{latest ? t('正在确认平台…') : t('尚未发布')}</small>}</div>{latestSummary?.updated_at && <time>{new Date(latestSummary.updated_at).toLocaleString()}</time>}</div>
    </section>
    <div className="release-layout">
      <aside className="release-index">
        <header><div><span>{t('版本记录')}</span><b>{t('{{count}} 个版本', { count: releaseItems.length })}</b></div><button className="icon-btn" title={t('刷新版本')} aria-label={t('刷新版本')} onClick={() => void releases.reload()}><RefreshCw size={14} /></button></header>
        <button className={'release-new ' + (creating ? 'active' : '')} onClick={() => { setCreating(true); setSelectedVersion(''); setDetail(null); setQueued([]); setActionError(''); setMessage(''); }}><Plus size={15} /><span><b>{t('新建版本')}</b><small>{t('准备下一次桌面更新')}</small></span></button>
        {releases.loading ? <Loading /> : releases.error ? <Loading error={releases.error} /> : <div className="release-trace">{releaseItems.length ? releaseItems.map(item => { const state = item.version === latest ? 'latest' : item.published ? 'published' : 'draft'; return <button key={item.version} className={(selectedVersion === item.version ? 'active ' : '') + state} onClick={() => void openRelease(item.version)}><span className="trace-node"><i /></span><span className="trace-copy"><span><b>v{item.version}</b><em>{stateLabel(item.version, item.published)}</em></span><small>{item.notes || t('没有发布说明')}</small><span className="trace-platforms">{t('{{updater}} 个自动更新包 · {{installer}} 个安装包', { updater: item.platforms.length, installer: item.installers.length })}</span><time>{item.updated_at ? new Date(item.updated_at).toLocaleDateString() : '—'}</time></span></button>; }) : <div className="release-index-empty">{t('创建第一个桌面版本')}</div>}</div>}
      </aside>
      <main className="release-workspace">
        {creating ? <Panel title="创建桌面版本" note="只建立版本草稿；创建后在同一工作台上传 updater、installer 与签名。" className="release-create"><form onSubmit={createRelease}><Field label="版本号" hint="遵循 SemVer，例如 0.3.0"><input autoFocus value={newVersion} onChange={event => setNewVersion(event.target.value)} placeholder="0.3.0" /></Field><Field label="发布说明" hint="会显示给检查到更新的桌面客户端"><textarea value={newNotes} onChange={event => setNewNotes(event.target.value)} placeholder={t('这次更新解决了什么？')} /></Field><div className="panel-actions"><button className="primary" disabled={!newVersion.trim() || creatingBusy}>{creatingBusy ? t('正在创建…') : t('创建版本草稿')}<ChevronRight size={15} /></button></div></form>{actionError && <div className="notice error"><TriangleAlert size={16} /><span>{actionError}</span></div>}{message && <div className="notice success"><Check size={16} /><span>{message}</span></div>}</Panel>
        : detailLoading ? <Loading /> : detailError ? <Loading error={detailError} /> : detail ? <>
          <Panel title={'v' + detail.version} note={detail.notes || t('这个版本没有发布说明。')} action={<span className={'release-state ' + (detail.version === latest ? 'latest' : detail.published ? 'published' : 'draft')}>{stateLabel(detail.version, detail.published)}</span>} className="release-detail">
            <div className="release-meta"><span><b>{detail.created_at ? new Date(detail.created_at).toLocaleString() : '—'}</b>{t('创建时间')}</span><span><b>{detail.updated_at ? new Date(detail.updated_at).toLocaleString() : '—'}</b>{t('最近更新')}</span><span><b>{Object.keys(detail.platforms || {}).length + Object.keys(detail.installers || {}).length}</b>{t('已存产物')}</span></div>
            <div className="release-asset-grid"><ReleaseAssetLane version={detail.version} title="自动更新包" note="带签名，进入 latest.json" assets={detail.platforms} /><ReleaseAssetLane version={detail.version} title="安装包" note="供首次安装或手动重装" assets={detail.installers} /></div>
          </Panel>
          <Panel title="追加或替换产物" note="拖入 GitHub artifact ZIP 或散文件；识别结果可在上传前修正。" className="release-upload-panel">
            <label className={'release-dropzone ' + (dragging ? 'dragging ' : '') + (parsing ? 'busy' : '')} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void addFiles(Array.from(event.dataTransfer.files)); }}><CloudUpload size={25} /><span><b>{parsing ? t('正在读取产物…') : t('拖入 artifact ZIP 或点击选择文件')}</b><small>{t('自动匹配版本、平台、产物类型和同名 .sig')}</small></span><input type="file" multiple accept=".zip,.sig,.exe,.dmg,.appimage,.deb,.rpm,.msi,.gz" disabled={parsing} onChange={event => { void addFiles(Array.from(event.target.files || [])); event.target.value = ''; }} /></label>
            {queued.length ? <div className="release-queue">
              <header><div><FileArchive size={16} /><span>{t('{{files}} 个文件 · {{assets}} 个产物', { files: queued.length, assets: pending.rows.length })}</span></div><button className="ghost mini" onClick={() => { setQueued([]); setOverrides({}); setUploadStates({}); }}>{t('清空队列')}</button></header>
              {versionError && <div className="notice error"><TriangleAlert size={15} /><span>{versionError}</span></div>}
              <div className="release-queue-rows">{pending.rows.map(row => <article className={(row.error ? 'invalid ' : '') + row.state.status} key={row.id}><div className="queue-file"><b title={row.file.entryName}>{row.file.file.name}</b><small>{formatBytes(row.file.file.size)}{row.file.archiveName ? ' · ' + row.file.archiveName : ''}</small></div><select aria-label={t('{{file}} 的平台', { file: row.file.file.name })} value={row.platform} onChange={event => setOverrides(current => ({ ...current, [row.id]: { ...current[row.id], platform: event.target.value } }))}><option value="">{t('选择平台')}</option>{DESKTOP_PLATFORMS.map(platform => <option key={platform}>{platform}</option>)}</select><select aria-label={t('{{file}} 的类型', { file: row.file.file.name })} value={row.kind} onChange={event => setOverrides(current => ({ ...current, [row.id]: { ...current[row.id], kind: event.target.value as DesktopAssetKind } }))}><option value="updater">{t('自动更新包')}</option><option value="installer">{t('安装包')}</option></select><div className="queue-state">{row.state.status === 'uploading' ? t('上传中…') : row.state.status === 'success' ? t('已上传') : row.state.status === 'error' ? row.state.error : row.error || (row.kind === 'updater' ? t('sig · {{file}}', { file: row.signatureFile?.file.name || '—' }) : t('无需签名'))}</div><div className="queue-actions">{row.state.status === 'error' && !row.error && <button className="ghost mini" onClick={async () => { await uploadOne(row); await releases.reload(); }}>{t('重试')}</button>}<button className="danger-icon" title={t('移除文件')} aria-label={t('移除文件')} onClick={() => removeQueued(row.id)}><X size={13} /></button></div></article>)}</div>
              {pending.orphanSignatures.length > 0 && <div className="orphan-signatures"><span>{t('未匹配签名')}</span>{pending.orphanSignatures.map(file => <button key={file.id} title={t('移除未匹配签名')} onClick={() => removeQueued(file.id)}>{file.file.name}<X size={11} /></button>)}</div>}
              <div className="panel-actions"><span className="queue-summary">{uploadBlocked ? t('先处理红色项目') : activeRows.length ? t('{{count}} 个产物可上传', { count: activeRows.length }) : t('队列已完成')}</span><button className="primary" disabled={uploadBlocked || !activeRows.length || uploading} onClick={() => void uploadAll()}><CloudUpload size={15} />{uploading ? t('正在上传…') : t('上传全部产物')}</button></div>
            </div> : <div className="release-upload-hint"><span>{t('支持')}</span><code>*.exe + *.sig</code><code>*.app.tar.gz + *.sig</code><code>*.AppImage + *.sig</code><code>*.dmg / *.deb</code></div>}
            {actionError && <div className="notice error"><TriangleAlert size={16} /><span>{actionError}</span></div>}{message && <div className="notice success"><Check size={16} /><span>{message}</span></div>}
          </Panel>
          <Panel title="投放自动更新" note="只发布已上传且签名完整的自动更新包；同版本可稍后补齐其他平台。" className="release-publish-panel">
            {readyPlatforms.length ? <><div className="publish-platforms">{readyPlatforms.map(platform => <label key={platform}><input type="checkbox" checked={selectedPlatforms.includes(platform)} onChange={event => setSelectedPlatforms(current => event.target.checked ? [...current, platform] : current.filter(item => item !== platform))} /><i><Check size={12} /></i><span>{platform}</span></label>)}</div><div className="publish-note"><TriangleAlert size={15} /><span>{t('回滚只会改变服务端 latest；已经安装更高版本的客户端不会自动降级。')}</span></div><div className="panel-actions"><button className="primary" disabled={!selectedPlatforms.length} onClick={() => void publish('publish')}><Rocket size={15} />{detail.version === latest ? t('重新发布所选平台') : t('发布所选平台')}</button>{detail.published && detail.version !== latest && <button className="danger-outline" disabled={!selectedPlatforms.length} onClick={() => void publish('rollback')}><RotateCcw size={14} />{t('回滚到此版本')}</button>}</div></> : <div className="release-publish-empty"><PackageOpen size={22} /><span>{t('先上传至少一个带签名的 updater，才能发布自动更新。')}</span></div>}
          </Panel>
        </> : <Empty text="选择一个版本查看发布详情。" />}
      </main>
    </div>
  </div>;
}

function Audit() {
  const audit = useLoad(() => api<Json>('/api/admin/audit?limit=300'), []);
  const diagnostics = useLoad(() => api<Json>('/api/admin/diagnostics/tasks'), []);
  const [tab, setTab] = useState<'audit' | 'runtime'>('audit');
  const [query, setQuery] = useState('');
  const [result, setResult] = useState('');
  const entries = audit.data?.entries || [];
  const today = new Date().toDateString();
  const todayCount = entries.filter((entry: Json) => new Date(entry.ts).toDateString() === today).length;
  const failed = entries.filter((entry: Json) => entry.result !== 'ok').length;
  const sources = new Set(entries.map((entry: Json) => entry.source).filter(Boolean)).size;
  const filtered = entries.filter((entry: Json) => {
    const matchesResult = !result || (result === 'ok' ? entry.result === 'ok' : entry.result !== 'ok');
    return matchesResult && (!query || JSON.stringify(entry).toLowerCase().includes(query.toLowerCase()));
  });
  const refresh = () => tab === 'audit' ? audit.reload() : diagnostics.reload();
  return <div className="audit-workspace">
    <section className="audit-vitals">
      <article><ShieldCheck size={17} /><span>{t('今日操作')}</span><b>{todayCount}</b><small>{t('最近保留 {{count}} 条', { count: entries.length })}</small></article>
      <article className={failed ? 'alert' : ''}><TriangleAlert size={17} /><span>{t('异常结果')}</span><b>{failed}</b><small>{failed ? t('需要检查失败记录') : t('没有操作失败')}</small></article>
      <article><TerminalSquare size={17} /><span>{t('活跃任务')}</span><b>{diagnostics.data?.pending ?? '—'}</b><small>{t('事件循环中的等待任务')}</small></article>
      <article><Fingerprint size={17} /><span>{t('操作来源')}</span><b>{sources}</b><small>{t('不同客户端地址')}</small></article>
    </section>
    <div className="audit-switcher"><div><button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}><ShieldCheck size={15} />{t('操作时间线')}</button><button className={tab === 'runtime' ? 'active' : ''} onClick={() => setTab('runtime')}><Activity size={15} />{t('运行诊断')}</button></div><button className="icon-btn" title={t('刷新当前视图')} aria-label={t('刷新当前视图')} onClick={() => void refresh()}><RefreshCw size={15} /></button></div>
    {tab === 'audit' ? <Panel title="管理员操作时间线" note="只记录操作元数据，不包含令牌、密钥和完整正文。" className="audit-panel">
      <div className="audit-filters"><label><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('搜索操作、目标或来源')} /></label><select value={result} onChange={event => setResult(event.target.value)}><option value="">{t('全部结果')}</option><option value="ok">{t('仅成功')}</option><option value="failed">{t('仅异常')}</option></select><span>{t('{{count}} 条记录', { count: filtered.length })}</span></div>
      {!audit.data ? <Loading error={audit.error} /> : filtered.length ? <div className="audit-timeline">{filtered.map((entry: Json, index: number) => {
        const actionParts = String(entry.action || 'unknown').split('.');
        const area = actionParts.shift() || 'system';
        const action = actionParts.join(' · ') || entry.action;
        return <article key={entry.ts + '-' + index} className={entry.result === 'ok' ? 'ok' : 'failed'}><div className="audit-rail"><i /></div><time><b>{new Date(entry.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</b><span>{new Date(entry.ts).toLocaleDateString()}</span></time><div className="audit-event"><header><span>{area}</span><b>{action}</b><i>{entry.result === 'ok' ? t('成功') : entry.result}</i></header><code>{entry.target || '—'}</code>{entry.detail && <p>{entry.detail}</p>}<footer>{t('来源')} {entry.source || 'unknown'}</footer></div></article>;
      })}</div> : <Empty text="没有符合当前筛选条件的审计记录。" />}
    </Panel> : <Panel title="事件循环诊断" note="pending 通常表示任务正在等待消息或定时器，并不等同于故障。" className="runtime-diagnostics">
      {!diagnostics.data ? <Loading error={diagnostics.error} /> : <><div className="runtime-callout"><Activity size={20} /><div><b>{t('{{count}} 个任务正在等待', { count: diagnostics.data.pending })}</b><span>{t('共采样 {{count}} 个 asyncio task；展开条目查看完整快照。', { count: diagnostics.data.total })}</span></div></div><div className="runtime-task-grid">{diagnostics.data.tasks.map((task: Json, index: number) => <details key={(task.name || 'task') + '-' + index} className={task.current ? 'current' : task.done ? 'done' : ''}><summary><span className="task-signal"><i /></span><div><b>{task.name || 'task-' + index}</b><code>{task.coro || 'unknown coroutine'}</code></div><span className="task-state">{task.current ? t('当前请求') : task.done ? t('已完成') : t('等待中')}</span></summary><div className="task-waiting"><span>{t('等待位置')}</span><code>{task.waiting_at || t('没有 Python 栈，可能尚未开始或正在等待底层 I/O')}</code><pre>{JSON.stringify(task, null, 2)}</pre></div></details>)}</div></>}
    </Panel>}
  </div>;
}

function Empty({ text }: { text: string }) { return <div className="empty"><Wrench size={23} /><p>{t(text)}</p></div>; }

export default function AdminApp() {
  const { language } = useAdminI18n();
  const [ready, setReady] = useState(false);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [bootstrap, setBootstrap] = useState<Json | null>(null);
  const [name, setName] = useState('');
  const [section, setSection] = useState<Section>(sectionFromLocation);
  const [lifeState, setLifeState] = useState<LifeState>('quiet');
  useEffect(() => {
    if (!storedToken()) { setSessionChecked(true); return; }
    api<{ name: string }>('/api/admin/session/verify', { method: 'POST' })
      .then(r => { setName(r.name || ''); setReady(true); })
      .catch(() => sessionStorage.removeItem('coworker-admin-token'))
      .finally(() => setSessionChecked(true));
  }, []);
  useEffect(() => {
    if (!ready) return;
    api<Json>('/api/admin/bootstrap').then(setBootstrap).catch(() => setBootstrap({ required: false }));
  }, [ready]);
  useEffect(() => {
    const syncSection = () => setSection(sectionFromLocation());
    window.addEventListener('popstate', syncSection);
    return () => window.removeEventListener('popstate', syncSection);
  }, []);
  useEffect(() => {
    if (!ready) return;
    let active = true;
    const refreshPresence = async () => {
      try {
        const result = await api<Json>('/api/admin/overview');
        if (!active) return;
        const running = Boolean(result.status?.is_running);
        setLifeState(running ? (result.status?.is_sleeping ? 'resting' : 'live') : 'quiet');
      } catch {
        if (active) setLifeState('quiet');
      }
    };
    void refreshPresence();
    const timer = window.setInterval(() => void refreshPresence(), 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [ready]);
  const current = useMemo(() => NAV.find(x => x.id === section) || NAV[0], [section]);
  const navigate = (next: Section) => {
    if (next === section) return;
    const url = new URL(window.location.href);
    if (next === 'overview') url.searchParams.delete('section');
    else url.searchParams.set('section', next);
    window.history.pushState({}, '', `${url.pathname}${url.search}${url.hash}`);
    setSection(next);
  };
  const lifeLabel = t(lifeState === 'live' ? '生命信号在线' : lifeState === 'resting' ? '安静休息中' : '等待生命信号');
  if (!sessionChecked) return <><AdminLanguageSwitch className="admin-language-toggle-floating" /><main className="admin-login"><div className="state-box"><span className="state-pulse"><i /><i /><i /></span><span>{t('正在确认本地值守状态…')}</span></div></main></>;
  if (!ready) return <Login onReady={n => { setName(n); setReady(true); }} />;
  if (!bootstrap) return <><AdminLanguageSwitch className="admin-language-toggle-floating" /><main className="admin-login"><div className="state-box"><span className="state-pulse"><i /><i /><i /></span><span>{t('正在读取初始化状态…')}</span></div></main></>;
  if (bootstrap.required) return <FirstRun data={bootstrap} onComplete={() => { setBootstrap({ required: false }); location.reload(); }} />;
  return <main className={`admin-shell life-${lifeState}`} data-language={language}>
    <aside className="admin-sidebar">
      <a className="admin-brand" href="/">
        <div className="brand-mark"><Orbit size={22} /><i /></div>
        <div><b>{name || 'Coworker'}</b><span>{t('生命值守台')}</span></div>
      </a>
      <nav aria-label={t('照看室导航')}>
        {NAV_GROUPS.map(group => <div className="nav-group" key={group}>
          <p>{t(group)}</p>
          {NAV.filter(item => item.group === group).map(item => <button type="button" key={item.id} className={section === item.id ? 'active' : ''} aria-current={section === item.id ? 'page' : undefined} title={t(item.description)} onClick={() => navigate(item.id)}><item.icon size={18} /><span>{t(item.label)}</span><ChevronRight className="nav-chevron" size={14} /></button>)}
        </div>)}
      </nav>
      <div className="sidebar-foot">
        <span className="sidebar-presence"><i />{lifeLabel}</span>
        <button type="button" onClick={() => { sessionStorage.removeItem('coworker-admin-token'); location.reload(); }}><LogOut size={16} /><span>{t('退出本次值守')}</span></button>
      </div>
    </aside>
    <section className="admin-main">
      <header className="admin-topbar">
        <div className="topbar-title"><p className="eyebrow">{t('值守控制台')}</p><h1>{t(current.label)}</h1><span>{t(current.description)}</span></div>
        <div className="topbar-actions">
          <AdminLanguageSwitch />
          <div className="shell-life" aria-label={lifeLabel}><div className="life-trace" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /></div><span><i />{lifeLabel}</span></div>
          <a href="/">{t('查看生命体主页')} <ChevronRight size={14} /></a>
        </div>
      </header>
      <div className="admin-content">
        {section === 'overview' && <Overview name={name} />}
        {section === 'models' && <Models />}
        {section === 'settings' && <Settings />}
        {section === 'memory' && <MemoryCenter coworkerName={name} />}
        {section === 'runtime' && <Runtime coworkerName={name} />}
        {section === 'identity' && <Identity onName={setName} />}
        {section === 'content' && <ContentManager />}
        {section === 'releases' && <DesktopReleases />}
        {section === 'audit' && <Audit />}
      </div>
    </section>
  </main>;
}
