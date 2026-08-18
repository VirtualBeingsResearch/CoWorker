import { ClipboardEvent as ReactClipboardEvent, createContext, FormEvent, Fragment, MouseEvent as ReactMouseEvent, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlarmClock, ArchiveRestore, BarChart3, Bot, Brain, CalendarDays, ChevronLeft, ChevronRight, CircleGauge,
  Check, Clock3, CloudUpload, Database, Download, ExternalLink, FileArchive, FileCode2, FileCog, FileText, Fingerprint, FolderOpen, HeartPulse, KeyRound, ListTodo, LogOut,
  MessagesSquare, Orbit, Play, RefreshCw, Save, Search, Settings2, ShieldCheck, SlidersHorizontal,
  Sparkles, TerminalSquare, Trash2, TriangleAlert, Wrench, X, Pencil, Plus, PackageOpen, Rocket, RotateCcw, Users,
} from 'lucide-react';
import './admin.css';
import { settingsPanelLabels, settingsPanelRegistration } from './settings/registry';
import {
  COMMON_MODEL_PRICE_CURRENCIES,
  modelPriceCurrencyDetails,
  validateModelPrices,
} from './settings/modelPricing';
import { EditableCombobox } from './EditableCombobox';
import type { Json } from './settings/types';
import { useSettingsDraft } from './settings/useSettingsDraft';
import { configFieldPresentation } from './settings/configFieldPresentation';
import { AdminLanguageSwitch, t, useAdminI18n } from '../i18n/admin';
import { loadInteractionHistoryPage } from './interactionHistory';
import { createBootstrapReconnectProof, resolveBootstrapAdminTarget, type BootstrapAdminTarget } from './bootstrapReconnect';
import { bootstrapTimezoneAdvice, detectBrowserTimezone } from './bootstrapTimezone';
import { AdminUsageOverview } from './UsageOverview';
import { AdminUsageAnalytics } from './UsageAnalytics';
import { LineNumberTextarea } from './LineNumberTextarea';
import { isTargetBubbleRecord, shouldShowInteractionContextAction } from './interactionNavigation';
import type { UsageStats } from '../api/types';
import {
  formatDate,
  formatDateTime,
  formatTime,
  localDateKey,
  localDateTimeInputToIso,
  pastedLogTimeToInput,
  setServerTimezone,
  timestampMillis,
  toAbsoluteIso,
  toLocalDateTimeInput,
} from '../lib/dateTime';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

type Section = 'overview' | 'usage' | 'memory' | 'models' | 'settings' | 'runtime' | 'identity' | 'people' | 'content' | 'relay' | 'releases' | 'audit';
type Workspace = 'overview' | 'operations' | 'configuration' | 'relationships' | 'advanced';
type RuntimeTab = 'tasks' | 'alarms' | 'logs' | 'maintenance';
type LifeState = 'live' | 'resting' | 'quiet';
type AdminIdentity = { name: string; confirmation_name: string };
type PromptSectionPreview = {
  name: string;
  variable: string;
  content_variable: string;
  full_text: string;
  content: string;
  available: boolean;
  lines: number;
};

const NAV: Array<{ id: Section; label: string; description: string; workspace: Workspace; icon: typeof Activity }> = [
  { id: 'overview', label: '生命总览', description: '状态、模型用量和关键运行指标', workspace: 'overview', icon: HeartPulse },
  { id: 'usage', label: '运行分析', description: 'Token、工具、技能与自主执行结果', workspace: 'overview', icon: BarChart3 },
  { id: 'runtime', label: '运行中心', description: '任务、闹钟、运行账本与维护', workspace: 'operations', icon: Activity },
  { id: 'memory', label: '记忆中心', description: '短期上下文、长期召回与并行思考记录', workspace: 'operations', icon: Database },
  { id: 'audit', label: '诊断与审计', description: '事件循环健康与管理员操作记录', workspace: 'operations', icon: ShieldCheck },
  { id: 'models', label: '模型编排', description: '主线模型、摘要与失败降级链', workspace: 'configuration', icon: Brain },
  { id: 'settings', label: '运行设置', description: '连接、记忆与循环参数', workspace: 'configuration', icon: Settings2 },
  { id: 'identity', label: '身份档案', description: '姓名、现居地和人格', workspace: 'relationships', icon: Fingerprint },
  { id: 'people', label: '通信录', description: '人物与跨信道身份', workspace: 'relationships', icon: Users },
  { id: 'content', label: '能力内容', description: 'Skill、Palace 与潜意识模式', workspace: 'advanced', icon: FileCog },
  { id: 'relay', label: '远程访问', description: '安全连接自托管 Relay', workspace: 'advanced', icon: CloudUpload },
  { id: 'releases', label: '桌面发布', description: '版本、签名产物与更新投放', workspace: 'advanced', icon: PackageOpen },
];
const WORKSPACES: Array<{ id: Workspace; label: string; mobileLabel: string; description: string; icon: typeof Activity; sections: Section[] }> = [
  { id: 'overview', label: '观测', mobileLabel: '总览', description: '状态、用量和关键指标', icon: HeartPulse, sections: ['overview', 'usage'] },
  { id: 'operations', label: '运维', mobileLabel: '运维', description: '任务、记忆与运行诊断', icon: Activity, sections: ['runtime', 'memory', 'audit'] },
  { id: 'configuration', label: '配置', mobileLabel: '配置', description: '模型连接与运行参数', icon: SlidersHorizontal, sections: ['models', 'settings'] },
  { id: 'relationships', label: '关系', mobileLabel: '人物', description: '身份与跨信道人物关系', icon: Users, sections: ['identity', 'people'] },
  { id: 'advanced', label: '扩展', mobileLabel: '高级', description: '能力、远程接入与发布维护', icon: Sparkles, sections: ['content', 'relay', 'releases'] },
];
const DEFAULT_SECTION_BY_WORKSPACE: Record<Workspace, Section> = {
  overview: 'overview',
  operations: 'runtime',
  configuration: 'models',
  relationships: 'identity',
  advanced: 'content',
};

const NavigationGuardContext = createContext<(owner: string, dirty: boolean) => void>(() => undefined);

function useNavigationGuard(owner: string, dirty: boolean) {
  const report = useContext(NavigationGuardContext);
  useEffect(() => {
    report(owner, dirty);
    return () => report(owner, false);
  }, [dirty, owner, report]);
}

function sectionFromLocation(): Section {
  const requested = new URLSearchParams(window.location.search).get('section');
  return NAV.some(item => item.id === requested) ? requested as Section : 'overview';
}

function workspaceForSection(section: Section) {
  const navItem = NAV.find(item => item.id === section) || NAV[0];
  return WORKSPACES.find(workspace => workspace.id === navItem.workspace) || WORKSPACES[0];
}

function sectionHref(next: Section) {
  const url = new URL(window.location.href);
  const current = sectionFromLocation();
  if (next === 'overview') url.searchParams.delete('section');
  else url.searchParams.set('section', next);
  if (next !== 'settings' || current !== 'settings') {
    url.searchParams.delete('group');
    url.searchParams.delete('source');
  }
  if (next !== 'runtime' || current !== 'runtime') {
    url.searchParams.delete('runtime_tab');
    url.searchParams.delete('log_start');
    url.searchParams.delete('log_end');
    url.searchParams.delete('log_type');
    url.searchParams.delete('log_seq');
    url.searchParams.delete('log_q');
    url.searchParams.delete('log_seq_start');
    url.searchParams.delete('log_seq_end');
    url.searchParams.delete('log_cursor');
  }
  if (next !== 'memory' || current !== 'memory') {
    url.searchParams.delete('memory_tab');
    url.searchParams.delete('thought_scope');
    url.searchParams.delete('bubble_id');
  }
  url.hash = '';
  return `${url.pathname}${url.search}${url.hash}`;
}

function modelPricingHref() {
  const url = new URL(window.location.href);
  url.searchParams.set('section', 'settings');
  url.searchParams.set('group', 'llm');
  url.searchParams.delete('source');
  url.hash = 'model-pricing';
  return `${url.pathname}${url.search}${url.hash}`;
}

function runtimeTabFromLocation(): RuntimeTab {
  const requested = new URLSearchParams(window.location.search).get('runtime_tab');
  return requested === 'alarms' || requested === 'logs' || requested === 'maintenance'
    ? requested
    : 'tasks';
}

function logTimeFromLocation(key: 'log_start' | 'log_end'): string {
  const value = new URLSearchParams(window.location.search).get(key) || '';
  return toAbsoluteIso(value);
}

function logTypeFromLocation(): string {
  const value = new URLSearchParams(window.location.search).get('log_type') || '';
  return /^[A-Za-z0-9_.:-]{1,120}$/.test(value) ? value : '';
}

function safeLocationParam(key: string, pattern: RegExp): string {
  const value = new URLSearchParams(window.location.search).get(key) || '';
  return pattern.test(value) ? value : '';
}

function boundedLocationParam(key: string, maxLength: number): string {
  const value = new URLSearchParams(window.location.search).get(key) || '';
  return value.length <= maxLength ? value : '';
}

function logSeqFromLocation(): number | null {
  const value = safeLocationParam('log_seq', /^\d+$/);
  const seq = value ? Number(value) : NaN;
  return Number.isSafeInteger(seq) ? seq : null;
}

function memoryTabFromLocation(): 'short' | 'long' | 'thoughts' {
  const value = new URLSearchParams(window.location.search).get('memory_tab');
  return value === 'long' || value === 'thoughts' ? value : 'short';
}

function thoughtScopeFromLocation(): 'bubbles' | 'subconscious' {
  return new URLSearchParams(window.location.search).get('thought_scope') === 'subconscious'
    ? 'subconscious'
    : 'bubbles';
}

function bubbleIdFromLocation(): string {
  return safeLocationParam('bubble_id', /^bbl_[A-Za-z0-9_-]{1,160}$/);
}

function editableLogTimeValue(value: string): string {
  return value.replace('T', ' ').replace(/\.000$/, '');
}

function logTimeInputValue(value: string): string {
  return editableLogTimeValue(toLocalDateTimeInput(value));
}

function storedToken() { return sessionStorage.getItem('coworker-admin-token') || ''; }

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function generateCommunicationToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `cwct_v1_${bytesToBase64Url(bytes)}`;
}

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

async function downloadApiFile(path: string, filename: string) {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${storedToken()}` },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(typeof body.detail === 'string' ? body.detail : t('下载失败'), response.status);
  }
  const href = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 0);
}

function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const requestId = useRef(0);
  const reload = useCallback(async () => {
    const currentRequest = ++requestId.current;
    setLoading(true); setError('');
    try {
      const result = await loader();
      if (requestId.current === currentRequest) setData(result);
    } catch (e) {
      if (requestId.current === currentRequest) setError(e instanceof Error ? e.message : t('加载失败'));
    } finally {
      if (requestId.current === currentRequest) setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    void reload();
    return () => { requestId.current += 1; };
  }, [reload]);
  return { data, error, loading, reload, setData };
}

function Login({ onReady }: { onReady: (identity: AdminIdentity) => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    sessionStorage.setItem('coworker-admin-token', token);
    try {
      const result = await api<AdminIdentity>('/api/admin/session/verify', { method: 'POST' });
      onReady(result);
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
  anthropic: 'Anthropic', openai: 'OpenAI', deepseek: 'DeepSeek',
  qwen: '通义千问', zhipu: '智谱 GLM', minimax: 'MiniMax', 'opencode-go': 'OpenCode Go',
  openai_compatible: '通用 OpenAI 兼容',
};
const THINKING_EFFORT_OPTIONS = ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
const PROVIDER_DEFAULT_MODELS: Record<string, string> = {
  anthropic: 'claude-sonnet-4-8', openai: 'gpt-5.5', deepseek: 'deepseek-v4-pro',
  qwen: 'qwen3.7-plus', zhipu: 'glm-5.2', minimax: 'MiniMax-M3', 'opencode-go': 'deepseek-v4-flash',
};

function preferredModelFor(providerType: string, models: string[]) {
  const preferred = PROVIDER_DEFAULT_MODELS[providerType];
  return preferred && models.includes(preferred) ? preferred : models[0] || '';
}

const BOOTSTRAP_CONFIG_GROUP_ORDER = ['llm', 'memory', 'agent', 'i18n', 'api', 'relay', 'channel_access', 'wecom', 'weixin', 'telegram', 'desktop_updates'];
const BOOTSTRAP_CONFIG_GROUP_LABELS: Record<string, string> = {
  llm: '模型与 Provider', memory: '记忆系统', agent: 'Agent 循环', i18n: '运行语言', api: 'API 服务', relay: '远程访问', channel_access: '信道访问', wecom: '企业微信', weixin: '微信 Claw', telegram: 'Telegram', desktop_updates: '桌面更新',
};
const BOOTSTRAP_CONFIG_GROUP_NOTES: Record<string, string> = {
  llm: '首个 Provider 连接由上方统一生成；这里可以继续设置输出预算、摘要、视觉与降级链。',
  memory: '短期上下文、压缩树、自动召回、记忆抽取与人物记忆。',
  agent: '目录、轮询、批处理、Bubble、潜意识和主动运行的全部循环参数。',
  i18n: '控制系统 Prompt、工具说明和运行时通知所使用的语言。',
  api: '公开访问地址、内部监听地址、端口、跨域来源与桌面通信凭据。',
  relay: '自托管 Relay 的连接、实例身份与认证参数。',
  channel_access: '所有信道的入站和出站 participant 匹配规则。',
  wecom: '企业微信长连接的启用状态、Bot 身份、密钥与地址。',
  weixin: '个人微信 ClawBot 的全局启用状态；账号配对需初始化后完成。',
  telegram: '每个实例独立保存 Token、长轮询 offset 与已知 chat；同一 chat 可通过多个实例接入。',
  desktop_updates: '桌面发布目录、同步来源、周期、容量限制和 Feed 凭据。',
};
const BOOTSTRAP_CONFIG_EXCLUSIONS = new Set([
  'llm.default_provider', 'llm.default_model', 'llm.managed_providers', 'llm.providers_file', 'llm.runtime_config_file',
  'admin.token', 'admin.config_file', 'desktop_updates.admin_token',
]);
function bootstrapFieldVisible(group: string, key: string) {
  const path = `${group}.${key}`;
  if (BOOTSTRAP_CONFIG_EXCLUSIONS.has(path)) return false;
  return !(group === 'llm' && /_(api_key|base_url)$/.test(key));
}

function bootstrapConfigurationGroups(configuration: Json) {
  const known = BOOTSTRAP_CONFIG_GROUP_ORDER.filter(group => configuration[group] !== undefined);
  const extensions = Object.keys(configuration).filter(group => group !== 'admin' && !BOOTSTRAP_CONFIG_GROUP_ORDER.includes(group));
  return [...known, ...extensions];
}

function bootstrapConfigurationChanges(baseline: Json, draft: Json) {
  const changes: Json = {};
  for (const group of bootstrapConfigurationGroups(draft)) {
    if (group === 'channel_access') {
      if (JSON.stringify(baseline[group] || {}) !== JSON.stringify(draft[group] || {})) changes[group] = structuredClone(draft[group] || {});
      continue;
    }
    const groupChanges: Json = {};
    for (const [key, value] of Object.entries(draft[group] || {})) {
      if (!bootstrapFieldVisible(group, key)) continue;
      if (JSON.stringify(baseline[group]?.[key]) !== JSON.stringify(value)) groupChanges[key] = structuredClone(value);
    }
    if (Object.keys(groupChanges).length) changes[group] = groupChanges;
  }
  return changes;
}

function BootstrapConfigurationEditor({ baseline, value, change, replaceGroup, secretInputs, setSecretInputs, secretStatus, invalidPaths, setJsonValidity, initialGroup = 'llm' }: {
  baseline: Json;
  value: Json;
  change: (group: string, key: string, value: unknown) => void;
  replaceGroup: (group: string, value: Json) => void;
  secretInputs: Record<string, string>;
  setSecretInputs: (value: Record<string, string>) => void;
  secretStatus: Json;
  invalidPaths: Set<string>;
  setJsonValidity: (path: string, valid: boolean) => void;
  initialGroup?: string;
}) {
  const [group, setGroup] = useState(initialGroup);
  const panelRef = useRef<HTMLElement>(null);
  const groups = bootstrapConfigurationGroups(value);
  const fields = Object.entries(value[group] || {}).filter(([key]) => bootstrapFieldVisible(group, key));
  const groupDirty = JSON.stringify(baseline[group] || {}) !== JSON.stringify(value[group] || {})
    || Object.entries(secretInputs).some(([path, secret]) => path.startsWith(`${group}.`) && secret);
  const reset = () => {
    replaceGroup(group, structuredClone(baseline[group] || {}));
    setSecretInputs(Object.fromEntries(Object.entries(secretInputs).filter(([path]) => !path.startsWith(`${group}.`))));
  };
  const CustomSettingsPanel = ['channel_access', 'telegram'].includes(group)
    ? settingsPanelRegistration(group)?.component
    : undefined;
  const setDesktopValidation = useCallback(
    (message: string) => setJsonValidity('desktop_updates', !message),
    [setJsonValidity],
  );
  useEffect(() => {
    panelRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  }, [group]);
  return <div className="bootstrap-config-workbench">
    <nav aria-label={t('完整初始化配置组')}>{groups.map(key => {
      const dirty = JSON.stringify(baseline[key] || {}) !== JSON.stringify(value[key] || {}) || Object.entries(secretInputs).some(([path, secret]) => path.startsWith(`${key}.`) && secret);
      return <button type="button" className={group === key ? 'active' : ''} onClick={() => setGroup(key)} key={key}><span>{t(BOOTSTRAP_CONFIG_GROUP_LABELS[key] || key)}{dirty && <i />}</span><ChevronRight size={13} /></button>;
    })}</nav>
    <section className="bootstrap-config-panel" ref={panelRef}>
      <header><div><b>{t(BOOTSTRAP_CONFIG_GROUP_LABELS[group] || group)}</b><small>{t(BOOTSTRAP_CONFIG_GROUP_NOTES[group] || '')}</small></div><button type="button" className="ghost mini" disabled={!groupDirty} onClick={reset}><RotateCcw size={13} />{t('恢复推荐值')}</button></header>
      {group === 'llm' && <div className="bootstrap-config-managed"><ShieldCheck size={15} /><span><b>{t('首个连接由基础设置管理')}</b><small>{t('Provider、启动模型、API Key 和 Base URL 会以页面上方填写的连接为准。')}</small></span></div>}
      {CustomSettingsPanel ? <CustomSettingsPanel value={value[group] || {}} change={(key, next) => change(group, key, next)} apply={async () => true} dirty={groupDirty} saving={false} request={api} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={secretStatus} /> : group === 'desktop_updates' ? <DesktopUpdateSettings value={value.desktop_updates || {}} change={(key, next) => change('desktop_updates', key, next)} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={secretStatus} onValidationChange={setDesktopValidation} updateUrl={false} /> : <div className="bootstrap-config-grid">{fields.map(([key, fieldValue]) => {
        const path = `${group}.${key}`;
        return <ConfigurationField key={key} path={path} value={fieldValue} change={next => change(group, key, next)} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={secretStatus} setJsonValidity={setJsonValidity} passiveMode={Boolean(value.agent?.passive_mode)} />;
      })}</div>}
      {invalidPaths.size > 0 && Array.from(invalidPaths).some(path => path === group || path.startsWith(`${group}.`)) && <p className="field-error" role="alert">{t('请先修正这个配置组中的 JSON 格式。')}</p>}
    </section>
  </div>;
}

function FirstRun({ data, onComplete }: { data: Json; onComplete: () => void }) {
  const { language } = useAdminI18n();
  const catalogs = data.providers || [];
  const configurationDefaults = data.defaults?.configuration || {};
  const [detectedTimezone] = useState(() => detectBrowserTimezone());
  const [configurationBaseline] = useState<Json>(() => structuredClone(configurationDefaults));
  const initialType = catalogs.some((item: Json) => item.type === 'deepseek') ? 'deepseek' : catalogs[0]?.type || 'openai';
  const [providerType, setProviderType] = useState(initialType);
  const [remoteModels, setRemoteModels] = useState<Record<string, string[]>>({});
  const staticModels: string[] = catalogs.find((item: Json) => item.type === providerType)?.models || [];
  const models: string[] = remoteModels[providerType] || staticModels;
  const preferredModel = preferredModelFor(providerType, models);
  const [model, setModel] = useState(preferredModel);
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [name, setName] = useState('');
  const [discovering, setDiscovering] = useState(false);
  const [discoveryError, setDiscoveryError] = useState('');
  const [configuration, setConfiguration] = useState<Json>(() => structuredClone(configurationBaseline));
  const [configurationSecrets, setConfigurationSecrets] = useState<Record<string, string>>({});
  const [invalidConfigurationPaths, setInvalidConfigurationPaths] = useState<Set<string>>(new Set());
  const [customModelCapabilities, setCustomModelCapabilities] = useState({ tools: false, vision: false, video: false });
  const submitInFlight = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [phase, setPhase] = useState<'form' | 'restarting'>('form');
  const [restartTarget, setRestartTarget] = useState<BootstrapAdminTarget | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedInitialGroup, setAdvancedInitialGroup] = useState('llm');
  const advancedReturnFocus = useRef<HTMLButtonElement | null>(null);
  const normalizedModel = model.trim();
  const normalizedName = name.trim();
  const nameExamples = language === 'en' ? ['Mira', 'Rowan', 'Nova', 'Sol'] : ['阿澈', '星野', 'Nova', 'Mira'];
  const productStyleName = /(?:coworker|co-worker|assistant|bot|助手|助理|机器人)$/i.test(normalizedName);
  const customModel = normalizedModel !== '' && !staticModels.includes(normalizedModel);
  const passiveMode = Boolean(configuration.agent?.passive_mode);
  const serverTimezone = typeof data.server_timezone === 'string' && data.server_timezone.trim()
    ? data.server_timezone.trim()
    : t('未能读取');
  const timezoneAdvice = bootstrapTimezoneAdvice(detectedTimezone);
  const timezoneAdviceText = t('检测到浏览器使用 {{browserTimezone}}。Coworker 不会修改系统时区；若时间显示不一致，建议在容器或启动环境中使用：', {
    browserTimezone: timezoneAdvice.detectedTimezone,
  });
  const changeConfiguration = (group: string, key: string, next: unknown) => setConfiguration(current => ({ ...current, [group]: { ...(current[group] || {}), [key]: next } }));
  const replaceConfigurationGroup = (group: string, next: Json) => setConfiguration(current => ({ ...current, [group]: next }));
  const setConfigurationJsonValidity = useCallback((path: string, valid: boolean) => setInvalidConfigurationPaths(current => {
    const next = new Set(current);
    if (valid) next.delete(path); else next.add(path);
    if (next.size === current.size && Array.from(next).every(item => current.has(item))) return current;
    return next;
  }), []);
  const setPassiveMode = (next: boolean) => changeConfiguration('agent', 'passive_mode', next);
  const openAdvanced = (group: string, trigger: HTMLButtonElement) => {
    advancedReturnFocus.current = trigger;
    setAdvancedInitialGroup(group);
    setAdvancedOpen(true);
  };
  const closeAdvanced = () => {
    setAdvancedOpen(false);
    window.requestAnimationFrame(() => advancedReturnFocus.current?.focus());
  };

  const changeProvider = (nextProvider: string) => {
    const nextModels: string[] = remoteModels[nextProvider] || catalogs.find((item: Json) => item.type === nextProvider)?.models || [];
    setProviderType(nextProvider);
    setModel(preferredModelFor(nextProvider, nextModels));
    setCustomModelCapabilities({ tools: false, vision: false, video: false });
    setDiscoveryError('');
  };

  const discoverModels = async () => {
    if (!apiKey.trim() || discovering) return;
    setDiscovering(true);
    setDiscoveryError('');
    try {
      const result = await api<Json>('/api/admin/model/discover', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          api_key: apiKey.trim(),
          base_url: baseUrl.trim(),
        }),
      });
      const nextModels: string[] = Array.isArray(result.models) ? result.models.map(String) : [];
      setRemoteModels(current => ({ ...current, [providerType]: nextModels }));
      if (nextModels.length) setModel(preferredModelFor(providerType, nextModels));
      if (result.error) setDiscoveryError(String(result.error));
    } catch (discoverError) {
      setDiscoveryError(discoverError instanceof Error ? discoverError.message : t('模型目录拉取失败'));
    } finally {
      setDiscovering(false);
    }
  };

  useEffect(() => {
    if (!advancedOpen) return;
    const previousOverflow = document.body.style.overflow;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeAdvanced();
    };
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [advancedOpen]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitInFlight.current) return;
    submitInFlight.current = true;
    setSubmitting(true);
    setError('');
    try {
      const target = resolveBootstrapAdminTarget(
        window.location.href,
        configurationDefaults.api || {},
        configuration.api || {},
      );
      const reconnectProof = createBootstrapReconnectProof();
      await api('/api/admin/bootstrap', { method: 'POST', body: JSON.stringify({
        provider_type: providerType,
        model: normalizedModel,
        api_key: apiKey,
        base_url: baseUrl,
        coworker_name: normalizedName,
        reconnect_proof: reconnectProof,
        model_capabilities: customModel ? customModelCapabilities : null,
        configuration: bootstrapConfigurationChanges(configurationDefaults, configuration),
        secrets: Object.fromEntries(Object.entries(configurationSecrets).filter(([, value]) => value !== '')),
      }) });
      setRestartTarget(target);
      setPhase('restarting');
      const deadline = Date.now() + 90_000;
      const waitUntilReady = async () => {
        while (Date.now() < deadline) {
          await new Promise(resolve => window.setTimeout(resolve, 1500));
          try {
            if (target.originChanged) {
              const response = await fetch(target.reconnectUrl, { cache: 'no-store' });
              if (!response.ok) throw new Error('Coworker reconnect probe is not ready');
              const probe = await response.json();
              if (probe.proof !== reconnectProof) throw new Error('Coworker reconnect proof mismatch');
              window.location.replace(target.adminUrl);
              return;
            }
            const status = await api<Json>('/api/admin/bootstrap');
            if (!status.required) { onComplete(); return; }
          } catch { /* Restart temporarily closes the connection. */ }
        }
        setError(t(target.originChanged
          ? '新管理员地址暂时无法访问，请稍后通过下方地址打开。'
          : '配置已经保存，但服务仍在重启。请稍后刷新页面。'));
      };
      void waitUntilReady();
    } catch (e) {
      submitInFlight.current = false;
      setSubmitting(false);
      setError(e instanceof Error ? e.message : t('初始化失败'));
    }
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
        {phase === 'restarting' ? <div className="bootstrap-restarting" role="status"><div className="restart-orbit"><Orbit size={34} /><i /><i /></div><p className="access-step">{t('设置步骤 03')}</p><h2>{t('正在带着新配置醒来')}</h2><p>{t(restartTarget?.originChanged ? '管理员访问地址已变更。服务恢复后会自动前往新地址；浏览器会要求你在新地址重新输入管理员令牌。' : '页面会在服务恢复后自动进入照看室，不需要重复填写。')}</p>{restartTarget?.originChanged && <div className="bootstrap-reconnect-target"><span>{t('新的管理员地址')}</span><code>{restartTarget.adminUrl}</code><a className="primary" href={restartTarget.adminUrl}>{t('立即前往新地址')}<ChevronRight size={15} /></a></div>}{error && <p className="form-error" role="alert">{error}</p>}</div> : <>
          <div className="bootstrap-heading"><p className="access-step">{t('设置步骤 02')}</p><h2>{t('配置第一个模型连接')}</h2><p>{t('这些值会写入本地管理配置，不需要创建')} <code>.env</code>{t('。')}</p></div>
          <form className="bootstrap-form" onSubmit={submit}>
            <div className="bootstrap-sections">
              <section className="bootstrap-section">
                <header className="bootstrap-section-head"><span className="bootstrap-section-index">01</span><div><h3>{t('模型连接')}</h3><p>{t('选择一个 Provider，填写 API Key；模型目录可以实时拉取。')}</p></div></header>
                <div className="bootstrap-grid">
                  <label><span>{t('供应商类型')}</span><select value={providerType} onChange={e => changeProvider(e.target.value)}>{catalogs.map((item: Json) => <option value={item.type} key={item.type}>{t(PROVIDER_LABELS[item.type] || item.type)}</option>)}</select></label>
                  <div className="bootstrap-model-field">
                    <label id="bootstrap-model-label" htmlFor="bootstrap-model-input">{t('启动模型')}</label>
                    <EditableCombobox id="bootstrap-model-input" value={model} options={models.map((item: string) => ({ value: item }))} onChange={next => { setModel(next); setCustomModelCapabilities({ tools: false, vision: false, video: false }); }} placeholder={t('选择推荐模型或输入模型 ID')} emptyMessage={t('没有匹配的推荐模型；仍可直接使用当前模型 ID。')} toggleLabel={t('展开推荐模型')} />
                  </div>
                  <label><span>API Key</span><input autoFocus required type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={t('只会保存到本机配置')} autoComplete="new-password" /></label>
                  <label><span>{t('自定义 Base URL')} <em>{t('可选')}</em></span><div className="bootstrap-base-url-row"><input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder={t('使用官方地址时留空')} /><button type="button" className="ghost mini" disabled={!apiKey.trim() || discovering} onClick={() => void discoverModels()}>{t(discovering ? '正在拉取模型目录…' : '拉取模型目录')}</button></div>{discoveryError && <small className="field-error" role="alert">{discoveryError}</small>}</label>
                </div>
                {customModel && <div className="bootstrap-model-warning bootstrap-model-capabilities"><TriangleAlert size={17} /><div><b>{t('声明这个自定义模型的能力')}</b><p>{t('初始化不会发起可能计费的在线探测。请按当前 API 服务的实际能力选择；主模型必须支持工具调用。')}</p><div className="model-capability-toggles">
                  <label><input type="checkbox" checked={customModelCapabilities.tools} onChange={e => setCustomModelCapabilities(current => ({ ...current, tools: e.target.checked }))} /><span>{t('工具调用')}<small>{t('主模型必需')}</small></span></label>
                  <label><input type="checkbox" checked={customModelCapabilities.vision} onChange={e => setCustomModelCapabilities(current => ({ ...current, vision: e.target.checked, video: e.target.checked ? current.video : false }))} /><span>{t('图片理解')}<small>{t('接受图片输入')}</small></span></label>
                  <label><input type="checkbox" checked={customModelCapabilities.video} onChange={e => setCustomModelCapabilities(current => ({ ...current, video: e.target.checked, vision: e.target.checked ? true : current.vision }))} /><span>{t('视频理解')}<small>{t('接受原生视频输入')}</small></span></label>
                </div>{!customModelCapabilities.tools && <p className="field-error" role="alert">{t('当前模型不能作为主模型：请确认它支持工具调用，或选择其他模型。')}</p>}</div></div>}
              </section>
              <section className="bootstrap-section">
                <header className="bootstrap-section-head"><span className="bootstrap-section-index">02</span><div><h3>{t('伙伴身份')}</h3><p>{t('名字可留空，她之后可以自己决定。')}</p></div></header>
                <div className="bootstrap-name-field">
                  <label><span>{t('给新伙伴取个名字')} <em>{t('可选')}</em></span><input value={name} onChange={e => setName(e.target.value)} placeholder={t('例如：阿澈、星野、Nova、Mira')} /></label>
                  <p>{t('像给孩子取名一样，选择一个自然的称呼，不需要添加 Coworker、助手或 Bot 等产品后缀。留空时，她以后也可以自己取名。')}</p>
                  <div className="bootstrap-name-examples" aria-label={t('名字举例，仅作说明')}><small>{t('仅作举例，不是推荐')}</small>{nameExamples.map(example => <span key={example}>{example}</span>)}</div>
                  {productStyleName && <div className="bootstrap-name-warning"><TriangleAlert size={14} />{t('这个名字更像产品标识。可以试试更自然、能直接呼唤的名字。')}</div>}
                </div>
              </section>
              <section className="bootstrap-section">
                <header className="bootstrap-section-head"><span className="bootstrap-section-index">03</span><div><h3>{t('运行偏好')}</h3><p>{t('思考强度、运行语言和启动模式会随本次初始化写入。')}</p></div></header>
                <div className="bootstrap-grid">
                  <label><span>{t('主线思考强度')} <em>{t('可选')}</em></span><select value={configuration.llm?.thinking_effort || ''} onChange={e => changeConfiguration('llm', 'thinking_effort', e.target.value)}>{THINKING_EFFORT_OPTIONS.map(level => <option key={level} value={level}>{level || t('Provider 默认')}</option>)}</select></label>
                  <label><span>{t('运行语言')}</span><select value={configuration.i18n?.locale || 'zh-CN'} onChange={e => changeConfiguration('i18n', 'locale', e.target.value)}><option value="zh-CN">简体中文 (zh-CN)</option><option value="en">English (en)</option></select><small>{t('控制系统 Prompt、工具说明和运行时通知；界面语言用右上角切换')}</small></label>
                </div>
                <div className="bootstrap-runtime-defaults">
                  <div className="bootstrap-runtime-default">
                    <Clock3 size={17} />
                    <span><small className="bootstrap-runtime-label">{t('运行时区')}<em> · {t('由系统环境决定')}</em></small><b><code>{t('服务器')} · {serverTimezone}</code></b>{timezoneAdvice.available && <small className="bootstrap-timezone-guidance" role="note" aria-label={timezoneAdviceText} title={timezoneAdviceText}><TriangleAlert size={10} /><span><span>{t('仅提醒 · 建议')}</span><code>{timezoneAdvice.recommendation}</code></span></small>}</span>
                  </div>
                  <div className="bootstrap-mode-inline" role="radiogroup" aria-label={t('启动模式')}>
                    <span><small>{t('启动模式')}</small><b>{t(passiveMode ? '只响应外部事件 · 面向开发者' : '会自主继续推进 · 推荐给大多数用户')}</b></span>
                    <div>
                      <button type="button" className={!passiveMode ? 'active' : ''} role="radio" aria-checked={!passiveMode} onClick={() => setPassiveMode(false)}>{t('主动模式')}</button>
                      <button type="button" className={passiveMode ? 'active' : ''} role="radio" aria-checked={passiveMode} onClick={() => setPassiveMode(true)}>{t('Passive 模式')}</button>
                    </div>
                  </div>
                  {passiveMode && <div className="bootstrap-passive-guidance"><TriangleAlert size={16} /><p><b>{t('第一次运行需要由你开始')}</b><span>{t('初始化完成后，请在管理员总览点击“继续运行”。第一次唤醒会参与形成她对这个世界最初的记忆。')}</span></p></div>}
                </div>
              </section>
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
            <div className="bootstrap-submit-row">
              <button type="button" className="bootstrap-advanced-trigger" onClick={event => openAdvanced('llm', event.currentTarget)}><SlidersHorizontal size={16} /><span><b>{t('高级初始化')}</b><small>{t('全部参数')}</small></span></button>
              <button className="primary" disabled={submitting || !apiKey.trim() || !normalizedModel || invalidConfigurationPaths.size > 0 || (customModel && !customModelCapabilities.tools)}>{t(submitting ? '正在保存…' : passiveMode ? '保存，等待第一次继续' : '保存并唤醒')} <ChevronRight size={16} /></button>
            </div>
            {advancedOpen && <div className="modal-layer bootstrap-advanced-layer" onMouseDown={event => { if (event.target === event.currentTarget) closeAdvanced(); }}>
              <section className="bootstrap-advanced-dialog" role="dialog" aria-modal="true" aria-labelledby="bootstrap-advanced-title">
                <header><div><span>{t('高级初始化')}</span><h3 id="bootstrap-advanced-title">{t('高级初始化 · 全部参数')}</h3><p>{t('初始化时即可调整运行设置中的完整配置面；未修改的字段继续使用推荐值。')}</p></div><button type="button" className="icon-btn" aria-label={t('关闭高级初始化')} title={t('关闭')} onClick={closeAdvanced} autoFocus><X size={16} /></button></header>
                <div className="bootstrap-advanced-scroll">
                  <div className="bootstrap-config-intro"><Database size={17} /><p><b>{t('完整配置工作台')}</b><span>{t('共覆盖模型、记忆、Agent、运行语言、API、Relay、信道、微信与桌面更新。敏感值单独写入且不会回显。')}</span></p></div>
                  <BootstrapConfigurationEditor initialGroup={advancedInitialGroup} baseline={configurationBaseline} value={configuration} change={changeConfiguration} replaceGroup={replaceConfigurationGroup} secretInputs={configurationSecrets} setSecretInputs={setConfigurationSecrets} secretStatus={data.defaults?.secret_status || {}} invalidPaths={invalidConfigurationPaths} setJsonValidity={setConfigurationJsonValidity} />
                </div>
                <footer><span>{t('这些修改会与基础设置一起保存。')}</span><button type="button" className="primary" onClick={closeAdvanced}>{t('完成')}</button></footer>
              </section>
            </div>}
          </form>
          <p className="bootstrap-footnote"><ShieldCheck size={13} />{t('配置保存在')} <code>data/admin_config.json</code>{t('，API Key 不会回显到页面。')}</p>
        </>}
      </section>
    </section>
  </main>;
}

function ReleaseNotes({ text }: { text: string }) {
  const collapsible = text.length > 900;
  const [expanded, setExpanded] = useState(false);
  return <>
    <div className={'release-notes' + (collapsible && !expanded ? ' preview' : '')} lang={/[\u3400-\u9fff]/.test(text) ? 'zh-CN' : 'en'}><ReactMarkdown
    allowedElements={['p', 'h1', 'h2', 'h3', 'h4', 'strong', 'em', 'del', 'ul', 'ol', 'li', 'a', 'code', 'pre', 'blockquote', 'br', 'table', 'thead', 'tbody', 'tr', 'th', 'td']}
    remarkPlugins={[remarkGfm]}
    components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}
    >{text}</ReactMarkdown></div>
    {collapsible && <button type="button" className="release-notes-toggle" onClick={() => setExpanded(value => !value)}>{t(expanded ? '收起发布说明' : '展开完整发布说明')}</button>}
  </>;
}

function Panel({ title, note, action, children, className = '' }: { title: string; note?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`admin-panel ${className}`}>
    <header><div><h2>{t(title)}</h2>{typeof note === 'string' ? <p>{t(note)}</p> : note}</div>{action}</header>
    {children}
  </section>;
}

function Loading({ error }: { error?: string }) {
  return <div className={error ? 'state-box error' : 'state-box'} role={error ? 'alert' : 'status'}>{!error && <span className="state-pulse" aria-hidden="true"><i /><i /><i /></span>}<span>{t(error || '正在读取生命迹象…')}</span></div>;
}

function runtimePresenceLabel(status: Json) {
  if (!status.is_running) return t('未运行');
  if (status.is_sleeping) return t(status.passive_mode ? '等待事件' : '休息中');
  return t(status.passive_mode ? '处理事件' : '正在运行');
}

function runtimeWakePolicy(status: Json) {
  if (status.passive_mode) return t('仅由外部事件唤醒');
  if (Number(status.idle_sleep_seconds) === 0) return t('无间隔持续运行');
  return t('每 {{seconds}} 秒自唤醒', { seconds: status.idle_sleep_seconds });
}

function Overview({ name, onNavigate }: { name: string; onNavigate: (event: ReactMouseEvent<HTMLAnchorElement>, section: Section) => void }) {
  const { data, error, loading, reload } = useLoad(() => api<Json>('/api/admin/overview'), []);
  const usage = useLoad(() => api<UsageStats>('/api/admin/usage'), []);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState('');
  const reloadAll = async () => { await Promise.all([reload(), usage.reload()]); };
  const resume = async () => {
    setResuming(true);
    setResumeError('');
    try {
      await api('/api/admin/resume', { method: 'POST' });
      await reload();
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : t('继续运行失败'));
    } finally {
      setResuming(false);
    }
  };
  if (loading || !data) return <Loading error={error} />;
  const status = data.status; const counts = data.counts;
  const running = status.is_running;
  const resting = running && Boolean(status.is_sleeping);
  const firstPassiveStart = running && status.startup_reason === 'bootstrap' && Boolean(status.passive_mode) && Number(status.cycle_count || 0) === 0;
  const presenceState = running ? (resting ? 'resting' : 'running') : 'quiet';
  const presenceLabel = runtimePresenceLabel(status);
  const wakePolicy = runtimeWakePolicy(status);
  const sampledAt = formatTime(new Date(), [], { hour: '2-digit', minute: '2-digit' });
  const operations: Array<{ label: string; value: number; note: string; icon: typeof Activity; section: Section }> = [
    { label: t('活跃任务'), value: counts.active_tasks, note: t('{{count}} 项总计', { count: counts.tasks }), icon: ListTodo, section: 'runtime' },
    { label: t('运行 Bubble'), value: counts.active_bubbles, note: t('并行思考分支'), icon: Orbit, section: 'memory' },
    { label: t('短期上下文'), value: counts.short_term_messages, note: t('{{count}} 个树节点', { count: data.memory.tree_nodes }), icon: MessagesSquare, section: 'memory' },
    { label: t('长期记忆'), value: counts.long_term_memories, note: t('可语义检索'), icon: Database, section: 'memory' },
    { label: t('待触发闹钟'), value: counts.alarms, note: t('后台守候中'), icon: AlarmClock, section: 'runtime' },
    { label: t('上下文容量'), value: data.memory.max_tokens, note: 'Token', icon: CircleGauge, section: 'memory' },
  ];
  return <div className="page-stack overview-dashboard">
    <section className={`overview-status-strip ${presenceState}`}>
      <div className="overview-status-lead">
        <div className="overview-mini-signal" aria-hidden="true">{[22, 58, 92, 46, 72].map((height, index) => <i style={{ '--h': `${height}%`, '--d': `${index * .08}s` } as React.CSSProperties} key={height + '-' + index} />)}</div>
        <div><p className="eyebrow">{t('当前状态')}</p><h1>{name || 'Coworker'}<span className={`live-badge ${presenceState}`}>{presenceLabel}</span></h1></div>
      </div>
      <div className="overview-status-facts">
        <div><span>{t('主线模型')}</span><strong title={`${status.provider}/${status.model}`}>{status.provider}/{status.model}</strong></div>
        <div><span>{t('生命循环')}</span><strong>{t('第 {{count}} 次', { count: status.cycle_count || 0 })}</strong></div>
        <div><span>{t('唤醒方式')}</span><strong title={wakePolicy}>{wakePolicy}</strong></div>
        <div><span>{t('本次采样')}</span><strong>{sampledAt}</strong></div>
      </div>
      <div className="overview-status-actions">
        {resting && !firstPassiveStart && <button type="button" className="primary mini" disabled={resuming} onClick={() => void resume()} title={t('不添加消息，直接唤醒主循环')}><Play size={14} />{t(resuming ? '正在继续…' : '继续运行')}</button>}
        <button className="icon-btn" onClick={() => void reloadAll()} title={t('刷新总览')} aria-label={t('刷新总览')}><RefreshCw size={16} /></button>
      </div>
    </section>
    {firstPassiveStart && <section className="overview-first-cycle" role="note"><div className="overview-first-cycle-mark"><Orbit size={22} /><i /></div><div><span>{t('Passive 模式 · 第一次运行')}</span><h2>{t('请由你开启她对世界的第一次观察')}</h2><p>{t('被动模式不会自行开始生命循环。点击“开始第一次运行”会在不添加对话消息的情况下主动继续；这次醒来所感知的环境，将参与形成她最初的世界记忆。')}</p></div>{resting ? <button type="button" className="primary" disabled={resuming} onClick={() => void resume()}><Play size={15} />{t(resuming ? '正在开始…' : '开始第一次运行')}</button> : <small>{t('正在准备第一次运行，请稍候刷新。')}</small>}</section>}
    {resumeError && <div className="notice error"><TriangleAlert size={17} /><span>{resumeError}</span></div>}
    {data.pending_restart && <div className="notice amber"><TriangleAlert size={17} /><span>{t('有配置等待重启后生效。')}</span></div>}
    <div className="overview-main-grid">
      <AdminUsageOverview
        stats={usage.data}
        loading={usage.loading}
        error={usage.error}
        analyticsHref={sectionHref('usage')}
        pricingHref={modelPricingHref()}
        onOpenAnalytics={event => onNavigate(event, 'usage')}
      />
      <Panel title="运行快照" note="当前任务、记忆和后台守候" className="overview-operations-panel">
        <div className="overview-operation-grid">
          {operations.map(item => <a className="overview-operation" href={sectionHref(item.section)} onClick={event => onNavigate(event, item.section)} key={item.label}><item.icon size={16} /><span>{item.label}</span><strong>{Number(item.value).toLocaleString()}</strong><small>{item.note}</small></a>)}
        </div>
        <footer className="overview-runtime-footer">
          <span><b>{t('回溯状态')}</b>{data.memory.backfill?.running ? `${data.memory.backfill.done}/${data.memory.backfill.total}` : t('空闲')}</span>
          <span><b>{t('本轮启动')}</b>{formatDateTime(status.started_at)}</span>
        </footer>
      </Panel>
    </div>
  </div>;
}

function UsageAnalyticsPage({ onOpenLogs, pricingHref }: {
  onOpenLogs: (startTime?: string, endTime?: string, eventType?: string) => void;
  pricingHref: string;
}) {
  const usage = useLoad(() => api<UsageStats>('/api/admin/usage'), []);
  return <AdminUsageAnalytics
    stats={usage.data}
    loading={usage.loading}
    error={usage.error}
    onReload={usage.reload}
    onLoadRange={(startDate, endDate) => {
      const query = new URLSearchParams({ start_date: startDate, end_date: endDate });
      return api<UsageStats>(`/api/admin/usage?${query.toString()}`);
    }}
    onOpenLogs={onOpenLogs}
    pricingHref={pricingHref}
  />;
}

function Models() {
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>('/api/admin/model'), []);
  const catalog = useLoad(() => api<Json>('/api/admin/model/catalog'), []);
  const [switchTo, setSwitchTo] = useState({ provider: '', model_id: '' });
  const [switchError, setSwitchError] = useState('');
  const [switching, setSwitching] = useState(false);
  const [refreshingCatalog, setRefreshingCatalog] = useState(false);
  const [draft, setDraft] = useState<Json | null>(null);
  const [fallbackText, setFallbackText] = useState('');
  useEffect(() => { if (data) { setDraft(JSON.parse(JSON.stringify(data))); setFallbackText((data.fallbacks || []).join('\n')); setSwitchTo({ provider: data.active.provider || '', model_id: data.active.model || '' }); } }, [data]);
  const selectedCatalog = (catalog.data?.providers || []).find((item: Json) => item.name === switchTo.provider);
  const switchModels: string[] = selectedCatalog?.models || [];
  const refreshCatalog = async () => {
    setRefreshingCatalog(true);
    try {
      const next = await api<Json>('/api/admin/model/catalog/refresh', { method: 'POST', body: JSON.stringify({}) });
      catalog.setData(next);
    } catch (catalogError) {
      setSwitchError(catalogError instanceof Error ? catalogError.message : t('模型目录刷新失败'));
    } finally {
      setRefreshingCatalog(false);
    }
  };
  const modelsDirty = Boolean(data && draft && JSON.stringify({ thinking_effort: draft.thinking_effort, summary: draft.summary, vision: draft.vision, fallbacks: draft.fallbacks, mem0: draft.mem0 }) !== JSON.stringify({ thinking_effort: data.thinking_effort, summary: data.summary, vision: data.vision, fallbacks: data.fallbacks, mem0: data.mem0 }));
  useNavigationGuard('models', modelsDirty);
  const save = async () => {
    if (!draft) return;
    const next = await api<Json>('/api/admin/model', { method: 'PATCH', body: JSON.stringify({ thinking_effort: draft.thinking_effort, summary: draft.summary, fallbacks: draft.fallbacks, vision: draft.vision, mem0: draft.mem0 }) });
    setData(next); setDraft(next); setFallbackText((next.fallbacks || []).join('\n'));
  };
  const switchModel = async () => {
    setSwitchError('');
    setSwitching(true);
    try {
      const next = await api<Json>('/api/admin/model/switch', { method: 'POST', body: JSON.stringify(switchTo) });
      setData(next); setDraft(next); setSwitchTo({ provider: '', model_id: '' });
    } catch (error) {
      setSwitchError(error instanceof Error ? error.message : t('切换模型失败'));
    } finally {
      setSwitching(false);
    }
  };
  if (loading || !draft) return <Loading error={error} />;
  const set = (path: string, value: any) => setDraft((old: Json) => { const n = structuredClone(old); const [a, b] = path.split('.'); if (b === undefined) { n[a] = value; } else { n[a][b] = value; } return n; });
  return <div className="page-stack">
    <Panel title="主线模型" note="切换立即生效，正在执行的单次调用不会被中断。">
      <div className="active-model"><Bot size={28} /><div><span>{t('当前接棒者')}</span><strong>{draft.active.provider}/{draft.active.model}</strong></div></div>
      <div className="inline-form"><select value={switchTo.provider} onChange={e => setSwitchTo({ ...switchTo, provider: e.target.value })}><option value="">{t('选择 Provider')}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select><EditableCombobox id="switch-model-input" value={switchTo.model_id} options={switchModels.map((modelId: string) => ({ value: modelId }))} onChange={next => setSwitchTo({ ...switchTo, model_id: next })} placeholder={t('模型 ID（留空使用默认）')} emptyMessage={t('当前 Provider 暂无模型目录，可直接输入模型 ID')} toggleLabel={t('展开模型目录')} /><div className="inline-form-actions"><button className="primary" disabled={!switchTo.provider || switching} onClick={() => void switchModel()}>{switching ? t('正在切换…') : t('切换模型')}</button><button type="button" className="ghost mini" disabled={refreshingCatalog} onClick={() => void refreshCatalog()}>{t(refreshingCatalog ? '正在刷新目录…' : '刷新模型目录')}</button></div></div>
      <Field label="主线思考强度" hint="空值沿用 Provider 默认；none 关闭思考，其余档位按 Provider 原生能力映射" hot><select value={draft.thinking_effort || ''} onChange={e => set('thinking_effort', e.target.value)}>{THINKING_EFFORT_OPTIONS.map(level => <option key={level} value={level}>{level || t('Provider 默认')}</option>)}</select></Field>
      {(switchError || selectedCatalog?.error || catalog.error) && <div className="notice error" role="alert"><TriangleAlert size={16} /><span>{switchError || selectedCatalog?.error || catalog.error}</span></div>}
    </Panel>
    <div className="two-col">
      <Panel title="摘要与压缩" note="控制上下文压缩时使用的模型。">
        <div className="field-grid"><Field label="Provider" hint="留空时跟随主线模型"><select value={draft.summary.provider} onChange={e => set('summary.provider', e.target.value)}><option value="">{t('跟随主线（{{provider}}）', { provider: draft.active.provider })}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select></Field><Field label="模型" hint="留空时跟随主线模型"><input value={draft.summary.model} onChange={e => set('summary.model', e.target.value)} placeholder={draft.active.model} /></Field><label className="switch"><input type="checkbox" checked={draft.summary.thinking} onChange={e => set('summary.thinking', e.target.checked)} /><i /><span>{t('启用 Thinking')}</span></label><Field label="思考强度"><select value={draft.summary.thinking_effort || ''} onChange={e => set('summary.thinking_effort', e.target.value)}>{THINKING_EFFORT_OPTIONS.map(level => <option key={level} value={level}>{level || t('Provider 默认')}</option>)}</select></Field></div>
      </Panel>
      <Panel title="视觉理解" note="为纯文本主模型提供图片分析能力。">
        <div className="field-grid"><Field label="Provider" hint="留空时关闭视觉分析"><select value={draft.vision.provider} onChange={e => set('vision.provider', e.target.value)}><option value="">{t('关闭')}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select></Field><Field label="模型" hint="视觉分析使用该模型"><input value={draft.vision.model} onChange={e => set('vision.model', e.target.value)} /></Field><label className="switch"><input type="checkbox" checked={draft.vision.thinking} onChange={e => set('vision.thinking', e.target.checked)} /><i /><span>{t('启用 Thinking')}</span></label><Field label="思考强度"><select value={draft.vision.thinking_effort || ''} onChange={e => set('vision.thinking_effort', e.target.value)}>{THINKING_EFFORT_OPTIONS.map(level => <option key={level} value={level}>{level || t('Provider 默认')}</option>)}</select></Field></div>
      </Panel>
    </div>
    <Panel title="记忆 LLM（mem0 抽取）" note="控制记忆提取/去重推断使用的模型；留空跟随主线，修改后立即生效。">
      <div className="field-grid"><Field label="Provider" hint="留空时跟随主线模型"><select value={draft.mem0.provider} onChange={e => set('mem0.provider', e.target.value)}><option value="">{t('跟随主线（{{provider}}）', { provider: draft.active.provider })}</option>{draft.providers.map((p: string) => <option key={p}>{p}</option>)}</select></Field><Field label="模型" hint="留空时跟随主线模型"><input value={draft.mem0.model} onChange={e => set('mem0.model', e.target.value)} placeholder={draft.active.model} /></Field><label className="switch"><input type="checkbox" checked={draft.mem0.thinking} onChange={e => set('mem0.thinking', e.target.checked)} /><i /><span>{t('启用 Thinking')}</span></label></div>
    </Panel>
    <Panel title="失败降级链" note="每行填写 provider 或 provider/model，按从上到下的顺序接棒。">
      <LineNumberTextarea className="code-area short" value={fallbackText} onChange={e => { setFallbackText(e.target.value); setDraft({ ...draft, fallbacks: e.target.value.split('\n').map(x => x.trim()).filter(Boolean) }); }} />
      <div className="panel-actions"><button className="primary" onClick={() => void save()}><Save size={15} />{t('保存并热更新')}</button><button className="ghost" onClick={() => { setFallbackText((data?.fallbacks || []).join('\n')); void reload(); }}>{t('放弃修改')}</button></div>
    </Panel>
  </div>;
}

function Field({ label, children, hint, hot = false }: { label: string; children: ReactNode; hint?: string; hot?: boolean }) { return <label className="field"><span>{t(label)}{hot && <em className="effect-badge hot">{t('立即生效')}</em>}</span>{children}{hint && <small>{t(hint)}</small>}</label>; }

function ComboboxField({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return <div className="field"><label className="field-label" htmlFor={id}>{t(label)}</label>{children}{hint && <small>{t(hint)}</small>}</div>;
}

function StringListEditor({ label, hint, value, onChange, placeholder }: {
  label: string;
  hint: string;
  value: string[];
  onChange: (value: string[]) => void;
  placeholder: string;
}) {
  const [candidate, setCandidate] = useState('');
  const addCandidate = () => {
    const next = candidate.trim();
    if (!next || value.includes(next)) return;
    onChange([...value, next]);
    setCandidate('');
  };
  return <div className="field string-list-field">
    <span>{t(label)}</span>
    <div className="string-list-editor">
      {value.length > 0 ? <div className="string-list-items">{value.map((item, index) => <div className="string-list-item" key={`${item}:${index}`}><code>{item}</code><button type="button" onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))} title={t('删除匹配规则')} aria-label={t('删除匹配规则 {{item}}', { item })}><X size={13} /></button></div>)}</div> : <div className="string-list-empty">{t('当前不按 participant 匹配')}</div>}
      <div className="string-list-add">
        <input value={candidate} onChange={event => setCandidate(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addCandidate(); } }} placeholder={placeholder} />
        <button type="button" className="ghost mini" disabled={!candidate.trim() || value.includes(candidate.trim())} onClick={addCandidate}><Plus size={14} />{t('添加规则')}</button>
      </div>
    </div>
    <small>{t(hint)}</small>
  </div>;
}

function ProviderModelCapabilityEditor({ value, onChange }: {
  value: Json[];
  onChange: (value: Json[]) => void;
}) {
  const changeCapability = (index: number, key: string, nextValue: string | boolean) => {
    const next = value.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: nextValue } : item);
    if (key === 'video' && nextValue === true) next[index].vision = true;
    if (key === 'vision' && nextValue === false) next[index].video = false;
    onChange(next);
  };
  return <section className="provider-model-capabilities">
    <header><div><b>{t('自定义模型能力')}</b><small>{t('为当前连接声明目录外模型或覆盖内置判断；能力按模型 ID 精确匹配。')}</small></div><button type="button" className="ghost mini" onClick={() => onChange([...value, { model: '', tools: false, vision: false, video: false }])}><Plus size={13} />{t('添加模型')}</button></header>
    {value.length ? <div className="provider-model-list">{value.map((capability, index) => <article key={index}>
      <label className="provider-model-id"><span>{t('模型 ID')}</span><input value={capability.model || ''} onChange={event => changeCapability(index, 'model', event.target.value)} placeholder="model-id" /></label>
      <div className="model-capability-toggles compact">
        <label><input type="checkbox" checked={Boolean(capability.tools)} onChange={event => changeCapability(index, 'tools', event.target.checked)} /><span>{t('工具调用')}</span></label>
        <label><input type="checkbox" checked={Boolean(capability.vision)} onChange={event => changeCapability(index, 'vision', event.target.checked)} /><span>{t('图片理解')}</span></label>
        <label><input type="checkbox" checked={Boolean(capability.video)} onChange={event => changeCapability(index, 'video', event.target.checked)} /><span>{t('视频理解')}</span></label>
      </div>
      <button type="button" className="danger-icon" title={t('移除模型能力')} aria-label={t('移除模型能力')} onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14} /></button>
    </article>)}</div> : <p>{t('尚未声明自定义模型；未列出的模型继续使用接口协议的内置能力目录。')}</p>}
  </section>;
}

function ProviderModelPriceEditor({ value, onChange, providerNames }: {
  value: Json[];
  onChange: (value: Json[]) => void;
  providerNames: string[];
}) {
  const { language } = useAdminI18n();
  const changePrice = (index: number, key: string, nextValue: unknown) => {
    onChange(value.map((item, itemIndex) => itemIndex === index
      ? { ...item, [key]: nextValue }
      : item));
  };
  const numberValue = (raw: string, optional = false) => raw === ''
    ? (optional ? null : '')
    : Number(raw);
  const validationError = validateModelPrices(value);
  const validationMessage = validationError === 'identity'
    ? t('模型定价必须填写 Provider 和模型 ID。')
    : validationError === 'currency'
      ? t('模型定价币种必须是三个英文字母。')
      : validationError === 'rates'
        ? t('输入价和输出价必须是非负数。')
        : validationError === 'cached_rate'
          ? t('缓存输入价必须留空或填写非负数。')
          : validationError === 'duplicate'
            ? t('同一个 Provider 中不能重复配置模型价格。')
            : '';
  const providerOptions = providerNames.map(name => ({ value: name }));
  const currencyOptions = COMMON_MODEL_PRICE_CURRENCIES.map(currency => {
    const details = modelPriceCurrencyDetails(currency, language);
    return {
      value: currency,
      label: details.displayName,
      detail: details.symbol === currency ? undefined : details.symbol,
    };
  });
  return <section className="provider-model-prices" id="model-pricing">
    <header><div><b>{t('模型定价')}</b><small>{t('按 Provider 与模型 ID 精确匹配；价格单位为每百万 Token，修改后立即重算历史消费估算。')}</small></div><button type="button" className="ghost mini" onClick={() => onChange([...value, {
      provider: providerNames[0] || '',
      model: '',
      currency: 'USD',
      input_per_million: 0,
      output_per_million: 0,
      cached_input_per_million: null,
    }])}><Plus size={13} />{t('添加价格')}</button></header>
    {value.length ? <div className="provider-price-list">{value.map((price, index) => <article key={index}>
      <div className="provider-price-object-grid">
        <ComboboxField id={`model-price-provider-${index}`} label="Provider" hint="可选择有效连接，也可输入历史 Provider">
          <EditableCombobox id={`model-price-provider-${index}`} value={String(price.provider || '')} options={providerOptions} onChange={next => changePrice(index, 'provider', next)} placeholder="openai" emptyMessage={t('没有匹配的 Provider；可继续使用当前输入。')} toggleLabel={t('展开 Provider 选项')} />
        </ComboboxField>
        <Field label="模型 ID" hint="区分大小写并精确匹配"><input value={price.model || ''} onChange={event => changePrice(index, 'model', event.target.value)} placeholder="gpt-5.2" /></Field>
        <ComboboxField id={`model-price-currency-${index}`} label="币种" hint="可选择常用币种，也可输入其他三字母代码">
          <EditableCombobox id={`model-price-currency-${index}`} value={String(price.currency || '')} options={currencyOptions} onChange={next => changePrice(index, 'currency', next)} placeholder="USD" emptyMessage={t('没有匹配的常用币种；可继续使用三字母代码。')} toggleLabel={t('展开币种选项')} maxLength={3} normalize={next => next.toUpperCase()} />
        </ComboboxField>
      </div>
      <button type="button" className="danger-icon provider-price-remove" title={t('移除模型价格')} aria-label={t('移除模型价格')} onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14} /></button>
      <div className="provider-price-rate-block"><div className="provider-price-rate-heading"><span>{t('Token 单价')}</span><small>{t('以下金额均按每百万 Token 计算')}</small></div><div className="provider-price-rate-grid">
        <Field label="输入"><input type="number" min="0" step="any" value={price.input_per_million ?? ''} onChange={event => changePrice(index, 'input_per_million', numberValue(event.target.value))} /></Field>
        <Field label="输出"><input type="number" min="0" step="any" value={price.output_per_million ?? ''} onChange={event => changePrice(index, 'output_per_million', numberValue(event.target.value))} /></Field>
        <Field label="缓存输入" hint="留空时使用普通输入价"><input type="number" min="0" step="any" value={price.cached_input_per_million ?? ''} onChange={event => changePrice(index, 'cached_input_per_million', numberValue(event.target.value, true))} placeholder={t('同输入价')} /></Field>
      </div></div>
    </article>)}</div> : <p>{t('尚未配置模型价格；用量仍会统计 Token，但消费估算会标记为未定价。')}</p>}
    {validationMessage && <p className="field-error" role="alert">{validationMessage}</p>}
    <div className="provider-price-note"><TriangleAlert size={15} /><span>{t('消费为本地估算，不含请求费、图片或视频独立计费、缓存写入、阶梯价、折扣与税费；最终以 Provider 账单为准。')}</span></div>
  </section>;
}

function TransportListEditor({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const options = [
    { value: 'websocket', label: 'WebSocket', hint: '桌面与网页聊天的实时连接' },
    { value: 'sse', label: 'SSE', hint: '基于事件流的实时连接' },
  ];
  const toggle = (transport: string, enabled: boolean) => {
    onChange(enabled ? [...value, transport] : value.filter(item => item !== transport));
  };
  return <div className="field transport-list-field">
    <span>{t('透明接管实时信道')}</span>
    <div className="transport-list-editor">{options.map(option => <label key={option.value}><input type="checkbox" checked={value.includes(option.value)} onChange={event => toggle(option.value, event.target.checked)} /><i><Check size={12} /></i><span><b>{option.label}</b><small>{t(option.hint)}</small></span></label>)}</div>
    <small>{t('勾选后，这些实时信道会显示泡泡接手和归还的结构化状态。修改后需要安全重启。')}</small>
  </div>;
}

function JsonEditor({ value, onChange, onValidityChange }: {
  value: unknown;
  onChange: (value: unknown) => void;
  onValidityChange: (valid: boolean) => void;
}) {
  const serialize = (next: unknown) => JSON.stringify(next, null, 2) ?? '';
  const [text, setText] = useState(() => serialize(value));
  const [valid, setValid] = useState(true);
  const lastSubmitted = useRef(serialize(value));
  const invalidDraft = useRef(false);
  const validityCallback = useRef(onValidityChange);
  validityCallback.current = onValidityChange;
  useEffect(() => {
    const serialized = serialize(value);
    if (invalidDraft.current || serialized !== lastSubmitted.current) {
      setText(serialized);
      setValid(true);
      invalidDraft.current = false;
      validityCallback.current(true);
    }
    lastSubmitted.current = serialized;
  }, [value]);
  const edit = (next: string) => {
    setText(next);
    try {
      const parsed = JSON.parse(next);
      const serialized = serialize(parsed);
      setValid(true);
      invalidDraft.current = false;
      lastSubmitted.current = serialized;
      onValidityChange(true);
      onChange(parsed);
    } catch {
      setValid(false);
      invalidDraft.current = true;
      onValidityChange(false);
    }
  };
  return <>
    <LineNumberTextarea className={`code-area compact${valid ? '' : ' invalid'}`} value={text} onChange={event => edit(event.target.value)} aria-invalid={!valid} />
    {!valid && <small className="field-error" role="alert">{t('JSON 格式无效；修正后才能保存这个字段。')}</small>}
  </>;
}

function ConfigurationField({ path, value, change, secretInputs, setSecretInputs, secretStatus, setJsonValidity, hot = false, passiveMode = false, activeAdminToken }: {
  path: string;
  value: unknown;
  change: (value: unknown) => void;
  secretInputs: Record<string, string>;
  setSecretInputs: (value: Record<string, string>) => void;
  secretStatus: Json;
  setJsonValidity: (path: string, valid: boolean) => void;
  hot?: boolean;
  passiveMode?: boolean;
  activeAdminToken?: Json;
}) {
  const [copyTokenState, setCopyTokenState] = useState<'idle' | 'copying' | 'copied' | 'error'>('idle');
  const segments = path.split('.');
  const key = segments[segments.length - 1] || path;
  const label = CONFIG_LABELS[path] || humanize(key);
  const presentation = configFieldPresentation(path, { passiveMode });
  const copyCommunicationToken = async () => {
    setCopyTokenState('copying');
    try {
      const result = await api<Json>('/api/admin/communication-token');
      const token = String(result.communication_token || '');
      if (!token) throw new Error(t('通信令牌未配置'));
      await navigator.clipboard.writeText(token);
      setCopyTokenState('copied');
    } catch {
      setCopyTokenState('error');
    }
    window.setTimeout(() => setCopyTokenState('idle'), 1600);
  };
  if (presentation.editor === 'locale') return <Field label={label} hint={presentation.hint} hot={hot}><select value={String(value)} onChange={event => change(event.target.value)}><option value="zh-CN">简体中文 (zh-CN)</option><option value="en">English (en)</option></select></Field>;
  if (presentation.editor === 'fallback-list' || presentation.editor === 'cors-list' || presentation.editor === 'participant-list') return <Fragment>
    {presentation.editor === 'participant-list' && <div className="config-section-heading"><div><b>{t('泡泡接管提示')}</b><small>{t('控制哪些对话能看到泡泡接手、代答和归还；修改后需要安全重启。')}</small></div></div>}
    <StringListEditor label={label} hint={presentation.hint || ''} value={Array.isArray(value) ? value.map(String) : []} onChange={change} placeholder={presentation.placeholder || ''} />
  </Fragment>;
  if (presentation.editor === 'transport-list') return <TransportListEditor value={Array.isArray(value) ? value.map(String) : []} onChange={change} />;
  if (secretStatus[path] !== undefined) {
    const status = secretStatus[path] || {};
    const usesAdminToken = path === 'api.communication_token' && !status.configured && activeAdminToken?.configured;
    const hint = status.configured
      ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' })
      : usesAdminToken
        ? t('当前使用管理员令牌；可设置独立令牌以隔离权限')
        : t('当前未配置；请设置以保护 REST 消息、状态与 Desktop 通信');
    const placeholder = status.configured
      ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' })
      : usesAdminToken ? t('留空继续使用管理员令牌') : t('输入新值（建议设置）');
    return <Field label={label} hot={hot} hint={hint}>
      <div className="secret-field-row">
        <input type="password" value={secretInputs[path] || ''} onChange={event => setSecretInputs({ ...secretInputs, [path]: event.target.value })} placeholder={placeholder} />
        {path === 'api.communication_token' && (
          <>
            <button
              type="button"
              className="ghost mini"
              title={t('生成符合 Relay 要求的通信令牌')}
              onClick={() => setSecretInputs({ ...secretInputs, [path]: generateCommunicationToken() })}
            >
              {t('生成')}
            </button>
            <button
              type="button"
              className="ghost mini"
              disabled={copyTokenState === 'copying'}
              title={t('复制通信令牌')}
              onClick={() => void copyCommunicationToken()}
            >
              {t(copyTokenState === 'copied'
                ? '通信令牌已复制'
                : copyTokenState === 'copying'
                  ? '正在复制…'
                  : copyTokenState === 'error'
                    ? '通信令牌复制失败'
                    : '复制令牌')}
            </button>
          </>
        )}
      </div>
    </Field>;
  }
  if (typeof value === 'boolean') return <label className="switch config-switch"><input type="checkbox" checked={value} onChange={event => change(event.target.checked)} /><i /><span>{t(label)}{hot && <em className="effect-badge hot">{t('立即生效')}</em>}</span></label>;
  if (typeof value === 'number') return <Field label={label} hint={presentation.hint} hot={hot}><input type="number" value={value} min={presentation.minimum} max={presentation.maximum} step={presentation.step ?? 'any'} onChange={event => change(Number(event.target.value))} /></Field>;
  if (typeof value === 'string') return <Field label={label} hint={presentation.hint} hot={hot}><input type={presentation.inputType} value={value} onChange={event => change(event.target.value)} placeholder={presentation.placeholder} /></Field>;
  return <Field label={label} hint="JSON 结构" hot={hot}><JsonEditor value={value} onChange={change} onValidityChange={valid => setJsonValidity(path, valid)} /></Field>;
}

const GROUP_LABELS: Record<string, string> = { llm: '模型与 Provider', i18n: '运行语言', memory: '记忆系统', agent: 'Agent 循环', api: 'API 服务', wecom: '企业微信', desktop_updates: '桌面更新', admin: '管理端', ...settingsPanelLabels() };
const HIDDEN_CONFIG = new Set(['admin.token', 'desktop_updates.admin_token', 'agent.system_prompt_template']);
const SYSTEM_PROMPT_VARIABLE_DESCRIPTIONS: Record<string, string> = {
  IDENTITY: '姓名、位置与人格身份',
  ENVIRONMENT: '操作系统、Python、目录与时区',
  INSTINCTS: '内置本能与新生指引',
  GUIDELINES: '通用行为与记忆指引',
  LANGUAGE_POLICY: '参与者回复语言策略',
  THINKING: 'thinking.md 中的可选思维内容',
  CHANNELS: '当前信道的可选操作指引',
  SKILLS: '已加载 Skill 的可选注册表',
  PALACES: '已加载 Palace 的可选注册表',
};
const LLM_MODEL_ORCHESTRATION_FIELDS = new Set(['summary_provider', 'summary_model', 'summary_thinking', 'fallbacks', 'vision_provider', 'vision_model', 'vision_thinking']);

const LLM_CONFIG_FIELD_ORDER = [
  'default_provider',
  'default_model',
  'max_tokens',
  'thinking_effort',
  'summary_provider',
  'summary_model',
  'summary_thinking',
  'summary_thinking_effort',
  'fallbacks',
  'vision_provider',
  'vision_model',
  'vision_thinking',
  'vision_thinking_effort',
  'managed_providers',
  'model_prices',
];

function orderedConfigEntries(group: string, value: Json): [string, unknown][] {
  if (group !== 'llm' || !value || Array.isArray(value) || typeof value !== 'object') {
    return Object.entries(value || {});
  }
  const rank = new Map(LLM_CONFIG_FIELD_ORDER.map((key, index) => [key, index]));
  return Object.entries(value).sort(([left], [right]) => {
    const leftRank = rank.get(left) ?? Number.MAX_SAFE_INTEGER;
    const rightRank = rank.get(right) ?? Number.MAX_SAFE_INTEGER;
    return leftRank - rightRank || left.localeCompare(right);
  });
}
type DesktopUpdateSourceConfig = {
  id: string;
  name: string;
  type: 'github' | 'coworker';
  token?: string;
  include_prereleases?: boolean;
  api_base_url?: string;
  repository?: string;
  include_drafts?: boolean;
  base_url?: string;
};

type DesktopUnit = 'minutes' | 'hours' | 'days';
type ByteUnit = 'MiB' | 'GiB';

const INTERVAL_UNITS: Array<{ value: DesktopUnit; label: string; seconds: number }> = [
  { value: 'minutes', label: '分钟', seconds: 60 },
  { value: 'hours', label: '小时', seconds: 3600 },
  { value: 'days', label: '天', seconds: 86400 },
];
const BYTE_UNITS: Array<{ value: ByteUnit; bytes: number }> = [
  { value: 'MiB', bytes: 1024 * 1024 },
  { value: 'GiB', bytes: 1024 * 1024 * 1024 },
];

function createUuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();

  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function desktopSource(type: 'github' | 'coworker' = 'github'): DesktopUpdateSourceConfig {
  const id = createUuid();
  return type === 'github'
    ? { id, name: t('GitHub 上游'), type, api_base_url: 'https://api.github.com', repository: '', token: '', include_drafts: false, include_prereleases: false }
    : { id, name: t('Coworker 上游'), type, base_url: '', token: '', include_prereleases: false };
}

function sourceSecretPath(id: string) { return `desktop_updates.sync_sources.${id}.token`; }
function sourceProviderLabel(source?: DesktopUpdateSourceConfig) { return source?.type === 'coworker' ? 'Coworker Feed' : 'GitHub Releases'; }
function sourceTarget(source?: DesktopUpdateSourceConfig) { return source?.type === 'coworker' ? source.base_url || '' : source?.repository || ''; }
function sourceEndpoint(source?: DesktopUpdateSourceConfig) { return source?.type === 'coworker' ? source.base_url || '' : source?.api_base_url || ''; }
function isSourceConfigured(source?: DesktopUpdateSourceConfig) { return !!(source && source.name?.trim() && sourceEndpoint(source).trim() && (source.type === 'coworker' || source.repository?.trim())); }

function preferredInterval(seconds: number): { value: number; unit: DesktopUnit } {
  if (seconds >= 86400 && seconds % 86400 === 0) return { value: seconds / 86400, unit: 'days' };
  if (seconds >= 3600 && seconds % 3600 === 0) return { value: seconds / 3600, unit: 'hours' };
  return { value: Math.max(1, Math.round(seconds / 60)), unit: 'minutes' };
}

function preferredBytes(bytes: number): { value: number; unit: ByteUnit } {
  const gib = 1024 * 1024 * 1024;
  if (bytes >= gib && bytes % gib === 0) return { value: bytes / gib, unit: 'GiB' };
  return { value: Math.max(1, Math.round(bytes / (1024 * 1024))), unit: 'MiB' };
}

function intervalToSeconds(value: number, unit: DesktopUnit) {
  return Math.max(0, Math.round(value * (INTERVAL_UNITS.find(item => item.value === unit)?.seconds || 60)));
}

function bytesFromUnit(value: number, unit: ByteUnit) {
  return Math.max(0, Math.round(value * (BYTE_UNITS.find(item => item.value === unit)?.bytes || 1024 * 1024)));
}

function describeDesktopUpdateSave(before: Json = {}, after: Json = {}, fallback: string) {
  const beforeActive = before.sync_active_source || '';
  const afterActive = after.sync_active_source || '';
  const beforeSources = (Array.isArray(before.sync_sources) ? before.sync_sources : []) as DesktopUpdateSourceConfig[];
  const afterSources = (Array.isArray(after.sync_sources) ? after.sync_sources : []) as DesktopUpdateSourceConfig[];
  const beforeActiveSource = beforeSources.find(source => source.id === beforeActive);
  const afterActiveSource = afterSources.find(source => source.id === afterActive);
  if (!afterActive && beforeActive) return t('同步已关闭；已保存的上游来源会继续保留。');
  if (afterActive && beforeActive !== afterActive) return t('当前上游已切换为 {{name}}，保存后立即生效。', { name: afterActiveSource?.name || sourceProviderLabel(afterActiveSource) });
  if (afterActive && JSON.stringify(beforeActiveSource || {}) !== JSON.stringify(afterActiveSource || {})) return t('当前上游配置已更新，保存后立即生效。');
  if (JSON.stringify(beforeSources) !== JSON.stringify(afterSources)) return t('备用上游来源已更新，不会影响当前同步任务。');
  if (before.sync_interval_seconds !== after.sync_interval_seconds || before.sync_max_asset_bytes !== after.sync_max_asset_bytes || before.sync_max_run_bytes !== after.sync_max_run_bytes) return t('同步周期和下载限制已更新，保存后立即生效。');
  if (before.sync_on_start !== after.sync_on_start || before.feed_token !== after.feed_token) return t('桌面更新全局设置已保存。');
  return fallback;
}

function DesktopUpdateSettings({ value, change, secretInputs, setSecretInputs, secretStatus, onValidationChange, updateUrl = true }: { value: Json; change: (key: string, value: any) => void; secretInputs: Record<string, string>; setSecretInputs: (value: Record<string, string>) => void; secretStatus: Record<string, { configured?: boolean; last4?: string }>; onValidationChange: (message: string) => void; updateUrl?: boolean }) {
  const sources = (Array.isArray(value.sync_sources) ? value.sync_sources : []) as DesktopUpdateSourceConfig[];
  const active = value.sync_active_source || '';
  const sourceFromUrl = updateUrl ? new URLSearchParams(window.location.search).get('source') || '' : '';
  const [selectedSourceId, setSelectedSourceId] = useState(() => sourceFromUrl || active || sources[0]?.id || '');
  const selectedSource = sources.find(source => source.id === selectedSourceId) || null;
  const activeSource = sources.find(source => source.id === active) || null;
  const detailRef = useRef<HTMLElement | null>(null);
  const initialInterval = preferredInterval(Number(value.sync_interval_seconds || 21600));
  const initialAsset = preferredBytes(Number(value.sync_max_asset_bytes || 2 * 1024 * 1024 * 1024));
  const initialRun = preferredBytes(Number(value.sync_max_run_bytes || 4 * 1024 * 1024 * 1024));
  const [intervalUnit, setIntervalUnit] = useState<DesktopUnit>(initialInterval.unit);
  const [assetUnit, setAssetUnit] = useState<ByteUnit>(initialAsset.unit);
  const [runUnit, setRunUnit] = useState<ByteUnit>(initialRun.unit);
  const updateSources = (next: DesktopUpdateSourceConfig[], nextActive = active) => { change('sync_sources', next); change('sync_active_source', nextActive || null); };
  const patchSource = (id: string, patch: Partial<DesktopUpdateSourceConfig>) => updateSources(sources.map(source => source.id === id ? { ...source, ...patch } : source));
  const setUrlSource = (id: string, replace = false) => {
    if (!updateUrl) return;
    const params = new URLSearchParams(window.location.search);
    params.set('section', 'settings'); params.set('group', 'desktop_updates');
    if (id) params.set('source', id); else params.delete('source');
    window.history[replace ? 'replaceState' : 'pushState'](null, '', `?${params.toString()}`);
  };
  const selectSource = (id: string, replace = false) => { setSelectedSourceId(id); setUrlSource(id, replace); };
  const addSource = (type: 'github' | 'coworker') => { const next = desktopSource(type); updateSources([...sources, next]); selectSource(next.id); };
  const duplicate = (source: DesktopUpdateSourceConfig) => {
    const next = { ...source, id: createUuid(), name: t('{{name}} 副本', { name: source.name || sourceProviderLabel(source) }), token: '' };
    updateSources([...sources, next]); setSecretInputs(Object.fromEntries(Object.entries(secretInputs).filter(([key]) => key !== sourceSecretPath(next.id)))); selectSource(next.id);
  };
  const remove = (id: string) => {
    const source = sources.find(item => item.id === id);
    if (!source) return;
    const isActive = active === id;
    if (!confirm(isActive ? t('删除当前上游“{{name}}”？同步会切换为关闭，但不会影响已导入的草稿。', { name: source.name || sourceProviderLabel(source) }) : t('删除上游来源“{{name}}”？', { name: source.name || sourceProviderLabel(source) }))) return;
    const index = sources.findIndex(item => item.id === id);
    const next = sources.filter(item => item.id !== id);
    updateSources(next, isActive ? '' : active);
    setSecretInputs(Object.fromEntries(Object.entries(secretInputs).filter(([key]) => key !== sourceSecretPath(id))));
    if (selectedSourceId === id) selectSource(next[Math.min(index, next.length - 1)]?.id || '', true);
  };
  const move = (index: number, delta: number) => { const next = [...sources]; const target = index + delta; if (target < 0 || target >= next.length) return; [next[index], next[target]] = [next[target], next[index]]; updateSources(next); };
  const setActive = (id: string) => change('sync_active_source', id || null);
  const setInterval = (amount: number, unit = intervalUnit) => change('sync_interval_seconds', intervalToSeconds(amount, unit));
  const setLimit = (key: 'sync_max_asset_bytes' | 'sync_max_run_bytes', amount: number, unit: ByteUnit) => change(key, bytesFromUnit(amount, unit));
  const intervalAmount = Number(((value.sync_interval_seconds || 21600) / (INTERVAL_UNITS.find(item => item.value === intervalUnit)?.seconds || 60)).toFixed(2));
  const assetAmount = Number(((value.sync_max_asset_bytes || 0) / (BYTE_UNITS.find(item => item.value === assetUnit)?.bytes || 1)).toFixed(2));
  const runAmount = Number(((value.sync_max_run_bytes || 0) / (BYTE_UNITS.find(item => item.value === runUnit)?.bytes || 1)).toFixed(2));
  const validationError = useMemo(() => {
    const names = sources.map(source => (source.name || '').trim().toLowerCase()).filter(Boolean);
    if (names.length !== new Set(names).size) return t('上游来源名称不能重复。');
    if (Number(value.sync_interval_seconds || 0) < 300) return t('检测间隔至少为 5 分钟。');
    if (Number(value.sync_max_asset_bytes || 0) < 1024 || Number(value.sync_max_run_bytes || 0) < 1024) return t('下载大小上限必须大于 1 KiB。');
    if (Number(value.sync_max_run_bytes || 0) < Number(value.sync_max_asset_bytes || 0)) return t('单次同步总量上限不能小于单个制品上限。');
    return '';
  }, [sources, value.sync_interval_seconds, value.sync_max_asset_bytes, value.sync_max_run_bytes]);
  useEffect(() => { onValidationChange(validationError); }, [onValidationChange, validationError]);
  useEffect(() => {
    const current = updateUrl ? new URLSearchParams(window.location.search).get('source') || '' : '';
    if (current && sources.some(source => source.id === current)) { setSelectedSourceId(current); return; }
    if (selectedSourceId && !sources.some(source => source.id === selectedSourceId)) setSelectedSourceId(active || sources[0]?.id || '');
  }, [active, selectedSourceId, sources, updateUrl]);
  useEffect(() => {
    if (!updateUrl) return;
    const syncSourceFromLocation = () => {
      const current = new URLSearchParams(window.location.search).get('source') || '';
      setSelectedSourceId(current && sources.some(source => source.id === current) ? current : active || sources[0]?.id || '');
    };
    window.addEventListener('popstate', syncSourceFromLocation);
    return () => window.removeEventListener('popstate', syncSourceFromLocation);
  }, [active, sources, updateUrl]);
  const feedStatus = secretStatus['desktop_updates.feed_token'];
  const activeConfigured = isSourceConfigured(activeSource || undefined);
  return <div className="desktop-update-settings">
    <section className={'desktop-sync-overview ' + (active ? activeConfigured ? 'ready' : 'warning' : 'disabled')}>
      <div className="desktop-sync-overview-copy"><RefreshCw size={22} /><div><span>{t('上游同步')}</span><h3>{active ? activeConfigured ? t('当前上游已就绪') : t('当前上游未配置完整') : t('上游同步已关闭')}</h3><p>{activeSource ? <><b>{activeSource.name || sourceProviderLabel(activeSource)}</b>{' · '}{sourceProviderLabel(activeSource)}{sourceTarget(activeSource) ? ` · ${sourceTarget(activeSource)}` : ''}</> : t('选择一个来源作为当前上游后，定时同步和立即同步才会运行。')}</p></div></div>
      <Field label="当前上游" hint="切换后保存即可立即应用，不需要重启"><select value={active || ''} onChange={event => setActive(event.target.value)}><option value="">{t('关闭上游同步')}</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name || sourceProviderLabel(source)}</option>)}</select></Field>
    </section>
    {validationError && <div className="notice error"><TriangleAlert size={15} /><span>{validationError}</span></div>}
    <div className="desktop-source-toolbar"><div><b>{t('上游来源')}</b><small>{t('可以保存多个同类型来源，但同一时间只有当前上游会运行。')}</small></div><div><button type="button" className="ghost mini" onClick={() => addSource('github')}><Plus size={14} />{t('添加 GitHub 来源')}</button><button type="button" className="ghost mini" onClick={() => addSource('coworker')}><Plus size={14} />{t('添加 Coworker 来源')}</button></div></div>
    {sources.length ? <div className="desktop-source-workbench">
      <div className="desktop-source-list" role="list">{sources.map((source, index) => {
        const configured = isSourceConfigured(source); const isActive = active === source.id; const isSelected = selectedSourceId === source.id;
        return <article role="listitem" className={'desktop-source-item ' + (isActive ? 'active ' : '') + (isSelected ? 'selected ' : '') + (!configured ? 'warning' : '')} key={source.id}>
          <button type="button" onClick={() => selectSource(source.id)}><span><b>{source.name || t('未命名来源')}</b><small>{sourceProviderLabel(source)}{sourceTarget(source) ? ` · ${sourceTarget(source)}` : ''}</small></span><em className={'desktop-source-badge ' + (isActive ? 'active' : !configured ? 'warning' : '')}>{isActive ? t('当前') : configured ? t('备用') : t('待补全')}</em></button>
          <div className="desktop-source-row-actions"><button type="button" className="ghost mini" disabled={isActive} onClick={() => setActive(source.id)}>{t('设为当前')}</button><button type="button" className="ghost mini" disabled={index === 0} onClick={() => move(index, -1)}>{t('上移')}</button><button type="button" className="ghost mini" disabled={index === sources.length - 1} onClick={() => move(index, 1)}>{t('下移')}</button><button type="button" className="ghost mini" onClick={() => duplicate(source)}>{t('复制')}</button><button type="button" className="danger-outline mini" onClick={() => remove(source.id)}>{t('删除')}</button></div>
        </article>;
      })}</div>
      <section className="desktop-source-detail" ref={detailRef}>{selectedSource ? <>
        <header><div><span>{t('来源详情')}</span><h3>{selectedSource.name || t('未命名来源')}</h3><p>{selectedSource.id}</p></div><em className={'desktop-source-badge ' + (active === selectedSource.id ? 'active' : '')}>{active === selectedSource.id ? t('当前上游') : t('备用上游')}</em></header>
        <div className="desktop-source-form">
          <div className="desktop-form-row two">
            <Field label="来源名称"><input value={selectedSource.name || ''} onChange={event => patchSource(selectedSource.id, { name: event.target.value })} /></Field>
            <Field label="Provider 类型" hint="切换类型会清空此来源的地址字段，并需要重新确认访问 Token"><select value={selectedSource.type} onChange={event => { const type = event.target.value as 'github' | 'coworker'; if (type !== selectedSource.type && !confirm(t('切换来源类型会清空当前地址字段和未保存 Token，继续吗？'))) return; patchSource(selectedSource.id, type === 'github' ? { type, api_base_url: 'https://api.github.com', repository: '', base_url: undefined, token: '' } : { type, base_url: '', api_base_url: undefined, repository: undefined, include_drafts: undefined, token: '' }); setSecretInputs({ ...secretInputs, [sourceSecretPath(selectedSource.id)]: '' }); }}><option value="github">GitHub Releases</option><option value="coworker">Coworker Feed</option></select></Field>
          </div>
          <div className="desktop-form-row two">
            {selectedSource.type === 'github' ? <><Field label="GitHub API Base URL"><input value={selectedSource.api_base_url || ''} onChange={event => patchSource(selectedSource.id, { api_base_url: event.target.value })} placeholder="https://api.github.com" /></Field><Field label="上游仓库（owner/repo）"><input value={selectedSource.repository || ''} onChange={event => patchSource(selectedSource.id, { repository: event.target.value })} placeholder="owner/repo" /></Field></> : <Field label="Coworker Base URL" hint="会读取该实例的 published release feed"><input value={selectedSource.base_url || ''} onChange={event => patchSource(selectedSource.id, { base_url: event.target.value })} placeholder="https://coworker.example.com" /></Field>}
            <Field label="访问 Token" hint={secretStatus[sourceSecretPath(selectedSource.id)]?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: secretStatus[sourceSecretPath(selectedSource.id)]?.last4 || '' }) : t('当前未配置')}><input type="password" value={secretInputs[sourceSecretPath(selectedSource.id)] || ''} onChange={event => setSecretInputs({ ...secretInputs, [sourceSecretPath(selectedSource.id)]: event.target.value })} placeholder={secretStatus[sourceSecretPath(selectedSource.id)]?.configured ? t('留空保留，输入新值则替换') : t('可选')} /></Field>
          </div>
          <div className="desktop-option-grid">
            {selectedSource.type === 'github' && <label className="desktop-option-card"><input type="checkbox" checked={!!selectedSource.include_drafts} onChange={event => patchSource(selectedSource.id, { include_drafts: event.target.checked })} /><span>{t('同步 GitHub 草稿')}</span><small>{t('仅在需要接收上游 draft 时开启。')}</small></label>}
            <label className="desktop-option-card"><input type="checkbox" checked={!!selectedSource.include_prereleases} onChange={event => patchSource(selectedSource.id, { include_prereleases: event.target.checked })} /><span>{t('同步预发布版本')}</span><small>{t('允许 SemVer 预发布版本进入本地草稿。')}</small></label>
          </div>
        </div>
      </> : <div className="provider-empty">{t('选择一个上游来源查看详情。')}</div>}</section>
    </div> : <div className="provider-empty desktop-source-empty">{t('还没有上游来源。添加 GitHub 或 Coworker 来源后，在上方选择一个作为当前上游。')}</div>}
    <section className="desktop-global-settings">
      <div className="config-section-heading"><div><b>{t('全局同步设置')}</b><small>{t('这些限制和周期作用于当前活跃来源。')}</small></div></div>
      <div className="config-fields"><Field label="检测间隔"><div className="unit-field"><input type="number" min="1" value={intervalAmount} onChange={event => setInterval(Number(event.target.value))} /><select value={intervalUnit} onChange={event => { const unit = event.target.value as DesktopUnit; setIntervalUnit(unit); setInterval(intervalAmount, unit); }}>{INTERVAL_UNITS.map(unit => <option value={unit.value} key={unit.value}>{t(unit.label)}</option>)}</select></div></Field><label className="switch config-switch"><input type="checkbox" checked={value.sync_on_start !== false} onChange={event => change('sync_on_start', event.target.checked)} /><i /><span>{t('服务启动时立即检测')}</span></label><Field label="单个制品大小上限"><div className="unit-field"><input type="number" min="1" value={assetAmount} onChange={event => setLimit('sync_max_asset_bytes', Number(event.target.value), assetUnit)} /><select value={assetUnit} onChange={event => { const unit = event.target.value as ByteUnit; setAssetUnit(unit); setLimit('sync_max_asset_bytes', assetAmount, unit); }}>{BYTE_UNITS.map(unit => <option value={unit.value} key={unit.value}>{unit.value}</option>)}</select></div><small>{formatBytes(value.sync_max_asset_bytes || 0)}</small></Field><Field label="单次同步总量上限"><div className="unit-field"><input type="number" min="1" value={runAmount} onChange={event => setLimit('sync_max_run_bytes', Number(event.target.value), runUnit)} /><select value={runUnit} onChange={event => { const unit = event.target.value as ByteUnit; setRunUnit(unit); setLimit('sync_max_run_bytes', runAmount, unit); }}>{BYTE_UNITS.map(unit => <option value={unit.value} key={unit.value}>{unit.value}</option>)}</select></div><small>{formatBytes(value.sync_max_run_bytes || 0)}</small></Field><Field label="下游同步 Feed Token" hint={feedStatus?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: feedStatus.last4 || '' }) : t('当前 feed 已关闭')}><input type="password" value={secretInputs['desktop_updates.feed_token'] || ''} onChange={event => setSecretInputs({ ...secretInputs, 'desktop_updates.feed_token': event.target.value })} placeholder={feedStatus?.configured ? t('留空保留，输入新值则替换') : t('设置后允许其他 Coworker 实例同步本实例已发布版本')} /></Field></div>
    </section>
  </div>;
}

const CONFIG_LABELS: Record<string, string> = {
  'llm.default_provider': '启动时使用的 Provider',
  'llm.default_model': '启动时使用的模型',
  'llm.max_tokens': '单次输出上限',
  'llm.thinking_effort': '主线思考强度',
  'llm.summary_provider': '摘要 Provider',
  'llm.summary_model': '摘要模型',
  'llm.summary_thinking': '摘要 Thinking',
  'llm.summary_thinking_effort': '摘要思考强度',
  'llm.fallbacks': '主模型降级链',
  'llm.vision_provider': '视觉 Provider',
  'llm.vision_model': '视觉模型',
  'llm.vision_thinking': '视觉 Thinking',
  'llm.vision_thinking_effort': '视觉思考强度',
  'i18n.locale': '模型与运行时语言',
  'memory.db_path': '记忆数据目录',
  'memory.short_term_max_tokens': '短期上下文容量',
  'memory.compress_ratio': '每次自动压缩比例',
  'memory.tree_enabled': '启用记忆块树',
  'memory.tree_spine_cap_fraction': '记忆脊柱预算比例',
  'memory.tree_backfill_max_leaves': '回溯叶子数量上限',
  'memory.tree_backfill_concurrency': '回溯并发数',
  'memory.tree_merge_reach_depth': '高层合并下探深度',
  'memory.auto_recall_enabled': '自动召回长期记忆',
  'memory.auto_recall_relevance_threshold': '自动召回相关性阈值',
  'memory.auto_recall_limit': '单次自动召回数量',
  'agent.passive_mode': 'Passive 模式（开发者控制）',
  'agent.idle_sleep_seconds': '主动模式自唤醒间隔（秒）',
  'agent.inbox_dir': '收件箱目录',
  'agent.outbox_dir': '发件箱目录',
  'agent.desktop_registry_dir': '桌面连接注册目录',
  'agent.identity_dir': '身份数据目录',
  'agent.logs_dir': '运行日志目录',
  'agent.interaction_log_rotation_bytes': '交互日志轮换大小',
  'agent.skills_dir': 'Skill 目录',
  'agent.palaces_dir': 'Palace 目录',
  'agent.subconscious_dir': '潜意识模式目录',
  'agent.inbox_poll_interval': '收件箱轮询间隔（秒）',
  'agent.inbox_batch_max': '单批收件数量上限',
  'agent.tick': '启用生命循环 Tick',
  'agent.code_hard_timeout': '代码执行硬超时（秒）',
  'agent.image_max_dimension': '图片最大边长',
  'agent.message_time_prefix': '消息附加时间前缀',
  'agent.bubble_thinking': 'Bubble 启用 Thinking',
  'agent.bubble_max_concurrent': 'Bubble 最大并发数',
  'agent.bubble_handoff_transparency_participant_matches': '透明接管对象',
  'agent.bubble_handoff_transparency_stream_transports': '透明接管实时信道',
  'agent.bubble_timeout_resume_seconds': 'Bubble 超时续跑窗口（秒）',
  'agent.subconscious_thinking': '潜意识启用 Thinking',
  'agent.subconscious_summarize_before_compress': '压缩前生成潜意识摘要',
  'agent.subconscious_max_cycles': '潜意识最大循环次数',
  'api.host': 'API 监听地址',
  'api.port': 'API 监听端口',
  'api.public_url': 'API 公开访问地址',
  'api.communication_token': '通信令牌',
  'api.cors_origins': '允许的跨域来源',
  'relay.enabled': '启用 Relay',
  'relay.url': 'Relay 地址',
  'relay.instance_id': 'Relay 实例 ID',
  'relay.instance_private_key': 'Relay 实例私钥',
  'relay.relay_public_key': 'Relay 公钥',
  'relay.auth_epoch': 'Relay 认证 Epoch',
  'desktop_updates.dir': '本地发布目录',
  'desktop_updates.sync_sources': '上游来源',
  'desktop_updates.sync_active_source': '当前上游',
  'desktop_updates.sync_interval_seconds': '检测间隔（秒）',
  'desktop_updates.sync_on_start': '服务启动时立即检测',
  'desktop_updates.sync_max_asset_bytes': '单个制品大小上限（字节）',
  'desktop_updates.sync_max_run_bytes': '单次同步总量上限（字节）',
  'desktop_updates.feed_token': '下游同步 Feed Token',
  'memory.mem0_llm_provider': '记忆抽取 Provider（mem0）',
  'memory.mem0_llm_model': '记忆抽取模型（mem0）',
  'memory.mem0_llm_thinking': '记忆抽取 Thinking（mem0）',
  'memory.mem0_embedder_model': '记忆向量模型（mem0）',
  'memory.persona_enabled': '启用人物记忆',
  'memory.persona_store_path': '人物记忆文件',
  'wecom.enabled': '启用企业微信',
  'wecom.bot_id': '企业微信 Bot ID',
  'wecom.secret': '企业微信 Secret',
  'wecom.ws_url': '企业微信 WebSocket 地址',
  'weixin.enabled': '启用微信 Claw',
};

function Settings() {
  const { data, error, setData } = useLoad(() => api<Json>('/api/admin/config'), []);
  const settings = useSettingsDraft({
    serverData: data,
    updateServerData: setData,
    request: api,
    describeDesktopSave: describeDesktopUpdateSave,
  });
  const {
    change,
    changeProvider,
    clearOverridePaths,
    desktopValidationError,
    dirtyGroups,
    draft,
    group,
    invalidJsonPaths,
    isHot,
    message,
    resetGroup,
    save,
    saving,
    secretInputs,
    selectGroup,
    setDesktopValidationError,
    setJsonValidity,
    setSecretInputs,
    toggleClearOverride,
  } = settings;
  useNavigationGuard('settings', dirtyGroups.size > 0);
  useEffect(() => {
    if (!data || !draft || group !== 'llm' || window.location.hash !== '#model-pricing') return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById('model-pricing')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [data, draft, group]);
  if (!data || !draft) return <Loading error={error} />;
  const effectiveProviders = data.effective_providers || [];
  const externalProviders = effectiveProviders.filter((provider: Json) => !provider.managed);
  const providerNames = Array.from(new Set([
    ...effectiveProviders,
    ...(draft.llm?.managed_providers || []),
  ].map((provider: Json) => String(provider.name || '')).filter(Boolean)));
  const groups = Object.keys(draft).filter(k => GROUP_LABELS[k]);
  const adminToken = data.secret_status['admin.token'];
  const fallbackToken = data.secret_status['desktop_updates.admin_token'];
  const activeAdminToken = adminToken?.configured ? adminToken : fallbackToken;
  const providerSource = data.sources?.providers ? t('、{{source}}', { source: data.sources.providers }) : '';
  const configNote = t('有效配置来自 {{env}}{{providers}}，并由 {{override}} 覆盖。', { env: '.env', providers: providerSource, override: data.override_path });
  const groupOverrides = (data.overridden_fields || []).filter((path: string) => {
    const field = path.split('.')[1] || '';
    return path.startsWith(`${group}.`)
      && !HIDDEN_CONFIG.has(path)
      && field !== 'config_file'
      && !field.endsWith('runtime_config_file')
      && !(group === 'llm' && /_(api_key|base_url)$/.test(field));
  });
  const CustomSettingsPanel = settingsPanelRegistration(group)?.component;
  return <div className="settings-layout">
    <nav className="subnav">{groups.map(k => <button className={group === k ? 'active' : ''} disabled={saving} onClick={() => selectGroup(k)} key={k}><span>{t(GROUP_LABELS[k])}{dirtyGroups.has(k) && <i className="settings-dirty-dot" title={t('有未保存修改')} />}</span><ChevronRight size={14} /></button>)}</nav>
    <Panel title={GROUP_LABELS[group]} note={configNote} className="config-panel">
      {data.pending_restart && <div className="notice amber"><TriangleAlert size={16} />{t('存在等待重启的修改')}</div>}
      {group !== 'admin' && groupOverrides.length > 0 && <div className="config-override-strip"><div><Database size={16} /><span><b>{t('管理端覆盖')}</b><small>{t('这些字段不会跟随启动环境变化；可以恢复为继承配置。')}</small></span></div><div>{groupOverrides.map((path: string) => {
        const pending = clearOverridePaths.has(path);
        return <button className={pending ? 'pending' : ''} key={path} onClick={() => toggleClearOverride(path)}><span>{t(CONFIG_LABELS[path] || humanize(path.split('.')[1] || path))}</span><RotateCcw size={12} />{t(pending ? '保存后恢复继承' : '恢复继承')}</button>;
      })}</div></div>}
      {group === 'admin' ? <div className="admin-settings-status">
        <section className={`admin-security-hero ${activeAdminToken?.configured ? 'ready' : 'missing'}`}><div className="security-seal"><ShieldCheck size={27} /><i /></div><div><span>{t('保护状态')}</span><h3>{t(activeAdminToken?.configured ? '管理端访问已受保护' : '管理端令牌尚未配置')}</h3><p>{activeAdminToken?.configured ? t('当前令牌已加载，仅显示尾号 {{last4}}。完整值不会发送到浏览器。', { last4: activeAdminToken.last4 }) : t('请在启动环境中设置 ADMIN__TOKEN，然后重启 Coworker。')}</p></div><b>{t(activeAdminToken?.configured ? '已启用' : '未启用')}</b></section>
        <div className="admin-setting-cards"><article><KeyRound size={18} /><div><span>{t('令牌来源')}</span><b>{adminToken?.configured ? 'ADMIN__TOKEN' : fallbackToken?.configured ? 'DESKTOP_UPDATES__ADMIN_TOKEN' : t('未配置')}</b><small>{t('令牌只能通过启动配置轮换，管理页不会回显或覆盖。')}</small></div></article><article><FileCog size={18} /><div><span>{t('配置覆盖文件')}</span><code>{data.override_path}</code><small>{t('其他设置在这里持久化；管理员令牌不写入普通表单。')}</small></div></article><article><RefreshCw size={18} /><div><span>{t('配置生效状态')}</span><b>{t(data.pending_restart ? '等待安全重启' : '当前配置已加载')}</b><small>{t(data.pending_restart ? '保存的修改会在下一次安全重启后生效。' : '当前没有等待重启的管理端修改。')}</small></div></article><article><Fingerprint size={18} /><div><span>{t('浏览器会话')}</span><b>{t('仅当前标签会话')}</b><small>{t('令牌保存在 sessionStorage，关闭标签页后不会长期留存。')}</small></div></article></div>
        <div className="admin-security-note"><TriangleAlert size={16} /><p><b>{t('如何轮换管理员令牌')}</b><span>{t('修改部署环境中的')} <code>ADMIN__TOKEN</code>{t('，再执行安全重启。旧会话会在重启后失效。')}</span></p></div>
      </div> : <>{group === 'desktop_updates' ? <DesktopUpdateSettings value={draft.desktop_updates || {}} change={change} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={data.secret_status || {}} onValidationChange={setDesktopValidationError} /> : CustomSettingsPanel ? <CustomSettingsPanel value={draft[group] || {}} change={change} apply={save} dirty={dirtyGroups.has(group)} saving={saving} request={api} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={data.secret_status || {}} /> : <>{group === 'llm' && <div className="llm-config-overview"><div className="llm-config-copy"><Brain size={22} /><div><span>{t('启动配置')}</span><h3>{t('启动默认值与服务连接')}</h3><p>{t('这里决定 Coworker 重启时先连接哪个模型服务。运行中的模型切换、摘要模型和降级链请在“模型编排”页面调整。')}</p></div></div><div className="llm-config-facts"><span><b>{t(draft.llm.default_provider || '未设置')}</b>{t('启动 Provider')}</span><span><b>{t(draft.llm.default_model || '使用 Provider 默认值')}</b>{t('启动模型')}</span><span><b>{effectiveProviders.length}</b>{t('个可用连接')}</span><span><b>{draft.llm.model_prices?.length || 0}</b>{t('个定价模型')}</span></div></div>}<div className="config-fields">{group === 'llm' && <div className="config-section-heading"><div><b>{t('启动默认值')}</b><small>{t('只在进程启动时读取；修改后需要安全重启。')}</small></div></div>}{group === 'i18n' && <div className="config-section-heading"><div><b>{t('实例级运行语言')}</b><small>{t('语言控制系统 Prompt、工具说明和系统通知；修改后需要安全重启。')}</small></div></div>}{group === 'agent' && <div className="config-section-heading"><div><b>{t('空闲唤醒策略')}</b><small>{t('主动模式适合大多数用户，会按间隔继续运行；Passive 模式主要用于开发者控制，只等待外部事件，也可在总览中手动“继续运行”。')}</small></div></div>}{group === 'wecom' && <div className="config-section-heading"><div><b>{t('长连接热配置')}</b><small>{t('保存后立即启用、停用或重连企业微信；切换期间可能短暂不可用，无需重启 Coworker。')}</small></div></div>}{orderedConfigEntries(group, draft[group]).map(([key, value]) => {
        const path = `${group}.${key}`;
        if (HIDDEN_CONFIG.has(path) || key === 'config_file' || path.endsWith('runtime_config_file')) return null;
        if (group === 'llm' && (key === 'providers_file' || LLM_MODEL_ORCHESTRATION_FIELDS.has(key) || /_(api_key|base_url)$/.test(key))) return null;
        if (key === 'model_prices' && Array.isArray(value)) return <ProviderModelPriceEditor key={key} value={value} providerNames={providerNames} onChange={next => change('model_prices', next)} />;
        if (key === 'managed_providers' && Array.isArray(value)) return <div className="provider-editor" key={key}>
          <div className="provider-editor-head"><div><b>{t('Provider 连接')} <em className="effect-badge hot">{t('修改后立即生效')}</em></b><small>{t('一个连接代表一套模型服务地址、接口协议、访问密钥和模型能力。正在执行的单次调用不受影响，下一次调用使用新连接。')}</small></div><button className="ghost mini" onClick={() => change('managed_providers', [...value, { name: '', type: 'openai', api_key: '', base_url: '', default_model: '', model_capabilities: [] }])}><Plus size={14} />{t('添加连接')}</button></div>
          <div className="provider-source-note"><Database size={16} /><p><b>{t('配置来源彼此独立')}</b><span><code>.env</code> {t('和')} <code>providers.json</code>{t('中的连接只读展示；下方只编辑管理端覆盖，不会复制或接管外部密钥。')}</span></p></div>
          {externalProviders.length > 0 && <div className="provider-effective"><b>{t('外部有效连接（只读）')}</b>{externalProviders.map((provider: Json) => <span key={provider.name}><strong>{provider.name}</strong><code>{provider.type}</code><small>{provider.base_url || t('协议默认地址')}</small></span>)}</div>}
          {value.length ? value.map((provider: Json, index: number) => {
            const secretPath = `llm.managed_providers.${index}.api_key`;
            const status = data.secret_status[secretPath];
            return <article className="provider-row" key={index}>
              <Field label="连接名称" hint="在模型编排中引用的名称"><input value={provider.name || ''} onChange={e => changeProvider(index, 'name', e.target.value)} placeholder={t('例如 openai-work')} /></Field>
              <Field label="接口协议"><select value={provider.type || 'openai'} onChange={e => changeProvider(index, 'type', e.target.value)}>{['openai', 'anthropic', 'deepseek', 'qwen', 'zhipu', 'minimax', 'opencode-go', 'openai_compatible'].map(type => <option key={type} value={type}>{t(PROVIDER_LABELS[type] || type)}</option>)}</select></Field>
              <Field label="服务地址（Base URL）"><input value={provider.base_url || ''} onChange={e => changeProvider(index, 'base_url', e.target.value)} placeholder={t('留空使用协议默认地址')} /></Field>
              <Field label="默认模型" hint="调用未指定模型时使用"><input value={provider.default_model || ''} onChange={e => changeProvider(index, 'default_model', e.target.value)} placeholder={t('可留空')} /></Field>
              <Field label="API Key" hint={status?.configured ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' }) : t('当前未配置')}><input type="password" value={secretInputs[secretPath] || ''} onChange={e => setSecretInputs({ ...secretInputs, [secretPath]: e.target.value })} placeholder={status?.configured ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' }) : t('输入 API Key')} /></Field>
              <ProviderModelCapabilityEditor value={Array.isArray(provider.model_capabilities) ? provider.model_capabilities : []} onChange={next => changeProvider(index, 'model_capabilities', next)} />
              <button className="danger-icon provider-remove" title={t('移除 Provider')} onClick={() => { change('managed_providers', value.filter((_: unknown, i: number) => i !== index)); setSecretInputs(current => Object.fromEntries(Object.entries(current).filter(([path]) => !path.startsWith('llm.managed_providers.')))); }}><Trash2 size={15} /></button>
            </article>;
          }) : <div className="provider-empty">{t('还没有可用的 Provider 连接。点击“添加连接”配置模型服务。')}</div>}
        </div>;
        if (group === 'llm' && key.endsWith('thinking_effort')) return <Field key={key} label={CONFIG_LABELS[path] || humanize(key)} hint="空值沿用 Provider 默认；none 关闭思考，其余档位按 Provider 原生能力映射"><select value={String(value || '')} onChange={e => change(key, e.target.value)}>{THINKING_EFFORT_OPTIONS.map(level => <option key={level} value={level}>{level || t('Provider 默认')}</option>)}</select></Field>;
        if (path === 'llm.default_provider') return <Field key={key} label={CONFIG_LABELS[path]} hint="Coworker 启动后首先使用的连接"><select value={String(value)} onChange={e => change(key, e.target.value)}>{!providerNames.includes(String(value)) && <option value={String(value)}>{String(value)}</option>}{providerNames.map((name: string) => <option key={name}>{name}</option>)}</select></Field>;
        return <ConfigurationField key={key} path={path} value={value} change={next => change(key, next)} secretInputs={secretInputs} setSecretInputs={setSecretInputs} secretStatus={data.secret_status || {}} setJsonValidity={setJsonValidity} hot={isHot(path)} passiveMode={Boolean(draft.agent?.passive_mode)} activeAdminToken={activeAdminToken} />;
      })}</div></>}
      {message && <div className={`notice ${message.kind}`} role={message.kind === 'error' ? 'alert' : 'status'}>{message.text}</div>}
      <div className="panel-actions"><span className={'save-state ' + (dirtyGroups.has(group) ? 'dirty' : '')}>{t(dirtyGroups.has(group) ? '有未保存修改' : '当前分组已同步')}</span><button className="primary" disabled={saving || !dirtyGroups.has(group) || (group === 'desktop_updates' && !!desktopValidationError) || invalidJsonPaths.size > 0} onClick={() => void save()}><Save size={15} />{t(saving ? '正在保存…' : group === 'desktop_updates' || group === 'wecom' || group === 'weixin' || group === 'telegram' || group === 'channel_access' ? '保存并立即应用' : '保存覆盖')}</button><button className="ghost" disabled={saving || !dirtyGroups.has(group)} onClick={resetGroup}>{t('放弃本组修改')}</button></div></>}
    </Panel>
  </div>;
}

function humanize(text: string) { return text.replace(/_/g, ' ').replace(/\b\w/g, (m: string) => m.toUpperCase()); }

const TASK_STATUS: Record<string, string> = { pending: '待处理', in_progress: '进行中', completed: '已完成' };

function timeFromNow(value: string) {
  const parsed = timestampMillis(value);
  if (parsed == null) return t('时间未知');
  const delta = parsed - Date.now();
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

function Runtime({ confirmationName }: { confirmationName: string }) {
  const [tab, setTab] = useState<RuntimeTab>(runtimeTabFromLocation);
  const selectTab = (next: RuntimeTab) => {
    const url = new URL(window.location.href);
    if (next === 'tasks') url.searchParams.delete('runtime_tab');
    else url.searchParams.set('runtime_tab', next);
    if (next !== 'logs') {
      ['log_start', 'log_end', 'log_type', 'log_seq', 'log_q', 'log_seq_start', 'log_seq_end', 'log_cursor'].forEach(key => url.searchParams.delete(key));
    }
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    setTab(next);
  };
  return <div className="page-stack"><div className="tabbar">{[
    ['tasks', '任务'], ['alarms', '闹钟'], ['logs', '运行日志'], ['maintenance', '维护'],
  ].map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => selectTab(id as RuntimeTab)}>{t(label)}</button>)}</div>
    {tab === 'tasks' && <Tasks />}{tab === 'alarms' && <Alarms />}{tab === 'logs' && <Logs />}{tab === 'maintenance' && <Maintenance confirmationName={confirmationName} />}
  </div>;
}

function MemoryCenter({ coworkerName, confirmationName }: { coworkerName: string; confirmationName: string }) {
  const [tab, setTab] = useState<'short' | 'long' | 'thoughts'>(memoryTabFromLocation);
  const selectTab = (next: 'short' | 'long' | 'thoughts') => {
    const url = new URL(window.location.href);
    if (next === 'short') url.searchParams.delete('memory_tab');
    else url.searchParams.set('memory_tab', next);
    if (next !== 'thoughts') {
      url.searchParams.delete('thought_scope');
      url.searchParams.delete('bubble_id');
    }
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    setTab(next);
  };
  useEffect(() => {
    const sync = () => setTab(memoryTabFromLocation());
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);
  return <div className="page-stack memory-center">
    <div className="tabbar memory-tabs">
      <button className={tab === 'short' ? 'active' : ''} onClick={() => selectTab('short')}><MessagesSquare size={14} />{t('短期记忆')}</button>
      <button className={tab === 'long' ? 'active' : ''} onClick={() => selectTab('long')}><Database size={14} />{t('长期记忆')}</button>
      <button className={tab === 'thoughts' ? 'active' : ''} onClick={() => selectTab('thoughts')}><Orbit size={14} />{t('并行思考记录')}</button>
    </div>
    {tab === 'short' ? <ShortTermMemoryView coworkerName={coworkerName} confirmationName={confirmationName} /> : tab === 'long' ? <Memories /> : <Bubbles coworkerName={coworkerName} />}
  </div>;
}

const MEMORY_ROLE: Record<string, string> = { user: '消息', assistant: '搭档', system: '系统', tool: '工具结果' };
const MEMORY_SOURCE: Record<string, string> = {
  file: '文件投递', rest: 'REST API', websocket: 'WebSocket', wecom: '企业微信', weixin: '微信 Claw', telegram: 'Telegram',
  coworker_desktop: '桌面端', codex: 'Codex', bubble: '气泡', alarm: '闹钟提醒',
  code_job: '代码任务', task_reminder: '任务提醒', system: '系统', '并行思考': '并行思考',
  system_recovery: '系统恢复', system_error: '系统错误', skill_warning: '技能提醒',
  tick: '自主循环', model_switch: '模型切换', auto_recall: '自动回忆',
  compress_memory: '记忆压缩', sleep_interrupt: '唤醒消息',
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
    <summary><span className="message-index">{String(index + 1).padStart(2, '0')}</span><span className="message-summary-copy"><b>{role}</b><small>{formatDateTime(message.timestamp)}{' · '}{sourceName}{usage}</small><em className="message-preview">{memoryPreview(message)}</em></span><i>{summaryState}</i></summary>
    <div className="short-message-body"><pre>{memoryContentText(message.content)}</pre>{message.reasoning_content && <section className="message-reasoning"><b><Brain size={12} />{t('思考')}</b><pre>{message.reasoning_content}</pre></section>}{message.tool_calls?.length > 0 && <section className="message-tool-section"><b><Wrench size={12} />{t('工具调用')}</b><div className="message-tools">{message.tool_calls.map((call: Json) => <details className="tool-exchange" key={call.id || call.name} open><summary><span><Wrench size={11} />{call.name}</span><small>{'result' in call ? t('已返回') : t('等待结果')}</small></summary><div><label>{t('参数')}</label><pre>{memoryDetailText(call.arguments)}</pre><label>{t('结果')}</label><pre>{'result' in call ? memoryDetailText(call.result) : t('尚未返回结果')}</pre></div></details>)}</div></section>}{message.recalled_memory_ids?.length > 0 && <p>{t('召回长期记忆：')} {message.recalled_memory_ids.join(' · ')}</p>}{message.tool_call_id && <p>{t('工具调用 ID：')} {message.tool_call_id}</p>}</div>
  </details>;
}

function MemoryTreeNode({ node, depth = 0 }: { node: Json; depth?: number }) {
  const children = node.children || [];
  return <details className="short-tree-node" open={depth === 0} style={{ '--indent': Math.min(depth, 3) * 7 + 'px' } as React.CSSProperties}>
    <summary>
      <span className="tree-level">L{node.level}</span>
      <span className="tree-node-copy"><b>{formatDateTime(node.t_start)} → {formatDateTime(node.t_end)}</b><small>{t('{{count}} 条消息', { count: node.msg_count })}{' · '}{Number(node.token_estimate).toLocaleString()} token{' · '}{node.token_count_source === 'exact' ? t('精确摘要计数') : t('估算摘要计数')}</small></span>
      <span className={node.raw_available ? 'raw-state' : 'raw-state summary-only'}>{node.raw_available ? t('原文可达') : t('仅摘要')}</span>
    </summary>
    <div className="tree-node-detail"><p>{node.summary}</p>{children.length > 0 && <div className="tree-children">{children.map((child: Json, childIndex: number) => <MemoryTreeNode node={child} depth={depth + 1} key={child.t_start + '-' + child.level + '-' + childIndex} />)}</div>}</div>
  </details>;
}

function ShortTermMemoryView({ coworkerName, confirmationName }: { coworkerName: string; confirmationName: string }) {
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>('/api/admin/memory/short-term'), []);
  const [maxLeaves, setMaxLeaves] = useState(64);
  const [pinDraft, setPinDraft] = useState({ label: '', content: '' });
  const [pinSaving, setPinSaving] = useState(false);
  const [pinError, setPinError] = useState('');
  const [pinMessage, setPinMessage] = useState('');
  const [actionError, setActionError] = useState('');
  const [actionMessage, setActionMessage] = useState('');
  const tailRef = useRef<HTMLDivElement | null>(null);
  const tailStick = useRef(true);
  useEffect(() => {
    const node = tailRef.current;
    if (node && tailStick.current) node.scrollTop = node.scrollHeight;
  }, [data?.messages]);
  const onTailScroll = () => {
    const node = tailRef.current;
    if (node) tailStick.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
  };
  useEffect(() => {
    // 实时观察：挂载期间用轻量端点高频刷新当前消息尾部；完整快照在进入页面/手动刷新时拉取。
    const timer = window.setInterval(() => {
      void api<Json>('/api/admin/memory/short-term/messages')
        .then(tail => setData(current => current ? { ...current, messages: tail.messages } : current))
        .catch(() => undefined);
    }, data?.backfill?.running ? 1500 : 2000);
    return () => window.clearInterval(timer);
  }, [data?.backfill?.running, setData]);
  if (loading || !data) return <Loading error={error} />;
  const water = data.token_watermark;
  const ratio = Math.max(0, Number(water.ratio || 0));
  const percent = Math.round(ratio * 100);
  const measured = water.measured_at ? formatDateTime(water.measured_at) : t('当前读取');
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
        <p><ShieldCheck size={13} />{t('优先按最近一次精确输入触发压缩；无精确值时回退当前短期估算。')}</p>
      </div>
      <button className="icon-btn watermark-refresh" onClick={() => void reload()} title={t('刷新短期记忆')} aria-label={t('刷新短期记忆')}><RefreshCw size={16} /></button>
    </section>

    <div className="short-memory-grid">
      <Panel title="记忆脊柱" note="越老的记忆层级越高；展开节点可向下查看保留的细节。" className="short-tree-panel">
        <div className="short-tree">{data.tree.nodes.length ? data.tree.nodes.map((node: Json, treeIndex: number) => <MemoryTreeNode node={node} key={node.t_start + '-' + node.level + '-' + treeIndex} />) : <Empty text="记忆树还是空的；上下文压缩后会在这里形成时间脊柱。" />}</div>
      </Panel>
      <Panel title="当前消息尾部" note="这些消息会按顺序直接进入下一次主线思考。" className="short-tail-panel">
        <div className="short-message-list" ref={tailRef} onScroll={onTailScroll}>{data.messages.length ? data.messages.map((message: Json, messageIndex: number) => <MemoryMessage message={message} index={messageIndex} defaultOpen={messageIndex >= data.messages.length - 3} coworkerName={coworkerName} key={message.timestamp + '-' + message.index} />) : <Empty text="当前没有短期消息；新的输入会从这里开始累积。" />}</div>
      </Panel>
    </div>

    <Panel title="固定上下文" note="固定项会在缺失时重新注入主线，避免关键资料被压缩带走。">
      {(pinError || pinMessage) && <div className={'notice ' + (pinError ? 'error' : 'success')}>{pinError || pinMessage}</div>}
      <form className="pin-compose" onSubmit={addPin}><input required maxLength={80} value={pinDraft.label} onChange={event => setPinDraft({ ...pinDraft, label: event.target.value })} placeholder={t('标题，例如：项目约定')} /><textarea required value={pinDraft.content} onChange={event => setPinDraft({ ...pinDraft, content: event.target.value })} placeholder={t('需要始终保留在上下文里的内容')} /><button className="primary" disabled={pinSaving}><Plus size={14} />{pinSaving ? t('添加中…') : t('添加固定项')}</button></form>
      <div className="pinned-context-list">{data.pinned_items.length ? data.pinned_items.map((item: Json) => <details key={item.pin_id}><summary><Fingerprint size={15} /><span><b>{item.label}</b><small>{item.pin_id}{' · '}{formatDateTime(item.created_at)}</small></span><button type="button" className="icon-btn pin-delete" title={t('删除固定上下文')} aria-label={t('删除固定上下文 {{label}}', { label: item.label })} onClick={event => { event.preventDefault(); void removePin(item); }}><Trash2 size={14} /></button></summary><pre>{item.content}</pre>{item.file_path && <p><FileText size={12} />{t('跟随文件：')} {item.file_path}</p>}</details>) : <Empty text="当前没有固定上下文；可以从上方添加。" />}</div>
    </Panel>

    <Panel title="记忆维护" note="压缩会调用模型；回溯在后台从持久日志重建时间脊柱。">
      {(actionError || actionMessage) && <div className={'notice ' + (actionError ? 'error' : 'success')}>{actionError || actionMessage}</div>}
      <div className="danger-list memory-maintenance">
        <DangerAction title="全量压缩短期记忆" description="把当前主线消息压缩进记忆树，释放上下文空间。执行期间会产生模型调用。" button="开始压缩" confirmationName={confirmationName} onConfirm={async () => { await api('/api/admin/memory/compress', { method: 'POST', body: JSON.stringify({ confirm_name: confirmationName }) }); await reload(); }} />
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
    <div className="record-list">{visible.length ? visible.map((task: Json) => <article className={'record task-record ' + task.status} key={task.id}><div className="record-main"><span className={'status-pill ' + task.status}>{t(TASK_STATUS[task.status] || task.status)}</span><b>{task.description}</b>{task.details && <p className="record-details">{task.details}</p>}<small>{t('更新于 {{time}}', { time: formatDateTime(task.updated_at) })}{' · '}{task.id}</small></div><div className="row-actions"><select aria-label={t('更新任务“{{description}}”的状态', { description: task.description })} value={task.status} onChange={async event => { await api('/api/admin/tasks/' + task.id, { method: 'PATCH', body: JSON.stringify({ description: task.description, details: task.details || '', status: event.target.value }) }); await reload(); }}>{Object.entries(TASK_STATUS).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select><button className="icon-btn" title={t('编辑任务')} aria-label={t('编辑任务')} onClick={() => setEditing({ ...task })}><Pencil size={15} /></button><button className="danger-icon" title={t('删除任务')} aria-label={t('删除任务“{{description}}”', { description: task.description })} onClick={async () => { if (confirm(t('删除任务“{{description}}”？', { description: task.description }))) { await api('/api/admin/tasks/' + task.id, { method: 'DELETE' }); await reload(); } }}><Trash2 size={15} /></button></div></article>) : <Empty text={data.tasks.length ? '这个分类里没有任务。' : '还没有任务，先写下第一件要推进的事。'} />}</div>
    {editing && <div className="modal-layer"><div className="confirm-modal task-modal"><ListTodo size={24} /><h3>{t('编辑任务')}</h3><Field label="任务描述"><input autoFocus value={editing.description} onChange={event => setEditing({ ...editing, description: event.target.value })} /></Field><Field label="执行细节"><textarea value={editing.details || ''} onChange={event => setEditing({ ...editing, details: event.target.value })} placeholder={t('记录计划、进度或下一步')} /></Field><Field label="状态"><select value={editing.status} onChange={event => setEditing({ ...editing, status: event.target.value })}>{Object.entries(TASK_STATUS).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select></Field><div className="panel-actions"><button className="ghost" onClick={() => setEditing(null)}>{t('取消')}</button><button className="primary" disabled={!editing.description.trim()} onClick={() => void saveEdit()}><Check size={15} />{t('保存任务')}</button></div></div></div>}
  </Panel>;
}

function Bubbles({ coworkerName }: { coworkerName: string }) {
  const [scope, setScope] = useState<'bubbles' | 'subconscious'>(thoughtScopeFromLocation);
  const [targetBubbleId, setTargetBubbleId] = useState(bubbleIdFromLocation);
  const basePath = scope === 'bubbles' ? '/api/admin/bubbles' : '/api/admin/subconscious';
  const targetQuery = targetBubbleId ? '&bubble_id=' + encodeURIComponent(targetBubbleId) : '';
  const { data, error, loading, reload, setData } = useLoad(() => api<Json>(basePath + '?limit=50' + targetQuery), [scope, targetBubbleId]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState('');
  useEffect(() => setMoreError(''), [scope]);
  const selectScope = (next: 'bubbles' | 'subconscious') => {
    const url = new URL(window.location.href);
    if (next === 'bubbles') url.searchParams.delete('thought_scope');
    else url.searchParams.set('thought_scope', next);
    url.searchParams.delete('bubble_id');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    setTargetBubbleId('');
    setScope(next);
  };
  useEffect(() => {
    const sync = () => {
      setScope(thoughtScopeFromLocation());
      setTargetBubbleId(bubbleIdFromLocation());
    };
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);
  if (loading || !data) return <Loading error={error} />;
  const loadMore = async () => {
    setLoadingMore(true); setMoreError('');
    try {
      const next = await api<Json>(basePath + '?limit=50&offset=' + data.bubbles.length);
      setData({ ...next, bubbles: [...data.bubbles, ...(next.bubbles || [])] });
    } catch (error) { setMoreError(error instanceof Error ? error.message : t('更多历史记录加载失败')); }
    finally { setLoadingMore(false); }
  };
  return <Panel title="并行思考记录" note="查看主动 Bubble 和潜意识已落盘的完整思考轨迹。"><div className="list-toolbar"><div className="task-filters"><button className={scope === 'bubbles' ? 'active' : ''} onClick={() => selectScope('bubbles')}>{t('主动 Bubble')}</button><button className={scope === 'subconscious' ? 'active' : ''} onClick={() => selectScope('subconscious')}>{t('潜意识')}</button></div><button className="icon-btn" onClick={() => void reload()} title={t('刷新思考记录')} aria-label={t('刷新思考记录')}><RefreshCw size={15} /></button></div>{targetBubbleId && <div className="notice bubble-target-notice"><Orbit size={15} />{t('已定位思考记录 {{id}}', { id: targetBubbleId })}</div>}<div className="bubble-list">{data.bubbles.length ? data.bubbles.map((bubble: Json) => {
    const targeted = isTargetBubbleRecord(bubble, targetBubbleId);
    return <BubbleRecord bubble={bubble} reload={reload} scope={scope} coworkerName={coworkerName} defaultOpen={targeted} targeted={targeted} key={bubble.log_id || bubble.id} />;
  }) : <Empty text={targetBubbleId ? '没有找到对应的思考记录。' : scope === 'bubbles' ? '当前没有 Bubble 记录。' : '当前没有潜意识记录。'} />}</div>{moreError && <div className="notice error">{moreError}</div>}{data.has_more && <button className="bubble-load-more ghost" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? t('加载中…') : t('加载更多（已显示 {{shown}}/{{total}}）', { shown: data.bubbles.length, total: data.total })}</button>}</Panel>;
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
    if (event.type === 'thinking_start') return [{ ...common, role: 'system', content: t('第 {{count}} 轮开始{{mode}}', { count: Number(event.cycle || 0) + 1, mode: event.thinking === false ? t('（快速模式）') : event.thinking_effort ? `（${event.thinking_effort}）` : '' }) }];
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

function BubbleRecord({ bubble, reload, scope, coworkerName, defaultOpen = false, targeted = false }: { bubble: Json; reload: () => Promise<void>; scope: 'bubbles' | 'subconscious'; coworkerName: string; defaultOpen?: boolean; targeted?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [events, setEvents] = useState<Json[] | null>(null);
  const [historyError, setHistoryError] = useState('');
  const recordRef = useRef<HTMLElement>(null);
  const messages = useMemo(() => events ? bubbleHistoryMessages(events) : null, [events]);
  const fetchHistory = useCallback(async () => {
    if (events) return;
    setHistoryError('');
    try { const result = await api<Json>('/api/admin/' + scope + '/' + encodeURIComponent(bubble.log_id || bubble.id) + '/history'); setEvents(result.events || []); }
    catch (error) { setHistoryError(error instanceof Error ? error.message : t('历史记录加载失败')); }
  }, [bubble.id, bubble.log_id, events, scope]);
  const loadHistory = async () => {
    const next = !open; setOpen(next);
    if (next) await fetchHistory();
  };
  useEffect(() => { if (defaultOpen) void fetchHistory(); }, [defaultOpen, fetchHistory]);
  useEffect(() => {
    if (!targeted) return;
    recordRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [targeted]);
  const model = [bubble.provider, bubble.model].filter(Boolean).join('/') || t('模型未记录');
  const createdAt = bubble.created_at ? t(' · {{time}}', { time: formatDateTime(bubble.created_at) }) : '';
  return <article ref={recordRef} className={'bubble-record ' + (open ? 'open ' : '') + (targeted ? 'targeted' : '')}>
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
    await api('/api/admin/alarms', { method: 'POST', body: JSON.stringify({ message: draft.message, trigger_at: localDateTimeInputToIso(draft.trigger_at), repeat_seconds: draft.repeat_seconds ? Number(draft.repeat_seconds) : null }) });
    setDraft({ trigger_at: '', message: '', repeat_seconds: '' });
    await reload();
  };
  const alarms = [...data.alarms].sort((a: Json, b: Json) => (timestampMillis(a.trigger_at) ?? 0) - (timestampMillis(b.trigger_at) ?? 0));
  const summary = alarms.length
    ? t('正在守候 {{count}} 个提醒，最近一个 {{time}}', { count: alarms.length, time: timeFromNow(alarms[0].trigger_at) })
    : t('当前没有待触发提醒');
  return <Panel title="闹钟与守候" note="时间按本地时区输入；到点后提醒会进入 Coworker 的 inbox。">
    <div className="alarm-compose"><Field label="提醒时间"><input type="datetime-local" value={draft.trigger_at} min={toLocalDateTimeInput(new Date(), 'minute')} onChange={event => setDraft({ ...draft, trigger_at: event.target.value })} /></Field><Field label="重复"><select value={draft.repeat_seconds} onChange={event => setDraft({ ...draft, repeat_seconds: event.target.value })}><option value="">{t('仅一次')}</option><option value="3600">{t('每小时')}</option><option value="86400">{t('每天')}</option><option value="604800">{t('每周')}</option></select></Field><Field label="提醒内容"><input value={draft.message} onChange={event => setDraft({ ...draft, message: event.target.value })} onKeyDown={event => { if (event.key === 'Enter' && draft.trigger_at && draft.message.trim()) void create(); }} placeholder={t('到点要提醒什么？')} /></Field><button className="primary" disabled={!draft.trigger_at || !draft.message.trim()} onClick={() => void create()}><AlarmClock size={15} />{t('设定闹钟')}</button></div>
    <div className="alarm-summary"><Clock3 size={16} /><span>{summary}</span><button className="icon-btn" onClick={() => void reload()} title={t('刷新闹钟')} aria-label={t('刷新闹钟')}><RefreshCw size={14} /></button></div>
    <div className="record-list alarm-list">{alarms.length ? alarms.map((alarm: Json) => <article className="record alarm-record" key={alarm.id}><div className="alarm-time"><strong>{formatDate(alarm.trigger_at, [], { month: 'short', day: 'numeric' })}</strong><b>{formatTime(alarm.trigger_at, [], { hour: '2-digit', minute: '2-digit' })}</b></div><div className="record-main"><span className="alarm-due">{timeFromNow(alarm.trigger_at)}</span><b>{alarm.message}</b><small>{repeatLabel(alarm.repeat_seconds)}{' · '}{alarm.id}</small></div><button className="danger-icon" title={t('取消闹钟')} aria-label={t('取消闹钟“{{message}}”', { message: alarm.message })} onClick={async () => { if (confirm(t('取消闹钟“{{message}}”？', { message: alarm.message }))) { await api('/api/admin/alarms/' + alarm.id, { method: 'DELETE' }); await reload(); } }}><X size={15} /></button></article>) : <Empty text="还没有闹钟，设定一个需要按时记起的提醒。" />}</div>
  </Panel>;
}

function Logs() {
  const { language } = useAdminI18n();
  const dateLocale = language === 'zh' ? 'zh-CN' : 'en-US';
  const initialQuery = boundedLocationParam('log_q', 500);
  const initialSeqStart = safeLocationParam('log_seq_start', /^\d+$/);
  const initialSeqEnd = safeLocationParam('log_seq_end', /^\d+$/);
  const initialCursor = boundedLocationParam('log_cursor', 512) || null;
  const [query, setQuery] = useState(initialQuery);
  const [debouncedQuery, setDebouncedQuery] = useState(initialQuery);
  const [type, setType] = useState(logTypeFromLocation);
  const [seqStartDraft, setSeqStartDraft] = useState(initialSeqStart);
  const [seqEndDraft, setSeqEndDraft] = useState(initialSeqEnd);
  const [seqStart, setSeqStart] = useState(initialSeqStart);
  const [seqEnd, setSeqEnd] = useState(initialSeqEnd);
  const [sequenceError, setSequenceError] = useState('');
  const [timeStartDraft, setTimeStartDraft] = useState(() => logTimeInputValue(logTimeFromLocation('log_start')));
  const [timeEndDraft, setTimeEndDraft] = useState(() => logTimeInputValue(logTimeFromLocation('log_end')));
  const [timeStart, setTimeStart] = useState(() => logTimeFromLocation('log_start'));
  const [timeEnd, setTimeEnd] = useState(() => logTimeFromLocation('log_end'));
  const [timeError, setTimeError] = useState('');
  const [cursor, setCursor] = useState<string | null>(initialCursor);
  const [newerCursors, setNewerCursors] = useState<Array<string | null>>([]);
  const [page, setPage] = useState<Json | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [contextSeq, setContextSeq] = useState<number | null>(logSeqFromLocation);
  const [contextCursor, setContextCursor] = useState<string | null>(null);
  const [contextNewerCursors, setContextNewerCursors] = useState<Array<string | null>>([]);
  const [contextDetail, setContextDetail] = useState<Json | null>(null);
  const [detailError, setDetailError] = useState('');
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const [details, setDetails] = useState<Record<number, Json>>({});
  const [rowDetailErrors, setRowDetailErrors] = useState<Record<number, string>>({});
  const requestVersion = useRef(0);
  const contextVersion = useRef(0);
  const detailVersion = useRef(0);
  const anchorRef = useRef<HTMLElement>(null);
  const firstFilterReset = useRef(true);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQuery(query);
      const url = new URL(window.location.href);
      if (query) url.searchParams.set('log_q', query);
      else url.searchParams.delete('log_q');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    }, 320);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const sync = () => setContextSeq(logSeqFromLocation());
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);

  useEffect(() => {
    setContextCursor(null);
    setContextNewerCursors([]);
    setPage(null);
    if (contextSeq == null) {
      contextVersion.current += 1;
      setContextDetail(null); setDetailError('');
      return;
    }
    const version = ++contextVersion.current;
    setDetailError(''); setContextDetail(null);
    void api<Json>('/api/admin/interactions/' + contextSeq).then(detail => {
      if (version !== contextVersion.current) return;
      setContextDetail(detail);
    }).catch(reason => {
      if (version !== contextVersion.current) return;
      setDetailError(reason instanceof Error ? reason.message : t('日志详情加载失败'));
    });
  }, [contextSeq]);

  useEffect(() => {
    if (contextSeq == null || !page || !anchorRef.current) return;
    anchorRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [contextSeq, page]);

  const applyHistoryFilters = () => {
    const normalize = (value: string) => value.trim().replace(/^0+(?=\d)/, '');
    const start = normalize(seqStartDraft);
    const end = normalize(seqEndDraft);
    if ((start && !/^\d+$/.test(start)) || (end && !/^\d+$/.test(end))) {
      setSequenceError(t('序列号必须是非负整数。'));
      return;
    }
    if (start && end && (start.length > end.length || (start.length === end.length && start > end))) {
      setSequenceError(t('序列下限不能大于序列上限。'));
      return;
    }
    const draftedTimeStart = timeStartDraft.trim();
    const draftedTimeEnd = timeEndDraft.trim();
    if (Boolean(draftedTimeStart) !== Boolean(draftedTimeEnd)) {
      setTimeError(t('日志起止时间必须同时提供'));
      return;
    }
    const normalizedTimeStart = draftedTimeStart ? pastedLogTimeToInput(draftedTimeStart, 'start') : '';
    const normalizedTimeEnd = draftedTimeEnd ? pastedLogTimeToInput(draftedTimeEnd, 'end') : '';
    if ((draftedTimeStart && !normalizedTimeStart) || (draftedTimeEnd && !normalizedTimeEnd)) {
      setTimeError(t('无法识别粘贴的日志时间'));
      return;
    }
    const selectedTimeStart = normalizedTimeStart ? localDateTimeInputToIso(normalizedTimeStart) : '';
    const selectedTimeEnd = normalizedTimeEnd ? localDateTimeInputToIso(normalizedTimeEnd) : '';
    if (selectedTimeStart && selectedTimeEnd) {
      const startTimestamp = timestampMillis(selectedTimeStart);
      const endTimestamp = timestampMillis(selectedTimeEnd);
      if (startTimestamp == null || endTimestamp == null || startTimestamp > endTimestamp) {
        setTimeError(t('日志起始时间不能晚于结束时间'));
        return;
      }
      if (endTimestamp - startTimestamp > 86_400_000) {
        setTimeError(t('日志时间范围不能超过 24 小时'));
        return;
      }
    }
    setSeqStartDraft(start);
    setSeqEndDraft(end);
    setSeqStart(start);
    setSeqEnd(end);
    setSequenceError('');
    setTimeStartDraft(logTimeInputValue(selectedTimeStart));
    setTimeEndDraft(logTimeInputValue(selectedTimeEnd));
    setTimeStart(selectedTimeStart);
    setTimeEnd(selectedTimeEnd);
    setTimeError('');
    const url = new URL(window.location.href);
    if (selectedTimeStart && selectedTimeEnd) {
      url.searchParams.set('log_start', selectedTimeStart);
      url.searchParams.set('log_end', selectedTimeEnd);
    } else {
      url.searchParams.delete('log_start');
      url.searchParams.delete('log_end');
    }
    if (start) url.searchParams.set('log_seq_start', start);
    else url.searchParams.delete('log_seq_start');
    if (end) url.searchParams.set('log_seq_end', end);
    else url.searchParams.delete('log_seq_end');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };

  const pasteTime = (boundary: 'start' | 'end') => (event: ReactClipboardEvent<HTMLInputElement>) => {
    const value = pastedLogTimeToInput(event.clipboardData.getData('text'), boundary);
    event.preventDefault();
    if (!value) {
      setTimeError(t('无法识别粘贴的日志时间'));
      return;
    }
    if (boundary === 'start') setTimeStartDraft(editableLogTimeValue(value));
    else setTimeEndDraft(editableLogTimeValue(value));
    setTimeError('');
  };

  useEffect(() => {
    if (firstFilterReset.current) {
      firstFilterReset.current = false;
      return;
    }
    setCursor(null);
    setNewerCursors([]);
    setPage(null);
    const url = new URL(window.location.href);
    url.searchParams.delete('log_cursor');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }, [type, debouncedQuery, seqStart, seqEnd, timeStart, timeEnd]);

  useEffect(() => {
    const version = ++requestVersion.current;
    const controller = new AbortController();
    const activeCursor = contextSeq == null ? cursor : contextCursor;
    const filtersActive = contextSeq == null && Boolean(type || debouncedQuery || seqStart || seqEnd || timeStart || timeEnd);
    setLoading(true);
    setError('');
    void loadInteractionHistoryPage({
      cursor: activeCursor,
      filtersActive,
      fetchPage: pageCursor => {
        const params = new URLSearchParams({ limit: '100' });
        if (contextSeq != null) {
          params.set('seq_end', String(Math.min(Number.MAX_SAFE_INTEGER, contextSeq + 50)));
        } else {
          if (type) params.set('event_type', type);
          if (debouncedQuery) params.set('q', debouncedQuery);
          if (seqStart) params.set('seq_start', seqStart);
          if (seqEnd) params.set('seq_end', seqEnd);
          if (timeStart) params.set('start_time', timeStart);
          if (timeEnd) params.set('end_time', timeEnd);
        }
        if (pageCursor) params.set('cursor', pageCursor);
        return api<Json>('/api/admin/interactions?' + params.toString(), { signal: controller.signal });
      },
    })
      .then(result => {
        if (version !== requestVersion.current) return;
        setPage(result);
        setOpenSeq(null);
      })
      .catch(reason => {
        if (version !== requestVersion.current) return;
        if (reason instanceof DOMException && reason.name === 'AbortError') return;
        setError(reason instanceof Error ? reason.message : t('历史记录加载失败'));
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
    return () => controller.abort();
  }, [contextCursor, contextSeq, cursor, debouncedQuery, refreshKey, seqEnd, seqStart, timeEnd, timeStart, type]);

  const showOlder = () => {
    const next = typeof page?.next_cursor === 'string' ? page.next_cursor : null;
    if (!next || loading) return;
    if (contextSeq != null) {
      setContextNewerCursors(items => [...items, contextCursor]);
      setContextCursor(next);
      return;
    }
    setNewerCursors(items => [...items, cursor]);
    setCursor(next);
    const url = new URL(window.location.href);
    url.searchParams.set('log_cursor', next);
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };
  const showNewer = () => {
    const cursorStack = contextSeq == null ? newerCursors : contextNewerCursors;
    const previous = cursorStack[cursorStack.length - 1];
    if (previous === undefined || loading) return;
    if (contextSeq != null) {
      setContextNewerCursors(items => items.slice(0, -1));
      setContextCursor(previous);
      return;
    }
    setNewerCursors(items => items.slice(0, -1));
    setCursor(previous);
    const url = new URL(window.location.href);
    if (previous) url.searchParams.set('log_cursor', previous);
    else url.searchParams.delete('log_cursor');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };
  const openContext = (seq: number) => {
    if (!Number.isInteger(seq) || seq < 0) return;
    const url = new URL(window.location.href);
    url.searchParams.set('log_seq', String(seq));
    window.history.pushState(null, '', `${url.pathname}${url.search}${url.hash}`);
    setContextSeq(seq);
  };
  const toggleDetail = async (seq: number) => {
    if (!Number.isInteger(seq) || seq < 0) return;
    if (openSeq === seq) {
      setOpenSeq(null);
      return;
    }
    setOpenSeq(seq);
    if (details[seq]) return;
    const version = ++detailVersion.current;
    try {
      const detail = await api<Json>('/api/admin/interactions/' + seq);
      if (version !== detailVersion.current) return;
      setDetails(current => ({ ...current, [seq]: detail }));
      setRowDetailErrors(current => ({ ...current, [seq]: '' }));
    } catch (reason) {
      if (version !== detailVersion.current) return;
      setRowDetailErrors(current => ({ ...current, [seq]: reason instanceof Error ? reason.message : t('日志详情加载失败') }));
    }
  };
  const closeContext = () => {
    const url = new URL(window.location.href);
    url.searchParams.delete('log_seq');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    setContextSeq(null);
  };
  const clearHistorySearch = () => {
    setQuery('');
    setDebouncedQuery('');
    setType('');
    setSeqStartDraft('');
    setSeqEndDraft('');
    setSeqStart('');
    setSeqEnd('');
    setTimeStartDraft('');
    setTimeEndDraft('');
    setTimeStart('');
    setTimeEnd('');
    setSequenceError('');
    setTimeError('');
    setCursor(null);
    setNewerCursors([]);
    setContextSeq(null);
    setContextCursor(null);
    setContextNewerCursors([]);
    setOpenSeq(null);
    setPage(null);
    const url = new URL(window.location.href);
    ['log_start', 'log_end', 'log_type', 'log_seq', 'log_q', 'log_seq_start', 'log_seq_end', 'log_cursor'].forEach(key => url.searchParams.delete(key));
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };
  const bubbleHref = (bubble: Json) => {
    const url = new URL(window.location.href);
    url.searchParams.set('section', 'memory');
    url.searchParams.set('memory_tab', 'thoughts');
    if (bubble.scope === 'subconscious') url.searchParams.set('thought_scope', 'subconscious');
    else url.searchParams.delete('thought_scope');
    url.searchParams.set('bubble_id', String(bubble.id));
    ['runtime_tab', 'log_start', 'log_end', 'log_type', 'log_q', 'log_seq_start', 'log_seq_end', 'log_cursor', 'log_seq'].forEach(key => url.searchParams.delete(key));
    return `${url.pathname}${url.search}${url.hash}`;
  };
  const events = [...(page?.events || [])].reverse();
  const hasOlder = Boolean(page?.next_cursor);
  const effectiveCursor = contextSeq == null ? cursor : contextCursor;
  const canShowNewer = contextSeq == null ? newerCursors.length > 0 : contextNewerCursors.length > 0;
  const loadedLabel = page?.has_more
    ? t('本页 {{count}} 条，可继续向更早的记录回溯。', { count: events.length })
    : t('本页 {{count}} 条，已到最早记录。', { count: events.length });
  const sequenceScope = seqStart && seqEnd
    ? t('序列 {{start}} 至 {{end}}', { start: seqStart, end: seqEnd })
    : seqStart
      ? t('序列下限 {{start}}', { start: seqStart })
      : seqEnd
        ? t('序列上限 {{end}}', { end: seqEnd })
        : '';
  const timeScope = timeStart && timeEnd
    ? t('{{start}} 至 {{end}}', {
      start: formatDateTime(timeStart, dateLocale),
      end: formatDateTime(timeEnd, dateLocale),
    })
    : '';
  const activeScope = [timeScope, sequenceScope].filter(Boolean).join(' · ');
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
  const searchContinuation = contextSeq == null && events.length === 0 && hasOlder && (Boolean(type) || Boolean(debouncedQuery) || Boolean(activeScope));
  const filteredResults = shouldShowInteractionContextAction({
    contextSeq, type, query: debouncedQuery, seqStart, seqEnd, timeStart, timeEnd,
  });
  const startPickerValue = pastedLogTimeToInput(timeStartDraft, 'start');
  const endPickerValue = pastedLogTimeToInput(timeEndDraft, 'end');
  const hasHistorySearchState = Boolean(
    contextSeq != null || query || type || seqStartDraft || seqEndDraft
    || timeStartDraft || timeEndDraft || cursor,
  );
  const continuationLabel = activeScope
    ? t('继续查看范围内更早记录')
    : searchContinuation
      ? t('继续搜索更早日志')
      : t('查看更早记录');
  const emptyHistoryText = searchContinuation
    ? '这个扫描窗口里没有符合条件的记录；继续向更早的日志查找。'
    : filteredResults
      ? '当前筛选条件下没有匹配的日志。'
      : '这里还没有交互日志。';
  return <Panel
    title="生命全史日志"
    note="时间与序列范围都包含端点，结果从范围内最新记录开始分页；时间范围最多 24 小时。"
    action={<form className="log-filters history-log-filters" onSubmit={event => { event.preventDefault(); applyHistoryFilters(); }}>
      <select aria-label={t('筛选事件类型')} value={type} onChange={event => {
        const selectedType = event.target.value;
        setType(selectedType);
        const url = new URL(window.location.href);
        if (selectedType) url.searchParams.set('log_type', selectedType);
        else url.searchParams.delete('log_type');
        window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
      }}>
        <option value="">{t('全部事件')}</option>
        <option>message_in</option><option>thinking_start</option><option>llm_response</option><option>tool_call</option><option>tool_result</option><option>system_prompt</option><option>summary_llm_response</option><option>vision_llm_response</option><option>mem0_llm_response</option><option>memory_compression</option><option>subconscious_spawned</option><option>subconscious_done</option>
      </select>
      <input aria-label={t('过滤日志内容')} value={query} onChange={event => setQuery(event.target.value)} placeholder={t('过滤内容')} />
      <div className="history-time-range" aria-label={t('日志时间范围')}>
        <label><span>{t('开始')}</span><div className="history-editable-time"><input aria-label={t('日志开始时间')} title={t('可直接粘贴日志时间')} type="text" inputMode="numeric" autoComplete="off" placeholder="YYYY-MM-DD HH:mm:ss" value={timeStartDraft} onPaste={pasteTime('start')} onChange={event => { setTimeStartDraft(event.target.value); setTimeError(''); }} /><CalendarDays size={13} aria-hidden="true" /><input className="history-native-time-picker" aria-label={t('选择日志开始时间')} lang={dateLocale} type="datetime-local" step="any" value={startPickerValue} onChange={event => { setTimeStartDraft(editableLogTimeValue(event.target.value)); setTimeError(''); }} /></div></label>
        <span className="sequence-separator" aria-hidden="true">–</span>
        <label><span>{t('结束')}</span><div className="history-editable-time"><input aria-label={t('日志结束时间')} title={t('可直接粘贴日志时间')} type="text" inputMode="numeric" autoComplete="off" placeholder="YYYY-MM-DD HH:mm:ss" value={timeEndDraft} onPaste={pasteTime('end')} onChange={event => { setTimeEndDraft(event.target.value); setTimeError(''); }} /><CalendarDays size={13} aria-hidden="true" /><input className="history-native-time-picker" aria-label={t('选择日志结束时间')} lang={dateLocale} type="datetime-local" step="any" min={startPickerValue || undefined} value={endPickerValue} onChange={event => { setTimeEndDraft(editableLogTimeValue(event.target.value)); setTimeError(''); }} /></div></label>
      </div>
      <div className="sequence-range" aria-label={t('序列范围')}>
        <label><span>{t('序列下限')}</span><input aria-label={t('序列下限')} type="number" min="0" step="1" inputMode="numeric" value={seqStartDraft} onChange={event => { setSeqStartDraft(event.target.value); setSequenceError(''); }} placeholder="0" /></label>
        <span className="sequence-separator" aria-hidden="true">–</span>
        <label><span>{t('序列上限')}</span><input aria-label={t('序列上限')} type="number" min="0" step="1" inputMode="numeric" value={seqEndDraft} onChange={event => { setSeqEndDraft(event.target.value); setSequenceError(''); }} placeholder={t('当前')} /></label>
      </div>
      <button className="ghost mini sequence-locate" type="submit">{t('应用范围')}</button>
      {hasHistorySearchState && <button className="ghost mini history-clear-filters" type="button" onClick={clearHistorySearch}><X size={13} />{t('清除筛选')}</button>}
      <button className="icon-btn" type="button" aria-label={t('刷新生命全史日志')} title={t('刷新生命全史日志')} onClick={() => setRefreshKey(value => value + 1)}><RefreshCw size={15} /></button>
    </form>}
  >
    {sequenceError && <div className="notice error history-sequence-error">{sequenceError}</div>}
    {timeError && <div className="notice error history-sequence-error">{timeError}</div>}
    <div className="history-navigator">
      <div className="history-position"><span className={effectiveCursor ? 'history-marker earlier' : 'history-marker'}><Clock3 size={15} /></span><div><b>{contextSeq != null ? effectiveCursor ? t('从日志 #{{seq}} 继续回溯', { seq: contextSeq }) : t('已定位日志 #{{seq}}', { seq: contextSeq }) : cursor ? t('正在回溯更早的记录') : activeScope ? t('已应用日志范围') : t('最新记录')}</b><div className="history-detail-line"><small>{contextSeq != null ? t('目标日志位于这一页，并按正常生命全史顺序展示上下文。') + ' · ' + loadedLabel : activeScope ? activeScope + ' · ' + loadedLabel : loadedLabel}</small>{lifetimeSequenceLabel && <span className="history-sequence-total">{lifetimeSequenceLabel}</span>}</div></div></div>
      <div className="history-actions">{contextSeq != null && <button className="ghost mini" onClick={closeContext}><X size={13} />{t('返回筛选结果')}</button>}<button className="ghost mini" disabled={!canShowNewer || loading} onClick={showNewer}><ChevronRight size={14} />{t('较新')}</button><button className="ghost mini" disabled={!hasOlder || loading} onClick={showOlder}><ChevronLeft size={14} />{contextSeq != null ? t('查看更早记录') : continuationLabel}</button></div>
    </div>
    {loading && !page ? <Loading error={error} /> : error ? <Loading error={error} /> : <div className="log-table lifecycle-log-table"><div className="log-head" aria-hidden="true"><b>{t('时间')}</b><b>{t('事件')}</b><b>{t('内容')}</b></div>{events.length ? events.map((event: Json) => {
      const seq = Number(event.seq);
      const anchored = contextSeq === seq;
      const rowOpen = openSeq === seq;
      const rowDetail = details[seq];
      const rowDetailError = rowDetailErrors[seq];
      const meta = Object.entries(event.meta || {}).map(([key, value]) => key + ': ' + value).join(' · ');
      return <article ref={anchored ? anchorRef : undefined} key={String(event.seq) + '-' + event.type} className={[anchored ? 'context-anchor' : '', anchored || rowOpen ? 'open' : ''].filter(Boolean).join(' ')}><time title={String(event.ts || '')}>{formatDateTime(event.ts, dateLocale)}</time><span className={'event-type ' + event.type}>{event.type}</span><div className="interaction-row-copy"><code title={event.preview}>{event.preview}</code>{meta && <small>{meta}</small>}{event.bubble && <a className="bubble-log-link" href={bubbleHref(event.bubble)}><Orbit size={12} />{t('查看 Bubble {{id}}', { id: event.bubble.bubble_id || event.bubble.id })}<ExternalLink size={11} /></a>}</div>{anchored ? <span className="context-anchor-label">{t('当前日志')}</span> : Number.isInteger(seq) && <div className="interaction-row-actions"><button className="ghost mini interaction-detail-toggle" onClick={() => void toggleDetail(seq)}>{t(rowOpen ? '收起' : '详情')}</button>{filteredResults && <button className="ghost mini interaction-context-toggle" onClick={() => openContext(seq)}>{t('查看上下文')}</button>}</div>}{anchored && <div className="interaction-detail">{detailError ? <p className="notice error">{detailError}</p> : contextDetail ? <><pre>{JSON.stringify(contextDetail.entry, null, 2)}</pre>{contextDetail.truncated && <small>{t('为了保持页面流畅，这条超长记录已在详情中截断。')}</small>}</> : <div className="bubble-history-loading">{t('正在读取日志详情…')}</div>}</div>}{!anchored && rowOpen && <div className="interaction-detail">{rowDetailError ? <p className="notice error">{rowDetailError}</p> : rowDetail ? <><pre>{JSON.stringify(rowDetail.entry, null, 2)}</pre>{rowDetail.truncated && <small>{t('为了保持页面流畅，这条超长记录已在详情中截断。')}</small>}</> : <div className="bubble-history-loading">{t('正在读取日志详情…')}</div>}</div>}</article>;
    }) : <div className="history-empty"><Empty text={emptyHistoryText} /></div>}</div>}
  </Panel>;
}

function DangerAction({ title, description, button, confirmationName, onConfirm }: { title: string; description: string; button: string; confirmationName: string; onConfirm: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [done, setDone] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const close = () => {
    if (busy) return;
    setOpen(false);
    setTyped('');
    setError('');
  };
  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      await onConfirm();
      setOpen(false);
      setTyped('');
      setDone(t('操作已提交'));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t('请求失败'));
    } finally {
      setBusy(false);
    }
  };
  return <article className="danger-card">
    <TriangleAlert size={20} />
    <div><b>{t(title)}</b><p>{t(description)}</p>{done && <small>{done}</small>}</div>
    <button className="danger-outline" onClick={() => { setOpen(true); setDone(''); }}>{t(button)}</button>
    {open && <div className="modal-layer"><div className="confirm-modal">
      <TriangleAlert size={28} /><h3>{t(title)}</h3><p>{t(description)}</p>
      <Field label={t('输入“{{name}}”以确认', { name: confirmationName })}><input autoFocus disabled={busy} value={typed} onChange={event => setTyped(event.target.value)} /></Field>
      {error && <div className="notice error">{error}</div>}
      <div className="panel-actions"><button className="ghost" disabled={busy} onClick={close}>{t('取消')}</button><button className="danger-solid" disabled={busy || !confirmationName || typed !== confirmationName} onClick={() => void submit()}>{busy ? t('正在执行…') : t(button)}</button></div>
    </div></div>}
  </article>;
}

function Maintenance({ confirmationName }: { confirmationName: string }) {
  const backups = useLoad(() => api<Json>('/api/admin/backups'), []);
  return <div className="page-stack"><Panel title="应急备份" note="摘要恢复会把备份压缩后注入 inbox；完整恢复会替换当前短期上下文。"><div className="record-list">{backups.data?.backups?.length ? backups.data.backups.map((backup: Json) => <article className="record" key={backup.filename}><div><b>{backup.filename}</b><small>{backup.timestamp ? formatDateTime(backup.timestamp) : t('时间未知')}{' · '}{t('{{count}} 条消息', { count: backup.message_count ?? '—' })}</small></div><div className="row-actions"><button className="ghost" onClick={async () => { if (confirm(t('以摘要方式吸收备份 {{filename}}？', { filename: backup.filename }))) { await api('/api/admin/backups/restore', { method: 'POST', body: JSON.stringify({ filename: backup.filename, mode: 'summarize' }) }); } }}>{t('摘要恢复')}</button><BackupFullRestore filename={backup.filename} confirmationName={confirmationName} /></div></article>) : <Empty text="当前没有应急备份。" />}</div></Panel><Panel title="维护舱" note="重启会改变运行状态，因此需要明确确认。"><div className="danger-list"><DangerAction title="安全重启 Coworker" description="保存完整短期快照并重启进程。正在运行的 Bubble 会被取消，页面连接会短暂断开。" button="安全重启" confirmationName={confirmationName} onConfirm={() => api('/api/admin/restart', { method: 'POST', body: JSON.stringify({ confirm_name: confirmationName }) })} /></div></Panel></div>;
}

function BackupFullRestore({ filename, confirmationName }: { filename: string; confirmationName: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const close = () => {
    if (busy) return;
    setOpen(false);
    setTyped('');
    setError('');
  };
  const restore = async () => {
    setBusy(true);
    setError('');
    try {
      await api('/api/admin/backups/restore', { method: 'POST', body: JSON.stringify({ filename, mode: 'full', confirm_name: confirmationName }) });
      setOpen(false);
      setTyped('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t('请求失败'));
    } finally {
      setBusy(false);
    }
  };
  return <>
    <button className="danger-outline" onClick={() => setOpen(true)}>{t('完整恢复')}</button>
    {open && <div className="modal-layer"><div className="confirm-modal">
      <TriangleAlert size={28} /><h3>{t('完整恢复备份')}</h3><p>{t('用 {{filename}} 替换当前短期上下文；现有上下文会被覆盖。', { filename })}</p>
      <Field label={t('输入“{{name}}”以确认', { name: confirmationName })}><input autoFocus disabled={busy} value={typed} onChange={event => setTyped(event.target.value)} /></Field>
      {error && <div className="notice error">{error}</div>}
      <div className="panel-actions"><button className="ghost" disabled={busy} onClick={close}>{t('取消')}</button><button className="danger-solid" disabled={busy || !confirmationName || typed !== confirmationName} onClick={() => void restore()}>{busy ? t('正在恢复…') : t('完整恢复')}</button></div>
    </div></div>}
  </>;
}

function Identity({ onIdentity }: { onIdentity: (identity: AdminIdentity) => void }) {
  const identity = useLoad(() => api<Json>('/api/admin/identity'), []);
  const systemPrompt = useLoad(() => api<Json>('/api/admin/system-prompt'), []);
  const [draft, setDraft] = useState<Json | null>(null);
  const [saved, setSaved] = useState(false);
  const [promptDraft, setPromptDraft] = useState<string | null>(null);
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptMessage, setPromptMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const promptEditor = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => { if (identity.data) setDraft({ ...identity.data }); }, [identity.data]);
  useEffect(() => {
    if (systemPrompt.data) setPromptDraft(String(systemPrompt.data.desired_template || systemPrompt.data.default_template || ''));
  }, [systemPrompt.data]);
  const identityDirty = Boolean(identity.data && draft && ['name', 'current_location', 'personality'].some(key => (draft[key] || '') !== (identity.data?.[key] || '')));
  const promptDirty = Boolean(systemPrompt.data && promptDraft !== null && promptDraft !== String(systemPrompt.data.desired_template || ''));
  useNavigationGuard('identity', identityDirty || promptDirty);
  if (identity.loading || !draft) return <Loading error={identity.error} />;
  const save = async () => {
    await api<Json>('/api/admin/identity', { method: 'PUT', body: JSON.stringify(draft) });
    const session = await api<AdminIdentity>('/api/admin/session/verify', { method: 'POST' });
    onIdentity(session);
    setSaved(true);
    await Promise.all([identity.reload(), systemPrompt.reload()]);
  };
  const savePrompt = async () => {
    if (!systemPrompt.data || promptDraft === null || !promptDraft.trim()) {
      setPromptMessage({ kind: 'error', text: t('完整替换模式需要填写非空 System Prompt。') });
      return;
    }
    setPromptSaving(true);
    setPromptMessage(null);
    try {
      const storedTemplate = promptDraft === systemPrompt.data.default_template ? '' : promptDraft;
      await api('/api/admin/config', { method: 'PATCH', body: JSON.stringify({ changes: { agent: { system_prompt_template: storedTemplate } } }) });
      setPromptMessage({ kind: 'success', text: t('模板已保存，将在安全重启后用于所有新推理。') });
      await systemPrompt.reload();
    } catch (error) {
      setPromptMessage({ kind: 'error', text: error instanceof Error ? error.message : t('保存失败') });
    } finally {
      setPromptSaving(false);
    }
  };
  const restoreInheritedPrompt = async () => {
    setPromptSaving(true);
    setPromptMessage(null);
    try {
      await api('/api/admin/config', { method: 'PATCH', body: JSON.stringify({ clear_overrides: ['agent.system_prompt_template'] }) });
      setPromptMessage({ kind: 'success', text: t('管理端模板覆盖已清除，将在安全重启后恢复启动配置。') });
      await systemPrompt.reload();
    } catch (error) {
      setPromptMessage({ kind: 'error', text: error instanceof Error ? error.message : t('保存失败') });
    } finally {
      setPromptSaving(false);
    }
  };
  const insertPromptVariable = (name: string) => {
    const token = `{{${name}}}`;
    const editor = promptEditor.current;
    const current = promptDraft || '';
    const start = editor?.selectionStart ?? current.length;
    const end = editor?.selectionEnd ?? start;
    const prefix = start > 0 && current[start - 1] !== '\n' ? '\n' : '';
    const suffix = end < current.length && current[end] !== '\n' ? '\n' : '';
    const inserted = `${prefix}${token}${suffix}`;
    setPromptDraft(current.slice(0, start) + inserted + current.slice(end));
    requestAnimationFrame(() => {
      const cursor = start + inserted.length;
      promptEditor.current?.focus();
      promptEditor.current?.setSelectionRange(cursor, cursor);
    });
  };
  const promptVariables = (systemPrompt.data?.variables || []) as string[];
  const promptData = systemPrompt.data;
  const sectionPreviews = (promptData?.section_previews || promptVariables.map(name => ({
    name,
    variable: name,
    content_variable: `${name}_CONTENT`,
    full_text: '',
    content: '',
    available: false,
    lines: 0,
  }))) as PromptSectionPreview[];
  const usedPromptSections = new Set(sectionPreviews.filter(section => (
    promptDraft?.includes(`{{${section.variable}}}`)
    || promptDraft?.includes(`{{${section.content_variable}}}`)
  )).map(section => section.name));
  const usesBuiltInSections = usedPromptSections.size > 0;
  const promptLines = (promptDraft || '').split('\n');
  const blankPromptLines = promptLines.filter(line => !line.trim()).length;
  return <div className="page-stack">
    <Panel title="身份档案" note="保存会直接写入身份文件，并立即刷新 System Prompt 缓存以保持一致；后续推理将使用新身份。"><div className="identity-form"><Field label="姓名"><input value={draft.name || ''} onChange={event => setDraft({ ...draft, name: event.target.value })} /></Field><Field label="现居地"><input value={draft.current_location || ''} onChange={event => setDraft({ ...draft, current_location: event.target.value })} /></Field><Field label="人格"><textarea value={draft.personality || ''} onChange={event => setDraft({ ...draft, personality: event.target.value })} /></Field></div>{saved && <div className="notice success">{t('身份档案与 System Prompt 缓存已同步更新。')}</div>}<div className="panel-actions"><button className="primary" onClick={() => void save()}><Save size={15} />{t('保存档案')}</button></div></Panel>
    <Panel title="System Prompt 模板" note="通过只读分段变量组合 Prompt；自定义正文和方括号标题会原样保留。保存后需要安全重启。">
      {systemPrompt.loading || promptDraft === null || !promptData ? <Loading error={systemPrompt.error} /> : <div className="system-prompt-template-workbench">
        <div className="system-prompt-template-status"><span><b>{t(promptData.overridden ? '管理端覆盖' : '继承启动配置')}</b><small>{t(promptData.overridden ? '当前期望模板保存在 admin_config.json。' : '当前没有管理端模板覆盖。')}</small></span><em className={promptData.prompt_pending_restart ? 'pending' : 'active'}>{t(promptData.prompt_pending_restart ? '等待安全重启' : '当前已生效')}</em></div>
        {promptData.prompt_pending_restart && <div className="notice amber"><TriangleAlert size={16} /><span>{t('当前运行仍使用旧模板；安全重启后，主 Agent、Bubble 和潜意识会统一使用新模板。')}</span><a className="ghost mini" href="?section=runtime&runtime_tab=maintenance">{t('前往安全重启')}</a></div>}
        {!usesBuiltInSections && promptDraft.trim() && <div className="notice amber"><TriangleAlert size={16} /><span>{t('此模板没有引用任何内置分段，将完全替换 Identity、语言策略、Skill、Palace 和 Channel 指引。')}</span></div>}
        <div className="system-prompt-presets"><span>{t('快捷预设')}</span><button type="button" className="ghost mini" onClick={() => setPromptDraft(String(promptData.default_template || ''))}>{t('恢复标准模板')}</button><button type="button" className="ghost mini" onClick={() => setPromptDraft(`${promptData.default_template}\n\n[CUSTOM]\n`)}>{t('在标准模板后追加')}</button><button type="button" className="ghost mini" onClick={() => setPromptDraft('')}>{t('完全替换')}</button></div>
        <div className="system-prompt-template-grid">
          <div className="system-prompt-variable-list">
            <b>{t('可用分段变量')}</b>
            <small>{t('展开区段可预览当前运行内容，并选择插入完整区段或仅正文；同一区段只能选择一种。')}</small>
            {sectionPreviews.map(section => <details className="system-prompt-variable-card" key={section.name}>
              <summary><span><code>{`{{${section.variable}}}`}</code><small>{t(SYSTEM_PROMPT_VARIABLE_DESCRIPTIONS[section.name] || '')}</small></span><em>{section.available ? t('{{count}} 行', { count: section.lines }) : t('当前为空')}</em></summary>
              <div className="system-prompt-variable-card-body">
                <div className="system-prompt-variable-actions">
                  <button type="button" disabled={usedPromptSections.has(section.name)} onClick={() => insertPromptVariable(section.variable)}><Plus size={12} /><span>{t('完整区段')}<code>{`{{${section.variable}}}`}</code></span></button>
                  <button type="button" disabled={usedPromptSections.has(section.name)} onClick={() => insertPromptVariable(section.content_variable)}><Plus size={12} /><span>{t('仅正文')}<code>{`{{${section.content_variable}}}`}</code></span></button>
                </div>
                {!section.available ? <p>{t('当前运行实例中此可选区段为空；渲染时会自动省略。')}</p> : <div className="system-prompt-section-previews">
                  <span>{t('完整区段')}</span><pre>{section.full_text}</pre>
                  <span>{t('仅正文')}</span><pre>{section.content}</pre>
                </div>}
              </div>
            </details>)}
          </div>
          <label className="system-prompt-template-editor">
            <span>{t('期望模板')}<small>{t('{{lines}} 行 · {{blank}} 个空白行 · {{count}} / 100000 字符', { lines: promptLines.length, blank: blankPromptLines, count: promptDraft.length })}</small></span>
            <LineNumberTextarea ref={promptEditor} wrapperClassName="system-prompt-line-number-field" showSummary={false} maxLength={100000} spellCheck={false} value={promptDraft} aria-label={t('System Prompt 期望模板')} onChange={event => { setPromptDraft(event.target.value); setPromptMessage(null); }} placeholder={t('填写完整 System Prompt，或插入左侧分段变量。')} />
          </label>
        </div>
        <div className="system-prompt-template-help"><code>{'\\{{NAME}}'}</code><span>{t('用于输出字面量 {{NAME}}；未知、重复、未独占一行，或同一区段同时使用完整与正文变量都会被拒绝。')}</span></div>
        {promptMessage && <div className={`notice ${promptMessage.kind}`} role={promptMessage.kind === 'error' ? 'alert' : 'status'}>{promptMessage.text}</div>}
        <div className="panel-actions"><span className={'save-state ' + (promptDirty ? 'dirty' : '')}>{t(promptDirty ? '有未保存修改' : '当前模板草稿已同步')}</span><button type="button" className="primary" disabled={promptSaving || !promptDirty || !promptDraft.trim()} onClick={() => void savePrompt()}><Save size={15} />{t(promptSaving ? '正在保存…' : '保存模板')}</button><button type="button" className="ghost" disabled={promptSaving || !promptData.overridden} onClick={() => void restoreInheritedPrompt()}><RotateCcw size={15} />{t('恢复继承')}</button><button type="button" className="ghost" disabled={promptSaving || !promptDirty} onClick={() => setPromptDraft(String(promptData.desired_template || ''))}>{t('放弃修改')}</button></div>
      </div>}
    </Panel>
    <Panel title="当前 System Prompt" note="只读展示 Agent 当前实际使用的缓存版本；不包含工具 Schema、短期上下文或本轮消息。" action={<button className="ghost mini" disabled={systemPrompt.loading} onClick={() => void systemPrompt.reload()}><RefreshCw size={14} />{t('重新读取')}</button>}>
      {systemPrompt.loading || !promptData ? <Loading error={systemPrompt.error} /> : <><div className="system-prompt-facts"><span><b>{promptData.characters ?? 0}</b>{t('字符')}</span><span><b>{promptData.lines ?? 0}</b>{t('行')}</span><em>{t('只读')}</em></div><details className="system-prompt-preview"><summary><FileText size={16} /><span>{t('展开完整 System Prompt')}</span><small>{t('内容可选择复制，但不能在这里编辑')}</small></summary><pre tabIndex={0}><code>{promptData.content || ''}</code></pre></details></>}
    </Panel>
  </div>;
}

type PersonAliasView = {
  participant_id: string;
  conversation_id?: string | null;
  channel: string;
  notes: string[];
};
type PersonView = {
  person_id: string;
  display_name: string;
  notes: string[];
  aliases: PersonAliasView[];
  created_at: string;
  updated_at: string;
};

function personMatchesQuery(person: PersonView, query: string) {
  return [
    person.display_name,
    person.person_id,
    ...person.aliases.flatMap(alias => [alias.channel, alias.participant_id, alias.conversation_id || '']),
  ].some(value => value.toLocaleLowerCase().includes(query));
}

function PeopleView() {
  const people = useLoad(() => api<{ persons: PersonView[] }>('/api/admin/persons'), []);
  const [draftName, setDraftName] = useState('');
  const [personQuery, setPersonQuery] = useState('');
  const [addingPerson, setAddingPerson] = useState(false);
  const [selectedPersonId, setSelectedPersonId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [nameDraft, setNameDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cardLoading, setCardLoading] = useState(false);
  const [renderedCard, setRenderedCard] = useState('');
  const [notesDraft, setNotesDraft] = useState('');
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [mergeTargetId, setMergeTargetId] = useState('');

  useEffect(() => {
    const data = people.data;
    if (!data) return;
    setSelectedPersonId(current => (
      current && data.persons.some(person => person.person_id === current)
        ? current
        : data.persons[0]?.person_id ?? null
    ));
  }, [people.data]);

  useEffect(() => {
    const data = people.data;
    const query = personQuery.trim().toLocaleLowerCase();
    if (!data || !query) return;
    const matches = data.persons.filter(person => personMatchesQuery(person, query));
    if (!matches.length) return;
    setSelectedPersonId(current => current && matches.some(person => person.person_id === current)
      ? current
      : matches[0].person_id);
  }, [people.data, personQuery]);

  const selectedPerson = people.data?.persons.find(person => person.person_id === selectedPersonId) ?? null;

  useEffect(() => {
    if (!selectedPerson) {
      setRenderedCard('');
      setNotesDraft('');
      setCardLoading(false);
      return;
    }
    let active = true;
    setError(null);
    setCardLoading(true);
    setNotesDraft((selectedPerson.notes ?? []).join('\n'));
    api<{ content: string }>(`/api/admin/persons/${selectedPerson.person_id}/card`)
      .then(card => { if (active) setRenderedCard(card.content); })
      .catch(loadError => { if (active) setError(String(loadError)); })
      .finally(() => { if (active) setCardLoading(false); });
    return () => { active = false; };
  }, [selectedPerson]);

  useEffect(() => {
    setRenamingId(null);
    setDeletingId(null);
    setMergeTargetId('');
  }, [selectedPersonId]);

  if (people.loading || !people.data) return <Loading error={people.error} />;

  const create = async () => {
    const name = draftName.trim();
    if (!name) return;
    setBusy(true); setError(null);
    try {
      const created = await api<PersonView>('/api/admin/persons', { method: 'POST', body: JSON.stringify({ display_name: name }) });
      setDraftName('');
      setPersonQuery('');
      setAddingPerson(false);
      setSelectedPersonId(created.person_id);
      await people.reload();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const startRename = (person: PersonView) => {
    setRenamingId(person.person_id);
    setNameDraft(person.display_name);
  };

  const rename = async (person: PersonView) => {
    const name = nameDraft.trim();
    if (name === person.display_name) {
      setRenamingId(null);
      return;
    }
    setBusy(true); setError(null);
    try {
      await api<Json>(`/api/admin/persons/${person.person_id}`, { method: 'PATCH', body: JSON.stringify({ display_name: name }) });
      setRenamingId(null);
      await people.reload();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const remove = async (person: PersonView) => {
    setBusy(true); setError(null);
    try {
      await api<Json>(`/api/admin/persons/${person.person_id}`, { method: 'DELETE' });
      setDeletingId(null);
      setSelectedPersonId(null);
      await people.reload();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const merge = async (keep: PersonView) => {
    if (!mergeTargetId) return;
    setBusy(true); setError(null);
    try {
      await api<Json>(`/api/admin/persons/${keep.person_id}/merge`, { method: 'POST', body: JSON.stringify({ other_person_id: mergeTargetId }) });
      setMergeTargetId('');
      await people.reload();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const saveNotes = async () => {
    if (!selectedPerson) return;
    setBusy(true); setError(null);
    try {
      const notes = notesDraft.split('\n').map(s => s.trim()).filter(Boolean);
      await api<Json>(`/api/admin/persons/${selectedPerson.person_id}`, { method: 'PATCH', body: JSON.stringify({ notes }) });
      await people.reload();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const others = selectedPerson
    ? people.data.persons.filter(person => person.person_id !== selectedPerson.person_id)
    : [];
  const normalizedQuery = personQuery.trim().toLocaleLowerCase();
  const visiblePeople = normalizedQuery
    ? people.data.persons.filter(person => personMatchesQuery(person, normalizedQuery))
    : people.data.persons;
  const notesChanged = selectedPerson
    ? notesDraft !== (selectedPerson.notes ?? []).join('\n')
    : false;
  const personName = (person: PersonView) => person.display_name || t('未命名人物');
  const personInitial = (person: PersonView) => Array.from(personName(person))[0]?.toUpperCase() || '?';

  return <div className="page-stack">
    <Panel title="通信录" note="人物是跨信道的「关系」：同一真人的多个地址绑定到一个 person_id；画像由搭档在对话中维护。" className="people-panel" action={<button className="ghost mini" disabled={people.loading} onClick={() => void people.reload()}><RefreshCw size={14} />{t('重新读取')}</button>}>
      {error && <div className="notice error">{error}</div>}
      <div className="people-workbench">
        <aside className="people-directory">
          <div className="people-directory-tools">
            <div className="person-search"><Search size={15} aria-hidden="true" /><input type="search" aria-label={t('搜索人物')} value={personQuery} onChange={event => setPersonQuery(event.target.value)} placeholder={t('搜索人物')} />{personQuery && <button type="button" className="person-search-clear" aria-label={t('清空搜索')} title={t('清空')} onClick={() => setPersonQuery('')}><X size={13} /></button>}</div>
            <button type="button" className={`person-add-toggle${addingPerson ? ' active' : ''}`} aria-expanded={addingPerson} aria-controls="person-create-form" aria-label={t('添加人物')} title={t('添加人物')} onClick={() => setAddingPerson(open => !open)}><Plus size={16} /></button>
          </div>
          {addingPerson && <div className="person-create" id="person-create-form">
            <label htmlFor="person-create-name">{t('添加人物')}</label>
            <div className="person-create-row"><input autoFocus id="person-create-name" value={draftName} onChange={event => setDraftName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && draftName.trim()) void create(); if (event.key === 'Escape') setAddingPerson(false); }} placeholder={t('输入人物称呼')} /><button className="primary" aria-label={t('新建人物')} title={t('新建人物')} disabled={busy || !draftName.trim()} onClick={() => void create()}><Check size={15} /></button></div>
          </div>}
          <div className="people-directory-heading"><span>{normalizedQuery ? t('搜索结果') : t('人物')}</span><b>{normalizedQuery ? `${visiblePeople.length}/${people.data.persons.length}` : people.data.persons.length}</b></div>
          {people.data.persons.length === 0 ? <div className="person-directory-empty">{t('暂无人物：搭档会在对话中通过 persona 工具建立')}</div> : visiblePeople.length === 0 ? <div className="person-directory-empty searched"><Search size={18} /><span>{t('没有匹配的人物')}</span></div> : <div className="person-list">{visiblePeople.map(person => {
            const selected = person.person_id === selectedPersonId;
            return <button type="button" className={`person-row${selected ? ' selected' : ''}`} aria-pressed={selected} onClick={() => setSelectedPersonId(person.person_id)} key={person.person_id}>
              <span className="person-avatar">{personInitial(person)}</span>
              <span className="person-list-copy"><b>{personName(person)}</b><small>{person.aliases.length ? t('{{count}} 个联系地址', { count: person.aliases.length }) : t('还没有联系地址')}</small></span>
              <ChevronRight size={15} />
            </button>;
          })}</div>}
        </aside>
        <section className="person-detail">
          {selectedPerson ? <>
            <header className="person-profile-head">
              <span className="person-avatar large">{personInitial(selectedPerson)}</span>
              <div className="person-identity-copy">
                {renamingId === selectedPerson.person_id ? <div className="person-name-editor">
                  <input autoFocus aria-label={t('人物名称')} value={nameDraft} onChange={event => setNameDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void rename(selectedPerson); if (event.key === 'Escape') setRenamingId(null); }} />
                  <span className="person-name-editor-actions">
                    <button type="button" className="person-name-confirm" aria-label={t('保存')} title={t('保存')} disabled={busy} onClick={() => void rename(selectedPerson)}><Check size={16} /></button>
                    <button type="button" className="person-name-cancel" aria-label={t('取消')} title={t('取消')} disabled={busy} onClick={() => setRenamingId(null)}><X size={15} /></button>
                  </span>
                </div> : <div className="person-name-display"><h3>{personName(selectedPerson)}</h3><button className="ghost mini" disabled={busy} onClick={() => startRename(selectedPerson)}><Pencil size={13} />{t('编辑名称')}</button></div>}
                <code className="person-id">{selectedPerson.person_id}</code>
              </div>
            </header>
            <section className="person-detail-section">
              <header><div><b>{t('联系地址')}</b><small>{t('来自不同信道、但属于同一个人的身份')}</small></div><span>{selectedPerson.aliases.length}</span></header>
              {selectedPerson.aliases.length > 0 ? <div className="person-aliases">{selectedPerson.aliases.map((alias, index) => <span className="alias-chip" key={index} title={[alias.conversation_id ? `conversation: ${alias.conversation_id}` : '', ...(alias.notes ?? [])].filter(Boolean).join('\n')}><em>{alias.channel || t('信道')}</em><code>{alias.participant_id}{alias.conversation_id ? ` · ${alias.conversation_id}` : ''}</code></span>)}</div> : <p className="person-section-empty">{t('还没有联系地址；搭档可以在对话中绑定。')}</p>}
            </section>
            <section className="person-detail-section person-card-section">
              <header><div><b>{t('人物画像')}</b><small>{t('画像由人物备注和联系地址共同组成')}</small></div><FileText size={16} /></header>
              <div className={`person-card-preview${cardLoading ? ' loading' : ''}`}>{cardLoading ? <Loading /> : <pre>{renderedCard || t('暂无记录')}</pre>}</div>
              <Field label={t('个性化备注（每行一条）')}><LineNumberTextarea rows={7} value={notesDraft} onChange={event => setNotesDraft(event.target.value)} placeholder={t('每行一条备注：称呼、关系、背景、偏好…')} /></Field>
              <div className="person-note-actions"><small>{t('保存后会立即更新人物画像')}</small><button className="primary" disabled={busy || !notesChanged} onClick={() => void saveNotes()}><Save size={15} />{t('保存备注')}</button></div>
            </section>
            <details className="person-maintenance">
              <summary><SlidersHorizontal size={15} /><span><b>{t('整理人物资料')}</b><small>{t('合并重复人物或删除当前人物')}</small></span><ChevronRight size={14} /></summary>
              <div>
                {others.length > 0 && <section><label>{t('把重复人物并入当前人物')}</label><div><select className="person-merge-select" value={mergeTargetId} onChange={event => setMergeTargetId(event.target.value)}><option value="">{t('选择要并入的人物…')}</option>{others.map(other => <option key={other.person_id} value={other.person_id}>{personName(other)}</option>)}</select><button className="ghost" disabled={busy || !mergeTargetId} onClick={() => void merge(selectedPerson)}>{t('合并资料')}</button></div><small>{t('当前 person_id 会保留，另一个人物的地址和备注会合并进来。')}</small></section>}
                <section className="person-delete-zone"><label>{t('删除当前人物')}</label><button className={deletingId === selectedPerson.person_id ? 'danger-solid' : 'danger-outline'} disabled={busy} onClick={() => { if (deletingId === selectedPerson.person_id) void remove(selectedPerson); else setDeletingId(selectedPerson.person_id); }}><Trash2 size={14} />{deletingId === selectedPerson.person_id ? t('确认删除？') : t('删除人物')}</button></section>
              </div>
            </details>
          </> : <div className="person-detail-empty"><Users size={28} /><b>{t('选择一个人物')}</b><p>{t('在左侧选择人物后，这里会显示名称、联系地址和画像。')}</p></div>}
        </section>
      </div>
    </Panel>
  </div>;
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
  if (kind === 'palaces') return '---\nname: ""\nwhen_to_attach: ""\ncritical_skills: []\nrelated_skills: []\nmemory_tags: []\n---\n\n# ' + t('领域说明') + '\n\n';
  if (kind === 'subconscious') return '---\nname: ""\nenabled: true\ntrigger: periodic\ncontext_builder: short_term\nevery_n_cycles: 40\nevery_seconds: 1800\nevery_n_tool_calls: 0\nmax_cycles: 5\ngoal: ""\npurpose: ""\n---\n\n# ' + t('思考方式') + '\n\n';
  return '---\nname: ""\ndescription: ""\nversion: 1.0.0\n---\n\n# ' + t('使用说明') + '\n\n';
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
  useNavigationGuard('content', dirty);
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
            <div className="source-toolbar"><div className="editor-file"><span className={'source-status ' + (dirty ? 'dirty' : '')} title={dirty ? t('有未保存修改') : t('内容已同步')} /><FileText size={14} /><code><b>{selected || newId || t('目录-id')}</b><i>/</i>{activeFile || CONTENT_KIND[kind].filename}</code></div><div className="source-readout"><span>YAML + MD</span><span>UTF-8</span><b>{t('{{count}} 行', { count: meta.lines })}</b><b>{t('{{count}} 个空白行', { count: raw ? raw.split('\n').filter(line => !line.trim()).length : 0 })}</b><b>{new Blob([raw]).size.toLocaleString()} B</b></div></div>
            <div className="source-schema"><span>{t('必填字段')}</span><code>{CONTENT_SOURCE_GUIDE[kind].required}</code><i /> <p>{t(CONTENT_SOURCE_GUIDE[kind].tip)}</p><kbd>Ctrl S</kbd></div>
            <LineNumberTextarea wrapperClassName="source-line-number-field" showSummary={false} className="source-editor" value={raw} onChange={event => { setRaw(event.target.value); setMessage(''); }} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if ((selected || newSourceReady) && raw.trim()) void save(); } }} spellCheck={false} aria-label={t('能力内容源码')} />
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
type DesktopAsset = { file: string; signature: string; kind: DesktopAssetKind; size: number; uploaded_at: string; sha256?: string };
type DesktopReleaseSource = { type: string; source_id?: string; api_base_url?: string; base_url?: string; repository?: string; tag?: string; html_url?: string; draft?: boolean; prerelease?: boolean; revision?: string; synced_at?: string };
type DesktopReleaseSummary = { version: string; notes: string; pub_date: string; published: boolean; platforms: string[]; installers: string[]; created_at: string; updated_at: string; source?: DesktopReleaseSource };
type DesktopRelease = Omit<DesktopReleaseSummary, 'platforms' | 'installers'> & { platforms: Record<string, DesktopAsset>; installers: Record<string, DesktopAsset> };
type DesktopReleaseList = { latest_version: string | null; releases: DesktopReleaseSummary[] };
type DesktopVersionCount = { version: string | null; desktops: number; active_desktops: number; outdated: boolean | null };
type DesktopVersionStatistics = {
  latest_version: string | null;
  total_desktops: number;
  active_desktops: number;
  outdated_desktops: number;
  unknown_version_desktops: number;
  versions: DesktopVersionCount[];
};
function releaseSourceLabel(source: DesktopReleaseSource) {
  if (source.type === 'coworker_release') return 'Coworker 发布同步';
  if (source.draft) return 'GitHub 草稿同步';
  if (source.prerelease) return 'GitHub 预发布同步';
  return 'GitHub Release 同步';
}
type DesktopSyncStatus = {
  enabled: boolean; ready: boolean; readiness?: string; token_configured?: boolean; active_source?: string | null; active_source_name?: string; active_source_type?: string;
  source?: { source_id: string; name: string; provider: string; endpoint: string; target?: string; options?: Record<string, boolean | string | number> } | null;
  outcome: string; run_id?: string | null; phase?: string; version?: string | null; asset?: string | null;
  bytes_downloaded?: number; bytes_total?: number; next_run_at?: string | null; last_success_at?: string | null;
  finished_at?: string | null; last_error?: string; imported_versions?: string[]; skipped_releases?: string[];
  rate_limit?: { limit?: number | null; remaining?: number | null; reset_at?: string | null; retry_after_seconds?: number | null };
};
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
  const download = (asset: DesktopAsset) => {
    const path = '/api/desktop-updates/assets/' + encodeURIComponent(version) + '/' + encodeURIComponent(asset.file);
    void downloadApiFile(path, asset.file).catch(error => window.alert(error instanceof Error ? error.message : t('下载失败')));
  };
  return <section className="release-asset-lane"><header><div><b>{t(title)}</b><small>{t(note)}</small></div><span>{entries.length}</span></header>{entries.length ? <div>{entries.map(([platform, asset]) => <article key={platform}><div className="asset-platform"><i /> <span>{platform}</span></div><div className="asset-file"><b title={asset.file}>{asset.file}</b><small>{formatBytes(asset.size)}{' · '}{asset.uploaded_at ? formatDateTime(asset.uploaded_at) : t('时间未知')}</small></div><button type="button" onClick={() => download(asset)} title={t('下载 {{file}}', { file: asset.file })}><Download size={14} /></button></article>)}</div> : <p className="release-lane-empty">{t('还没有这类产物')}</p>}</section>;
}

function DesktopVersionOverview({
  statistics,
  loading,
  error,
  onRefresh,
}: {
  statistics: DesktopVersionStatistics | null;
  loading: boolean;
  error: string;
  onRefresh: () => void;
}) {
  const total = statistics?.total_desktops || 0;
  return <section className="release-fleet" aria-labelledby="release-fleet-title">
    <header>
      <div><p className="eyebrow">{t('安装基线')}</p><h3 id="release-fleet-title">{t('客户端版本分布')}</h3></div>
      <button className="icon-btn" type="button" title={t('刷新版本统计')} aria-label={t('刷新版本统计')} onClick={onRefresh} disabled={loading}><RefreshCw size={14} /></button>
    </header>
    {error && !statistics ? <div className="release-fleet-state error"><TriangleAlert size={17} /><span>{error || t('版本统计读取失败')}</span></div>
    : loading && !statistics ? <div className="release-fleet-state"><RefreshCw className="spin" size={17} /><span>{t('正在读取客户端版本…')}</span></div>
    : !total ? <div className="release-fleet-state"><CircleGauge size={18} /><span><b>{t('暂无桌面注册')}</b>{t('桌面端连接后，版本分布会显示在这里。')}</span></div>
    : <div className="release-fleet-body">
      <div className="release-fleet-summary">
        <span><b>{total}</b>{t('台已注册桌面')}</span>
        <span className="online"><b>{statistics?.active_desktops || 0}</b>{t('台在线')}</span>
        <span className={statistics?.outdated_desktops ? 'outdated' : ''}><b>{statistics?.outdated_desktops || 0}</b>{t('台待更新')}</span>
      </div>
      <div className="release-version-tracks">
        {statistics?.versions.map(item => {
          const isLatest = Boolean(item.version && item.version === statistics.latest_version);
          const trackClass = isLatest ? 'latest' : item.outdated ? 'outdated' : item.version ? '' : 'unknown';
          return <div className={'release-version-track ' + trackClass} key={item.version || 'unknown'}>
            <div className="release-version-label"><b>{item.version ? `v${item.version}` : t('未知版本')}</b><span>{isLatest ? t('当前 latest') : item.outdated ? t('待更新') : ''}</span></div>
            <div className="release-version-rail" aria-hidden="true"><i style={{ width: `${Math.max(4, (item.desktops / total) * 100)}%` }} /></div>
            <small>{t('{{desktops}} 台 · {{active}} 在线', { desktops: item.desktops, active: item.active_desktops })}</small>
          </div>;
        })}
      </div>
    </div>}
  </section>;
}

function DesktopReleases() {
  const releases = useLoad(() => api<DesktopReleaseList>('/api/desktop-updates/releases'), []);
  const sync = useLoad(() => api<DesktopSyncStatus>('/api/admin/desktop-updates/sync'), []);
  const versionStatistics = useLoad(() => api<DesktopVersionStatistics>('/api/desktop-updates/statistics'), []);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncError, setSyncError] = useState('');
  const [syncMessage, setSyncMessage] = useState('');
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
    const interval = window.setInterval(() => { void sync.reload(); }, sync.data?.outcome === 'running' ? 2000 : 30000);
    return () => window.clearInterval(interval);
  }, [sync.data?.outcome, sync.reload]);

  useEffect(() => {
    const interval = window.setInterval(() => { void versionStatistics.reload(); }, 30000);
    return () => window.clearInterval(interval);
  }, [versionStatistics.reload]);

  const importedKey = (sync.data?.imported_versions || []).join('|');
  useEffect(() => {
    if (!importedKey || sync.data?.outcome === 'running') return;
    void releases.reload();
  }, [importedKey, sync.data?.outcome, releases.reload]);

  const triggerSync = async () => {
    setSyncBusy(true); setSyncError(''); setSyncMessage('');
    try {
      const result = await api<{ run_id: string; coalesced: boolean }>('/api/admin/desktop-updates/sync', { method: 'POST' });
      setSyncMessage(result.coalesced ? t('已有同步任务正在运行，已继续跟踪。') : t('已开始检查当前上游。'));
      await sync.reload();
    } catch (error) { setSyncError(error instanceof Error ? error.message : t('启动同步失败')); }
    finally { setSyncBusy(false); }
  };

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
  const syncStatus = sync.data;
  const syncRunning = syncStatus?.outcome === 'running';
  const syncProgress = syncStatus?.bytes_total ? Math.min(100, Math.round(((syncStatus.bytes_downloaded || 0) / syncStatus.bytes_total) * 100)) : 0;
  const syncOutcome = syncStatus?.outcome ? t(({ succeeded: '同步成功', not_modified: '上游没有变化', no_updates: '没有更高版本', conflict: '版本存在冲突', rate_limited: '上游请求受限', failed: '同步失败', interrupted: '同步已中断', running: '同步进行中', idle: '等待检测' } as Record<string, string>)[syncStatus.outcome] || syncStatus.outcome) : t('状态未知');
  const syncReady = !!(syncStatus?.enabled && syncStatus?.ready && syncStatus?.source);
  const settingsHref = syncStatus?.source?.source_id ? `?section=settings&group=desktop_updates&source=${encodeURIComponent(syncStatus.source.source_id)}` : '?section=settings&group=desktop_updates';
  return <div className="release-page page-stack">
    <section className={'release-hero ' + (latest ? 'ready' : 'empty')}>
      <div className="release-signal"><Rocket size={25} /><i /><i /></div>
      <div><p className="eyebrow">{t('桌面更新投放')}</p><h2>{heroTitle}</h2>{!latest && <p>{t('创建版本并上传签名产物后，从这里开启第一次投放。')}</p>}</div>
      <div className="release-hero-platforms"><span>{t('已投放平台')}</span><div>{latestPlatforms.length ? latestPlatforms.map(platform => <b key={platform}>{platform}</b>) : <small>{latest ? t('正在确认平台…') : t('尚未发布')}</small>}</div>{latestSummary?.updated_at && <time>{formatDateTime(latestSummary.updated_at)}</time>}</div>
    </section>
    <DesktopVersionOverview statistics={versionStatistics.data} loading={versionStatistics.loading} error={versionStatistics.error} onRefresh={() => { void versionStatistics.reload(); }} />
    <section className={'release-sync-card ' + (syncRunning ? 'running' : syncStatus?.outcome || 'idle')}>
      <div className="release-sync-main"><div className="release-sync-icon"><RefreshCw size={20} /></div><div><p className="eyebrow">{t('上游同步')}</p><h3>{syncReady ? syncOutcome : t(syncStatus?.readiness === 'unconfigured' ? '当前上游未配置完整' : '上游同步已关闭')}</h3><p>{syncReady && syncStatus?.source ? <><code>{syncStatus.source.name}</code><span>{' · '}{syncStatus.source.provider}{syncStatus.source.target ? ` · ${syncStatus.source.target}` : ''}</span></> : t('配置一个 GitHub 或 Coworker 上游后，发布页才显示完整同步控制。')} <a href={settingsHref}>{t('配置上游')}</a></p></div></div>
      {syncReady && <div className="release-sync-facts">
        <span><b>{syncStatus?.version ? 'v' + syncStatus.version : '—'}</b>{t('当前候选')}</span>
        <span><b>{syncStatus?.next_run_at ? formatDateTime(syncStatus.next_run_at) : '—'}</b>{t('下次检测')}</span>
        {syncStatus?.rate_limit?.remaining != null && <span><b>{syncStatus.rate_limit.remaining}</b>{t('剩余请求额度')}</span>}
      </div>}
      {syncReady && syncRunning && <div className="release-sync-progress"><div><span>{syncStatus?.asset || t('正在读取 Release…')}</span><b>{syncStatus?.bytes_total ? `${formatBytes(syncStatus.bytes_downloaded || 0)} / ${formatBytes(syncStatus.bytes_total)}` : t(syncStatus?.phase || '正在检测')}</b></div><i><em style={{ width: `${syncProgress}%` }} /></i></div>}
      {!syncRunning && syncStatus?.last_error && <div className="notice error"><TriangleAlert size={15} /><span>{syncStatus.last_error}</span></div>}
      {(syncError || syncMessage || sync.error) && <div className={'notice ' + (syncError || sync.error ? 'error' : 'success')}>{syncError || sync.error ? <TriangleAlert size={15} /> : <Check size={15} />}<span>{syncError || sync.error || syncMessage}</span></div>}
      <div className="release-sync-footer"><p><ShieldCheck size={15} />{t('同步只导入本地草稿，不会修改 latest.json；请检查制品和签名后手动发布。')}</p><div><button className="ghost" onClick={() => void sync.reload()} disabled={sync.loading}><RefreshCw size={14} />{t('刷新状态')}</button>{syncReady && <button className="primary" onClick={() => void triggerSync()} disabled={syncBusy || syncRunning}><CloudUpload size={14} />{syncRunning ? t('同步中…') : t('立即同步')}</button>}</div></div>
    </section>
    <div className="release-layout">
      <aside className="release-index">
        <header><div><span>{t('版本记录')}</span><b>{t('{{count}} 个版本', { count: releaseItems.length })}</b></div><button className="icon-btn" title={t('刷新版本')} aria-label={t('刷新版本')} onClick={() => { void releases.reload(); void versionStatistics.reload(); }}><RefreshCw size={14} /></button></header>
        <button className={'release-new ' + (creating ? 'active' : '')} onClick={() => { setCreating(true); setSelectedVersion(''); setDetail(null); setQueued([]); setActionError(''); setMessage(''); }}><Plus size={15} /><span><b>{t('新建版本')}</b><small>{t('准备下一次桌面更新')}</small></span></button>
        {releases.loading ? <Loading /> : releases.error ? <Loading error={releases.error} /> : <div className="release-trace">{releaseItems.length ? releaseItems.map(item => { const state = item.version === latest ? 'latest' : item.published ? 'published' : 'draft'; return <button key={item.version} className={(selectedVersion === item.version ? 'active ' : '') + state} onClick={() => void openRelease(item.version)}><span className="trace-node"><i /></span><span className="trace-copy"><span><b>v{item.version}</b><em>{stateLabel(item.version, item.published)}</em></span><small>{item.notes || t('没有发布说明')}</small><span className="trace-platforms">{t('{{updater}} 个自动更新包 · {{installer}} 个安装包', { updater: item.platforms.length, installer: item.installers.length })}</span><time>{item.updated_at ? formatDate(item.updated_at) : '—'}</time></span></button>; }) : <div className="release-index-empty">{t('创建第一个桌面版本')}</div>}</div>}
      </aside>
      <main className="release-workspace">
        {creating ? <Panel title="创建桌面版本" note="只建立版本草稿；创建后在同一工作台上传 updater、installer 与签名。" className="release-create"><form onSubmit={createRelease}><Field label="版本号" hint="遵循 SemVer，例如 0.3.0"><input autoFocus value={newVersion} onChange={event => setNewVersion(event.target.value)} placeholder="0.3.0" /></Field><Field label="发布说明" hint="会显示给检查到更新的桌面客户端"><textarea value={newNotes} onChange={event => setNewNotes(event.target.value)} placeholder={t('这次更新解决了什么？')} /></Field><div className="panel-actions"><button className="primary" disabled={!newVersion.trim() || creatingBusy}>{creatingBusy ? t('正在创建…') : t('创建版本草稿')}<ChevronRight size={15} /></button></div></form>{actionError && <div className="notice error"><TriangleAlert size={16} /><span>{actionError}</span></div>}{message && <div className="notice success"><Check size={16} /><span>{message}</span></div>}</Panel>
        : detailLoading ? <Loading /> : detailError ? <Loading error={detailError} /> : detail ? <>
          <Panel title={'v' + detail.version} note={detail.notes ? <ReleaseNotes text={detail.notes} /> : t('这个版本没有发布说明。')} action={<span className={'release-state ' + (detail.version === latest ? 'latest' : detail.published ? 'published' : 'draft')}>{stateLabel(detail.version, detail.published)}</span>} className="release-detail">
            <div className="release-meta"><span><b>{detail.created_at ? formatDateTime(detail.created_at) : '—'}</b>{t('创建时间')}</span><span><b>{detail.updated_at ? formatDateTime(detail.updated_at) : '—'}</b>{t('最近更新')}</span><span><b>{Object.keys(detail.platforms || {}).length + Object.keys(detail.installers || {}).length}</b>{t('已存产物')}</span></div>
            {detail.source && <div className="release-provenance"><span>{t('同步来源')}</span><strong>{t(releaseSourceLabel(detail.source))}</strong><code>{detail.source.repository || detail.source.base_url || detail.source.revision || t('上游来源')}{detail.source.tag ? ` · ${detail.source.tag}` : ''}</code></div>}
            <div className="release-asset-grid"><ReleaseAssetLane version={detail.version} title="自动更新包" note="带签名，进入 latest.json" assets={detail.platforms} /><ReleaseAssetLane version={detail.version} title="安装包" note="供首次安装或手动重装" assets={detail.installers} /></div>
          </Panel>
          <Panel title="追加或替换产物" note="拖入 CI/发布产物 ZIP 或散文件；识别结果可在上传前修正。" className="release-upload-panel">
            <label className={'release-dropzone ' + (dragging ? 'dragging ' : '') + (parsing ? 'busy' : '')} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void addFiles(Array.from(event.dataTransfer.files)); }}><CloudUpload size={25} /><span><b>{parsing ? t('正在读取产物…') : t('拖入产物 ZIP 或点击选择文件')}</b><small>{t('自动匹配版本、平台、产物类型和同名 .sig')}</small></span><input type="file" multiple accept=".zip,.sig,.exe,.dmg,.appimage,.deb,.rpm,.msi,.gz" disabled={parsing} onChange={event => { void addFiles(Array.from(event.target.files || [])); event.target.value = ''; }} /></label>
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

const TRAFFIC_DIRECTIONS: Record<string, string> = {
  inbound: '入站',
  outbound: '出站',
};
const TRAFFIC_STATUSES: Record<string, string> = {
  received: '已接收',
  sent: '已发送',
  denied: '已拒绝',
  failed: '失败',
  duplicate: '重复忽略',
};
const TRAFFIC_SOURCES: Record<string, string> = {
  access_policy: '访问策略',
  agent: 'Agent',
  desktop: 'Desktop',
  rest: 'REST',
  websocket: 'WebSocket',
  wecom: 'WeCom',
  weixin: '微信 Claw',
  telegram: 'Telegram',
};
const TRAFFIC_REASONS: Record<string, string> = {
  policy: '策略拒绝',
  rejection_notice: '拒绝通知',
  message_id: '消息 ID 重复',
  no_channel: '无匹配信道',
  participant_validation: '通信地址校验',
  delivery: '投递失败',
};

function ChannelTrafficPanel({ data, error }: { data: Json | null; error: string }) {
  const [query, setQuery] = useState('');
  const [direction, setDirection] = useState('');
  const [status, setStatus] = useState('');
  const entries = data?.entries || [];
  const filtered = entries.filter((entry: Json) => {
    if (direction && entry.direction !== direction) return false;
    if (status && entry.status !== status) return false;
    return !query || `${entry.channel} ${entry.participant_id} ${entry.source} ${entry.reason}`.toLowerCase().includes(query.toLowerCase());
  });
  return <Panel title="消息流量" note="记录所有信道的收发结果与策略拒绝；只保存元数据，不保存消息正文、附件内容或凭据。" className="channel-traffic-panel">
    <div className="traffic-filters"><label><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('搜索信道、participant 或来源')} /></label><select value={direction} onChange={event => setDirection(event.target.value)}><option value="">{t('全部方向')}</option><option value="inbound">{t('入站')}</option><option value="outbound">{t('出站')}</option></select><select value={status} onChange={event => setStatus(event.target.value)}><option value="">{t('全部状态')}</option>{Object.entries(TRAFFIC_STATUSES).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select><span>{t('{{count}} 条记录', { count: filtered.length })}</span></div>
    {!data ? <Loading error={error} /> : filtered.length ? <div className="traffic-table"><div className="traffic-row traffic-head"><span>{t('时间')}</span><span>{t('方向')}</span><span>{t('信道')}</span><span>participant_id</span><span>{t('结果')}</span><span>{t('来源 / 原因')}</span></div>{filtered.map((entry: Json, index: number) => <article className={`traffic-row ${entry.status || 'unknown'}`} key={`${entry.ts}-${index}`}><time title={formatDateTime(entry.ts)}><b>{formatTime(entry.ts, [], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</b><small>{formatDate(entry.ts)}</small></time><span className={`traffic-direction ${entry.direction || ''}`}>{t(TRAFFIC_DIRECTIONS[entry.direction] || entry.direction || '未知')}</span><code>{entry.channel || '—'}</code><code title={entry.participant_id}>{entry.participant_id || '—'}</code><b className="traffic-result">{t(TRAFFIC_STATUSES[entry.status] || entry.status || '未知')}</b><small className="traffic-source">{t(TRAFFIC_SOURCES[entry.source] || entry.source || '未知')}{entry.reason ? ` · ${t(TRAFFIC_REASONS[entry.reason] || entry.reason)}` : ''}</small></article>)}</div> : <Empty text="没有符合当前筛选条件的消息流量记录。" />}
  </Panel>;
}

function Audit() {
  const audit = useLoad(() => api<Json>('/api/admin/audit?limit=300'), []);
  const diagnostics = useLoad(() => api<Json>('/api/admin/diagnostics/tasks'), []);
  const traffic = useLoad(() => api<Json>('/api/admin/channel-traffic?limit=500'), []);
  const [tab, setTab] = useState<'audit' | 'runtime' | 'traffic'>('audit');
  const [query, setQuery] = useState('');
  const [result, setResult] = useState('');
  useEffect(() => {
    if (tab !== 'traffic') return;
    const timer = window.setInterval(() => void traffic.reload(), 5_000);
    return () => window.clearInterval(timer);
  }, [tab, traffic.reload]);
  const entries = audit.data?.entries || [];
  const today = localDateKey();
  const todayCount = entries.filter((entry: Json) => localDateKey(entry.ts) === today).length;
  const failed = entries.filter((entry: Json) => entry.result !== 'ok').length;
  const sources = new Set(entries.map((entry: Json) => entry.source).filter(Boolean)).size;
  const filtered = entries.filter((entry: Json) => {
    const matchesResult = !result || (result === 'ok' ? entry.result === 'ok' : entry.result !== 'ok');
    return matchesResult && (!query || JSON.stringify(entry).toLowerCase().includes(query.toLowerCase()));
  });
  const refresh = () => tab === 'audit' ? audit.reload() : tab === 'runtime' ? diagnostics.reload() : traffic.reload();
  return <div className="audit-workspace">
    <section className="audit-vitals">
      <article><ShieldCheck size={17} /><span>{t('今日操作')}</span><b>{todayCount}</b><small>{t('最近保留 {{count}} 条', { count: entries.length })}</small></article>
      <article className={failed ? 'alert' : ''}><TriangleAlert size={17} /><span>{t('异常结果')}</span><b>{failed}</b><small>{failed ? t('需要检查失败记录') : t('没有操作失败')}</small></article>
      <article><TerminalSquare size={17} /><span>{t('活跃任务')}</span><b>{diagnostics.data?.pending ?? '—'}</b><small>{t('事件循环中的等待任务')}</small></article>
      <article><Fingerprint size={17} /><span>{t('操作来源')}</span><b>{sources}</b><small>{t('不同客户端地址')}</small></article>
    </section>
    <div className="audit-switcher"><div><button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}><ShieldCheck size={15} />{t('操作时间线')}</button><button className={tab === 'traffic' ? 'active' : ''} onClick={() => setTab('traffic')}><MessagesSquare size={15} />{t('消息流量')}</button><button className={tab === 'runtime' ? 'active' : ''} onClick={() => setTab('runtime')}><Activity size={15} />{t('运行诊断')}</button></div><button className="icon-btn" title={t('刷新当前视图')} aria-label={t('刷新当前视图')} onClick={() => void refresh()}><RefreshCw size={15} /></button></div>
    {tab === 'audit' ? <Panel title="管理员操作时间线" note="只记录操作元数据，不包含令牌、密钥和完整正文。" className="audit-panel">
      <div className="audit-filters"><label><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('搜索操作、目标或来源')} /></label><select value={result} onChange={event => setResult(event.target.value)}><option value="">{t('全部结果')}</option><option value="ok">{t('仅成功')}</option><option value="failed">{t('仅异常')}</option></select><span>{t('{{count}} 条记录', { count: filtered.length })}</span></div>
      {!audit.data ? <Loading error={audit.error} /> : filtered.length ? <div className="audit-timeline">{filtered.map((entry: Json, index: number) => {
        const actionParts = String(entry.action || 'unknown').split('.');
        const area = actionParts.shift() || 'system';
        const action = actionParts.join(' · ') || entry.action;
        return <article key={entry.ts + '-' + index} className={entry.result === 'ok' ? 'ok' : 'failed'}><div className="audit-rail"><i /></div><time><b>{formatTime(entry.ts, [], { hour: '2-digit', minute: '2-digit' })}</b><span>{formatDate(entry.ts)}</span></time><div className="audit-event"><header><span>{area}</span><b>{action}</b><i>{entry.result === 'ok' ? t('成功') : entry.result}</i></header><code>{entry.target || '—'}</code>{entry.detail && <p>{entry.detail}</p>}<footer>{t('来源')} {entry.source || 'unknown'}</footer></div></article>;
      })}</div> : <Empty text="没有符合当前筛选条件的审计记录。" />}
    </Panel> : tab === 'runtime' ? <Panel title="事件循环诊断" note="pending 通常表示任务正在等待消息或定时器，并不等同于故障。" className="runtime-diagnostics">
      {!diagnostics.data ? <Loading error={diagnostics.error} /> : <><div className="runtime-callout"><Activity size={20} /><div><b>{t('{{count}} 个任务正在等待', { count: diagnostics.data.pending })}</b><span>{t('共采样 {{count}} 个 asyncio task；展开条目查看完整快照。', { count: diagnostics.data.total })}</span></div></div><div className="runtime-task-grid">{diagnostics.data.tasks.map((task: Json, index: number) => <details key={(task.name || 'task') + '-' + index} className={task.current ? 'current' : task.done ? 'done' : ''}><summary><span className="task-signal"><i /></span><div><b>{task.name || 'task-' + index}</b><code>{task.coro || 'unknown coroutine'}</code></div><span className="task-state">{task.current ? t('当前请求') : task.done ? t('已完成') : t('等待中')}</span></summary><div className="task-waiting"><span>{t('等待位置')}</span><code>{task.waiting_at || t('没有 Python 栈，可能尚未开始或正在等待底层 I/O')}</code><pre>{JSON.stringify(task, null, 2)}</pre></div></details>)}</div></>}
    </Panel> : <ChannelTrafficPanel data={traffic.data} error={traffic.error} />}
  </div>;
}

function Empty({ text }: { text: string }) { return <div className="empty"><Wrench size={23} /><p>{t(text)}</p></div>; }

function RelayAccess() {
  const relay = useLoad<Json>(() => api('/api/admin/relay'), []);
  const [relayUrl, setRelayUrl] = useState('');
  const [pairingCode, setPairingCode] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [communicationToken, setCommunicationToken] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => void relay.reload(), 10_000);
    return () => window.clearInterval(timer);
  }, [relay.reload]);
  useEffect(() => {
    if (relay.data && !relay.data.instance_id) {
      setShowToken(false);
      setCommunicationToken('');
    }
  }, [relay.data]);
  const action = async (name: string, operation: () => Promise<unknown>, success: string, afterSuccess?: () => void) => {
    setBusy(name); setNotice(null);
    try {
      await operation();
      afterSuccess?.();
      setNotice({ kind: 'success', text: t(success) });
      await relay.reload();
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : t('操作失败') });
    } finally {
      setBusy('');
    }
  };
  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setNotice({ kind: 'success', text: t('{{label}}已复制', { label: t(label) }) });
    } catch {
      setNotice({ kind: 'error', text: t('无法访问剪贴板，请手动复制。') });
    }
  };
  const loadToken = async () => {
    const result = await api<Json>('/api/admin/relay/token');
    const token = String(result.communication_token || '');
    setCommunicationToken(token);
    return token;
  };
  const copyDesktopConfig = async () => {
    setBusy('copy-config');
    try {
      const token = communicationToken || await loadToken();
      await copy(`Base URL: ${String(relay.data?.public_base_url || '')}\nBearer Token: ${token}`, 'Desktop 配置');
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : t('操作失败') });
    } finally {
      setBusy('');
    }
  };
  if (relay.loading && !relay.data) return <Loading error={relay.error} />;
  const data = relay.data || {};
  const configured = Boolean(data.instance_id);
  const connected = data.status === 'connected';
  const connecting = data.status === 'connecting';
  const tokenCompatible = data.communication_token_compatible !== false;
  const statusTitle = connected ? '已安全连接' : connecting ? '正在建立安全连接' : configured ? '连接已中断' : '尚未配对';
  const statusBadge = connected ? '已连接' : connecting ? '连接中' : configured ? '已断开' : '未配置';
  const statusDetail = connected
    ? '新版 Desktop 可以使用下方配置连接这台 Coworker。'
    : connecting
      ? '正在认证 Relay 并建立加密出站隧道。'
      : configured
        ? '内置 Relay Client 会自动重连；你也可以查看错误或手动重连。'
        : '准备 Relay 地址和 10 分钟内有效的一次性配对码即可开始。';
  return <div className="page-stack relay-access">
    <Panel title="远程访问" note="通过自托管中继（Relay）让新版 Desktop 从公网安全连接这台 Coworker。"
      action={configured ? <button className="ghost mini" disabled={relay.loading || Boolean(busy)} onClick={() => void relay.reload()}><RefreshCw size={13} />{t(relay.loading ? '正在刷新…' : '刷新状态')}</button> : undefined}>
      <section className={`relay-hero ${connected ? 'connected' : configured ? 'waiting' : 'idle'}`}>
        <div className="relay-signal"><CloudUpload size={26} /><i /></div>
        <div><span>{t('远程访问状态')}</span><h3>{t(statusTitle)}</h3>
          <p>{t(statusDetail)}</p>
        </div>
        <b className={`relay-status-badge ${connected ? 'ok' : connecting ? 'pending' : configured ? 'error' : ''}`}>{t(statusBadge)}</b>
      </section>
      {!configured ? <>
        <ol className="relay-steps" aria-label={t('连接步骤')}>
          <li><span>1</span><div><b>{t('部署 Relay')}</b><small>{t('在公网主机初始化并启动 coworker-relay。')}</small></div></li>
          <li><span>2</span><div><b>{t('创建配对码')}</b><small><code>coworker-relay instance create --name home</code></small></div></li>
          <li><span>3</span><div><b>{t('连接 Coworker')}</b><small>{t('在下方粘贴 Relay 地址和一次性配对码。')}</small></div></li>
        </ol>
        <form className="relay-enroll" onSubmit={event => {
        event.preventDefault();
        void action('connect', () => api('/api/admin/relay/connect', {
          method: 'POST',
          body: JSON.stringify({ relay_url: relayUrl.trim().replace(/\/+$/, ''), pairing_code: pairingCode.trim() }),
        }), 'Relay 配对成功', () => {
          setRelayUrl('');
          setPairingCode('');
        });
      }}>
        <header><div><h3>{t('连接此 Coworker')}</h3><p>{t('配对只需完成一次；之后 Coworker 会自动维护出站连接。')}</p></div><ShieldCheck size={22} /></header>
        <label><span>{t('Relay 地址')}</span><input type="url" required inputMode="url" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="http://203.0.113.10:8443" value={relayUrl} onBlur={() => setRelayUrl(value => value.trim().replace(/\/+$/, ''))} onChange={event => setRelayUrl(event.target.value)} /><small>{t('示例：http://203.0.113.10:8443；不要填写 /i/... 实例路径。')}</small></label>
        <label><span>{t('一次性配对码')}</span><input required autoComplete="one-time-code" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="pair_…" value={pairingCode} onChange={event => setPairingCode(event.target.value)} /><small>{t('在 Relay 主机运行 instance create 后获得，10 分钟内仅可使用一次。')}</small></label>
        {data.communication_token_compatible === false && <div className="notice error" role="alert">
          <TriangleAlert size={18} /><span>{t('当前通信 Token 不符合 Relay 的高熵格式。请先轮换；现有直连 Desktop 随后也必须更新 Token。')}</span>
          <button type="button" className="ghost" disabled={Boolean(busy)} onClick={() => {
            if (window.confirm(t('轮换通信 Token？现有 Desktop 必须改用新 Token 后才能重新连接。'))) {
              void action('rotate-token', () => api('/api/admin/relay/rotate-token', { method: 'POST' }), '通信 Token 已轮换，请更新 Desktop 配置');
            }
          }}>{t('轮换为 Relay 通信 Token')}</button>
        </div>}
        <footer><button className="primary" disabled={Boolean(busy) || !tokenCompatible || !relayUrl.trim() || !pairingCode.trim()}><CloudUpload size={15} />{t(busy === 'connect' ? '正在配对并连接…' : '配对并连接')}</button><small>{t('不会开放 Coworker 的内网端口。')}</small></footer>
      </form>
      </> : <>
        <section className="relay-desktop-config">
          <header><div><span>{t('下一步')}</span><h3>{t('在新版 Desktop 中连接')}</h3><p>{t('只需填写 Base URL 和 Token；Desktop 会自动识别 Relay 并强制使用端到端加密。')}</p></div><button className="primary" disabled={Boolean(busy) || !data.public_base_url} onClick={() => void copyDesktopConfig()}><KeyRound size={15} />{t(busy === 'copy-config' ? '正在准备配置…' : '复制完整配置')}</button></header>
          <div className="relay-credentials">
            <article><span>{t('Base URL')}</span><div><code>{String(data.public_base_url || '')}</code><button className="ghost mini" disabled={!data.public_base_url} onClick={() => void copy(String(data.public_base_url || ''), 'Base URL')}>{t('复制到剪贴板')}</button></div></article>
            <article><span>{t('Bearer Token')}</span><div><code className={showToken ? '' : 'masked'}>{showToken ? communicationToken : '••••••••••••••••••••••••'}</code><span className="relay-inline-actions"><button className="ghost mini" disabled={busy === 'token'} onClick={() => {
            if (showToken) { setShowToken(false); setCommunicationToken(''); return; }
            setBusy('token');
            void loadToken().then(() => setShowToken(true)).catch(error => setNotice({ kind: 'error', text: error instanceof Error ? error.message : t('操作失败') })).finally(() => setBusy(''));
          }}>{t(showToken ? '隐藏' : '显示')}</button><button className="ghost mini" disabled={busy === 'token'} onClick={() => {
            setBusy('token');
            void (communicationToken ? Promise.resolve(communicationToken) : loadToken()).then(token => copy(token, 'Bearer Token')).catch(error => setNotice({ kind: 'error', text: error instanceof Error ? error.message : t('操作失败') })).finally(() => setBusy(''));
          }}>{t('复制到剪贴板')}</button></span></div></article>
          </div>
          <small className="relay-secret-note"><ShieldCheck size={13} />{t('完整配置包含通信 Token，请只粘贴到你信任的 Desktop。')}</small>
        </section>
        {data.last_error && <div className="notice error" role="alert">{String(data.last_error)}</div>}
        {data.auth_key_synced === false && <div className="notice error" role="alert"><TriangleAlert size={18} />{t('入口认证公钥尚未同步；Relay 不会接受新的 Desktop 连接。请先尝试重新连接。')}</div>}
      </>}
      {notice && <div className={`notice ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.text}</div>}
      {relay.error && <div className="notice error" role="alert"><span>{relay.error}</span><button className="ghost mini" onClick={() => void relay.reload()}>{t('重试')}</button></div>}
    </Panel>
    {configured && <Panel title="连接健康与维护" note="日常无需操作；连接中断时 Coworker 会自动重试。">
      <div className="relay-health-grid">
        <article><span>{t('Relay 地址')}</span><code>{String(data.relay_url || '')}</code></article>
        <article><span>{t('实例 ID')}</span><code>{String(data.instance_id || '')}</code></article>
        <article><span>{t('端到端协议')}</span><b>{data.e2ee ? `E2EE v${String(data.protocol_version || 1)}` : t('未启用')}</b><small>{t('认证 epoch {{epoch}}', { epoch: Number(data.auth_epoch || 0) })}</small></article>
        <article><span>{t('活动 Desktop 会话')}</span><b>{Number(data.active_sessions || 0)}</b></article>
        <article><span>{t('最后心跳')}</span><b>{data.last_heartbeat ? formatDateTime(String(data.last_heartbeat)) : t('尚无')}</b></article>
        <article><span>{t('隧道延迟')}</span><b>{data.latency_ms == null ? '—' : `${Math.round(Number(data.latency_ms))} ms`}</b></article>
      </div>
      <div className="relay-maintenance-actions">
        <button className="ghost" disabled={Boolean(busy) || !connected} onClick={() => void action('test', () => api('/api/admin/relay/test', { method: 'POST' }), 'Relay 端到端连接测试成功')}><ShieldCheck size={15} />{t(busy === 'test' ? '正在测试…' : '测试端到端连接')}</button>
        <button className="ghost" disabled={Boolean(busy)} onClick={() => void action('reconnect', () => api('/api/admin/relay/reconnect', { method: 'POST' }), '已请求重新连接')}><RefreshCw size={15} />{t(busy === 'reconnect' ? '正在重新连接…' : '重新连接')}</button>
      </div>
      <details className="relay-sensitive-actions">
        <summary><TriangleAlert size={15} /><span><b>{t('凭据与连接管理')}</b><small>{t('这些操作会让现有 Desktop 断开或需要重新配置。')}</small></span></summary>
        <div>
          <button className="danger-outline" disabled={Boolean(busy)} onClick={() => {
            if (window.confirm(t('轮换通信 Token？现有 Desktop 必须改用新 Token 后才能重新连接。'))) {
              void action('rotate-token', () => api('/api/admin/relay/rotate-token', { method: 'POST' }), '通信 Token 已轮换，请更新 Desktop 配置');
            }
          }}>{t(busy === 'rotate-token' ? '正在轮换…' : '轮换通信 Token')}</button>
          <button className="danger-solid" disabled={Boolean(busy)} onClick={() => {
            if (window.confirm(t('断开 Relay 并删除本地实例密钥？Relay 上的实例仍需使用 coworker-relay 撤销。'))) {
              void action('disconnect', () => api('/api/admin/relay', { method: 'DELETE' }), 'Relay 已断开');
            }
          }}>{t(busy === 'disconnect' ? '正在断开…' : '断开并删除本地凭据')}</button>
        </div>
      </details>
    </Panel>}
  </div>;
}

export default function AdminApp() {
  const { language } = useAdminI18n();
  const [ready, setReady] = useState(false);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [bootstrap, setBootstrap] = useState<Json | null>(null);
  const [identity, setIdentity] = useState<AdminIdentity>({ name: '', confirmation_name: '' });
  const [section, setSection] = useState<Section>(sectionFromLocation);
  const [lifeState, setLifeState] = useState<LifeState>('quiet');
  const dirtyOwners = useRef(new Set<string>());
  const sectionRef = useRef(section);
  const lastSectionByWorkspace = useRef<Record<Workspace, Section>>({ ...DEFAULT_SECTION_BY_WORKSPACE });
  const acceptedLocation = useRef(`${window.location.pathname}${window.location.search}${window.location.hash}`);
  const reportNavigationDirty = useCallback((owner: string, dirty: boolean) => {
    if (dirty) dirtyOwners.current.add(owner);
    else dirtyOwners.current.delete(owner);
  }, []);
  const confirmNavigation = useCallback(() => (
    dirtyOwners.current.size === 0 || window.confirm(t('当前页面有未保存修改，确定离开？'))
  ), []);
  useEffect(() => {
    if (!storedToken()) { setSessionChecked(true); return; }
    api<AdminIdentity>('/api/admin/session/verify', { method: 'POST' })
      .then(result => { setIdentity(result); setReady(true); })
      .catch(() => sessionStorage.removeItem('coworker-admin-token'))
      .finally(() => setSessionChecked(true));
  }, []);
  useEffect(() => {
    if (!ready) return;
    api<Json>('/api/admin/bootstrap').then(result => {
      setServerTimezone(result.server_timezone);
      setBootstrap(result);
    }).catch(() => setBootstrap({ required: false }));
  }, [ready]);
  useEffect(() => {
    const syncSection = () => {
      const next = sectionFromLocation();
      if (next !== sectionRef.current && !confirmNavigation()) {
        window.history.pushState({}, '', acceptedLocation.current);
        return;
      }
      sectionRef.current = next;
      acceptedLocation.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      setSection(next);
    };
    window.addEventListener('popstate', syncSection);
    return () => window.removeEventListener('popstate', syncSection);
  }, [confirmNavigation]);
  useEffect(() => {
    const warnAboutUnsavedChanges = (event: BeforeUnloadEvent) => {
      if (dirtyOwners.current.size > 0) event.preventDefault();
    };
    window.addEventListener('beforeunload', warnAboutUnsavedChanges);
    return () => window.removeEventListener('beforeunload', warnAboutUnsavedChanges);
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
  const currentWorkspace = useMemo(() => workspaceForSection(section), [section]);
  useEffect(() => {
    lastSectionByWorkspace.current[currentWorkspace.id] = section;
  }, [currentWorkspace.id, section]);
  const navigate = useCallback((next: Section) => {
    if (next === sectionRef.current || !confirmNavigation()) return;
    const href = sectionHref(next);
    window.history.pushState({}, '', href);
    sectionRef.current = next;
    lastSectionByWorkspace.current[workspaceForSection(next).id] = next;
    acceptedLocation.current = href;
    setSection(next);
    window.scrollTo(0, 0);
  }, [confirmNavigation]);
  const openRuntimeLogs = useCallback((startTime = '', endTime = '', eventType = '') => {
    if (!confirmNavigation()) return;
    const url = new URL(window.location.href);
    url.searchParams.set('section', 'runtime');
    url.searchParams.set('runtime_tab', 'logs');
    if (startTime && endTime) {
      url.searchParams.set('log_start', toAbsoluteIso(startTime));
      url.searchParams.set('log_end', toAbsoluteIso(endTime));
    } else {
      url.searchParams.delete('log_start');
      url.searchParams.delete('log_end');
    }
    if (eventType) url.searchParams.set('log_type', eventType);
    else url.searchParams.delete('log_type');
    ['log_seq', 'log_q', 'log_seq_start', 'log_seq_end', 'log_cursor'].forEach(key => url.searchParams.delete(key));
    url.searchParams.delete('group');
    url.searchParams.delete('source');
    const href = `${url.pathname}${url.search}${url.hash}`;
    window.history.pushState({}, '', href);
    sectionRef.current = 'runtime';
    lastSectionByWorkspace.current.operations = 'runtime';
    acceptedLocation.current = href;
    setSection('runtime');
    window.scrollTo(0, 0);
  }, [confirmNavigation]);
  const onSectionLinkClick = useCallback((event: ReactMouseEvent<HTMLAnchorElement>, next: Section) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(next);
  }, [navigate]);
  const name = identity.name || '';
  const confirmationName = identity.confirmation_name || '';
  const lifeLabel = t(lifeState === 'live' ? '生命信号在线' : lifeState === 'resting' ? '安静休息中' : '等待生命信号');
  if (!sessionChecked) return <><AdminLanguageSwitch className="admin-language-toggle-floating" /><main className="admin-login"><div className="state-box"><span className="state-pulse"><i /><i /><i /></span><span>{t('正在确认本地值守状态…')}</span></div></main></>;
  if (!ready) return <Login onReady={result => { setIdentity(result); setReady(true); }} />;
  if (!bootstrap) return <><AdminLanguageSwitch className="admin-language-toggle-floating" /><main className="admin-login"><div className="state-box"><span className="state-pulse"><i /><i /><i /></span><span>{t('正在读取初始化状态…')}</span></div></main></>;
  if (bootstrap.required) return <FirstRun data={bootstrap} onComplete={() => { setBootstrap({ required: false }); location.reload(); }} />;
  return <NavigationGuardContext.Provider value={reportNavigationDirty}><main className={`admin-shell life-${lifeState}`} data-language={language}>
    <aside className="admin-sidebar">
      <a className="admin-brand" href="/">
        <div className="brand-mark"><Orbit size={22} /><i /></div>
        <div><b>{name || 'Coworker'}</b><span>{t('生命值守台')}</span></div>
      </a>
      <nav className="workspace-nav" aria-label={t('照看室导航')}>
        {WORKSPACES.map(workspace => {
          const WorkspaceIcon = workspace.icon;
          const active = workspace.id === currentWorkspace.id;
          return <div className={`workspace-group${active ? ' active' : ''}`} role="group" aria-label={t(workspace.label)} key={workspace.id}>
            <div className="workspace-group-label" title={t(workspace.description)}>
              <WorkspaceIcon size={13} />
              <span>{t(workspace.label)}</span>
            </div>
            <div className="workspace-section-list">
              {workspace.sections.map(sectionId => {
                const item = NAV.find(candidate => candidate.id === sectionId)!;
                const ItemIcon = item.icon;
                const selected = section === item.id;
                return <a className={`workspace-section-link${selected ? ' active' : ''}`} href={sectionHref(item.id)} onClick={event => onSectionLinkClick(event, item.id)} aria-current={selected ? 'page' : undefined} title={t(item.description)} key={item.id}>
                  <ItemIcon size={15} />
                  <span>{t(item.label)}</span>
                </a>;
              })}
            </div>
          </div>;
        })}
      </nav>
      <nav className="mobile-workspace-nav" aria-label={t('照看室导航')}>
        {WORKSPACES.map(workspace => {
          const active = workspace.id === currentWorkspace.id;
          const target = active ? section : lastSectionByWorkspace.current[workspace.id];
          const WorkspaceIcon = workspace.icon;
          return <a className={`mobile-workspace-link${active ? ' active' : ''}`} href={sectionHref(target)} onClick={event => onSectionLinkClick(event, target)} title={t(workspace.description)} aria-label={t(workspace.label)} aria-current={active ? 'page' : undefined} key={workspace.id}>
            <WorkspaceIcon size={18} />
            <span>{t(workspace.mobileLabel)}</span>
          </a>;
        })}
      </nav>
      <div className="sidebar-foot">
        <span className="sidebar-presence"><i />{lifeLabel}</span>
        <button type="button" onClick={() => { if (!confirmNavigation()) return; dirtyOwners.current.clear(); sessionStorage.removeItem('coworker-admin-token'); location.reload(); }}><LogOut size={16} /><span>{t('退出本次值守')}</span></button>
      </div>
    </aside>
    <section className="admin-main">
      <header className="admin-topbar">
        <div className="topbar-title"><p className="eyebrow">{t(currentWorkspace.label)}</p><h1>{t(current.label)}</h1><span>{t(current.description)}</span><label className="workspace-picker"><span className="sr-only">{t('切换管理页面')}</span><select aria-label={t('切换管理页面')} value={section} onChange={event => navigate(event.target.value as Section)}>{WORKSPACES.map(workspace => <optgroup label={t(workspace.label)} key={workspace.id}>{workspace.sections.map(sectionId => <option value={sectionId} key={sectionId}>{t(NAV.find(item => item.id === sectionId)?.label || sectionId)}</option>)}</optgroup>)}</select></label></div>
        <div className="topbar-actions">
          <AdminLanguageSwitch />
          <div className="shell-life" aria-label={lifeLabel}><span><i />{lifeLabel}</span></div>
          <a href="/">{t('查看生命体主页')} <ChevronRight size={14} /></a>
        </div>
      </header>
      <div className="admin-content">
        {section === 'overview' && <Overview name={name} onNavigate={onSectionLinkClick} />}
        {section === 'usage' && <UsageAnalyticsPage onOpenLogs={openRuntimeLogs} pricingHref={modelPricingHref()} />}
        {section === 'models' && <Models />}
        {section === 'settings' && <Settings />}
        {section === 'memory' && <MemoryCenter coworkerName={name} confirmationName={confirmationName} />}
        {section === 'runtime' && <Runtime confirmationName={confirmationName} />}
        {section === 'identity' && <Identity onIdentity={setIdentity} />}
        {section === 'people' && <PeopleView />}
        {section === 'content' && <ContentManager />}
        {section === 'relay' && <RelayAccess />}
        {section === 'releases' && <DesktopReleases />}
        {section === 'audit' && <Audit />}
      </div>
    </section>
  </main></NavigationGuardContext.Provider>;
}
