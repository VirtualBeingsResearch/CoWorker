import {
  ChevronDown,
  Globe,
  ListOrdered,
  PlugZap,
  SlidersHorizontal,
  TriangleAlert,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { t } from '../../../i18n/admin';
import { CONFIG_LABELS } from '../configLabels';
import { configFieldPresentation } from '../configFieldPresentation';
import { SEARCH_BACKENDS, SearchBackendOrderEditor, searchBackendOrder } from '../searchBackends';
import type { Json, SettingsPanelProps } from '../types';

type ProviderDetails = {
  summary: string;
  fields: string[];
  keyless?: boolean;
};

type Provider = {
  id: string;
  label: string;
  summary: string;
  fields: string[];
  keyless: boolean;
};

const PROVIDER_DETAILS: Record<string, ProviderDetails> = {
  bocha: {
    summary: '中文网页搜索，适合资讯与通用检索。',
    fields: ['bocha_api_key'],
  },
  qianfan: {
    summary: '百度千帆搜索，覆盖中文网页与资讯。',
    fields: ['qianfan_api_key', 'qianfan_api_base'],
  },
  zhipu: {
    summary: '智谱开放平台 Web Search，支持搜索引擎与摘要长度设置。',
    fields: [
      'zhipu_api_key',
      'zhipu_use_llm_key',
      'zhipu_api_base',
      'zhipu_search_engine',
      'zhipu_content_size',
    ],
  },
  zhipu_prime: {
    summary: 'GLM Coding Plan 的联网搜索 MCP，密钥与开放平台不通用。',
    fields: [
      'zhipu_prime_api_key',
      'zhipu_prime_use_llm_key',
      'zhipu_prime_api_base',
      'zhipu_prime_content_size',
      'zhipu_prime_location',
    ],
  },
  deepseek: {
    summary: 'DeepSeek 官方搜索接口，可复用模型密钥。',
    fields: ['deepseek_api_key', 'deepseek_use_llm_key', 'deepseek_api_base'],
  },
  linkai: {
    summary: 'LinkAI 聚合搜索服务。',
    fields: ['linkai_api_key', 'linkai_api_base'],
  },
  anysearch: {
    summary: '支持匿名额度，可指定区域与语言。',
    fields: ['anysearch_api_key', 'anysearch_anonymous', 'anysearch_zone', 'anysearch_language'],
  },
  serply: {
    summary: 'Serply 搜索 API。',
    fields: ['serply_api_key'],
  },
  tavily: {
    summary: '面向 AI 应用的搜索 API，可选择搜索深度。',
    fields: ['tavily_api_key', 'tavily_search_depth'],
  },
  searxng: {
    summary: '自托管元搜索，不需要密钥。',
    fields: ['searxng_url', 'searxng_language', 'searxng_categories'],
  },
  keenable: {
    summary: '支持匿名额度的搜索服务。',
    fields: ['keenable_api_key', 'keenable_anonymous'],
  },
  ddgs: {
    summary: '无需密钥的 DuckDuckGo 搜索，始终可用。',
    fields: ['ddgs_backend'],
    keyless: true,
  },
};

const PROVIDERS: Provider[] = SEARCH_BACKENDS.flatMap(backend => {
  const details = PROVIDER_DETAILS[backend.id];
  return details ? [{ ...backend, ...details, keyless: details.keyless === true }] : [];
});

const PROVIDER_BY_ID = new Map(PROVIDERS.map(provider => [provider.id, provider]));

function humanize(text: string) {
  return text.replace(/_/g, ' ').replace(/\b\w/g, match => match.toUpperCase());
}

function secretConfigured(
  secretInputs: Record<string, string>,
  secretStatus: Record<string, { configured?: boolean; last4?: string }>,
  key: string,
) {
  return Boolean(secretInputs[`web_search.${key}`])
    || secretStatus[`web_search.${key}`]?.configured === true;
}

function providerConfigured(
  providerId: string,
  value: Json,
  secretInputs: Record<string, string>,
  secretStatus: Record<string, { configured?: boolean; last4?: string }>,
) {
  switch (providerId) {
    case 'ddgs':
      return true;
    case 'bocha':
      return secretConfigured(secretInputs, secretStatus, 'bocha_api_key');
    case 'qianfan':
      return secretConfigured(secretInputs, secretStatus, 'qianfan_api_key');
    case 'zhipu':
      return secretConfigured(secretInputs, secretStatus, 'zhipu_api_key')
        || (value.zhipu_use_llm_key === true && secretStatus['llm.zhipu_api_key']?.configured === true);
    case 'zhipu_prime':
      return secretConfigured(secretInputs, secretStatus, 'zhipu_prime_api_key')
        || (value.zhipu_prime_use_llm_key === true && secretStatus['llm.zhipu_api_key']?.configured === true);
    case 'deepseek':
      return secretConfigured(secretInputs, secretStatus, 'deepseek_api_key')
        || (value.deepseek_use_llm_key === true && secretStatus['llm.deepseek_api_key']?.configured === true);
    case 'linkai':
      return secretConfigured(secretInputs, secretStatus, 'linkai_api_key');
    case 'anysearch':
      return secretConfigured(secretInputs, secretStatus, 'anysearch_api_key')
        || value.anysearch_anonymous === true;
    case 'serply':
      return secretConfigured(secretInputs, secretStatus, 'serply_api_key');
    case 'tavily':
      return secretConfigured(secretInputs, secretStatus, 'tavily_api_key');
    case 'searxng':
      return Boolean(String(value.searxng_url || '').trim());
    case 'keenable':
      return secretConfigured(secretInputs, secretStatus, 'keenable_api_key')
        || value.keenable_anonymous === true;
    default:
      return false;
  }
}

function WebSearchField({
  fieldKey,
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: {
  fieldKey: string;
  value: Json;
  change: (key: string, next: unknown) => void;
  secretInputs: Record<string, string>;
  setSecretInputs: (value: Record<string, string>) => void;
  secretStatus: Record<string, { configured?: boolean; last4?: string }>;
}) {
  const path = `web_search.${fieldKey}`;
  const label = CONFIG_LABELS[path] || humanize(fieldKey);
  const presentation = configFieldPresentation(path);
  const status = secretStatus[path];

  if (status !== undefined) {
    const placeholder = status.configured
      ? t('••••••••{{last4}}（留空保留）', { last4: status.last4 || '' })
      : t('输入 API Key');
    return <label className="field web-search-secret-field">
      <span>{t(label)}</span>
      <input
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={secretInputs[path] || ''}
        onChange={event => setSecretInputs({ ...secretInputs, [path]: event.target.value })}
        placeholder={placeholder}
      />
      <small>{status.configured
        ? t('当前已配置 · 尾号 {{last4}}', { last4: status.last4 || '' })
        : t(presentation.hint || '当前未配置')}</small>
    </label>;
  }

  if (presentation.editor === 'choice') {
    const choices = presentation.choices || [];
    const current = String(value ?? '');
    return <label className="field web-search-choice-field">
      <span>{t(label)}</span>
      <select value={current} onChange={event => change(fieldKey, event.target.value)}>
        {!choices.some(choice => choice.value === current) && <option value={current}>{current}</option>}
        {choices.map(choice => <option key={choice.value || 'default'} value={choice.value}>{t(choice.label)}</option>)}
      </select>
      {presentation.hint && <small>{t(presentation.hint)}</small>}
    </label>;
  }

  if (typeof value === 'boolean') {
    return <div className="field web-search-toggle-field">
      <label className="switch">
        <input type="checkbox" checked={value} onChange={event => change(fieldKey, event.target.checked)} />
        <i />
        <span>{t(label)}</span>
      </label>
      {presentation.hint && <small>{t(presentation.hint)}</small>}
    </div>;
  }

  if (typeof value === 'number') {
    return <label className="field">
      <span>{t(label)}</span>
      <input
        type="number"
        value={value}
        min={presentation.minimum}
        max={presentation.maximum}
        step={presentation.step ?? 'any'}
        onChange={event => change(fieldKey, Number(event.target.value))}
      />
      {presentation.hint && <small>{t(presentation.hint)}</small>}
    </label>;
  }

  return <label className="field">
    <span>{t(label)}</span>
    <input
      type={presentation.inputType === 'url' ? 'url' : 'text'}
      spellCheck={false}
      value={String(value ?? '')}
      onChange={event => change(fieldKey, event.target.value)}
      placeholder={presentation.placeholder}
    />
    {presentation.hint && <small>{t(presentation.hint)}</small>}
  </label>;
}

function ProviderCard({
  provider,
  value,
  open,
  active,
  disabled,
  configured,
  onToggleOpen,
  onToggleEnabled,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: {
  provider: Provider;
  value: Json;
  open: boolean;
  active: boolean;
  disabled: boolean;
  configured: boolean;
  onToggleOpen: () => void;
  onToggleEnabled: (enabled: boolean) => void;
  change: (key: string, next: unknown) => void;
  secretInputs: Record<string, string>;
  setSecretInputs: (value: Record<string, string>) => void;
  secretStatus: Record<string, { configured?: boolean; last4?: string }>;
}) {
  const bodyId = `web-search-provider-${provider.id}`;
  const state = disabled ? 'muted' : provider.keyless || configured ? 'ok' : 'warn';
  const stateLabel = disabled
    ? '已禁用'
    : provider.keyless
      ? '无需密钥'
      : configured
        ? '已配置'
        : '未配置';
  const className = [
    'web-search-provider',
    active ? 'is-active' : '',
    disabled ? 'is-disabled' : '',
  ].filter(Boolean).join(' ');

  return <article className={className}>
    <header className="web-search-provider-head">
      <button
        type="button"
        className="web-search-provider-toggle"
        aria-expanded={open}
        aria-controls={open ? bodyId : undefined}
        onClick={onToggleOpen}
      >
        <span className="web-search-provider-mark" aria-hidden="true"><Globe size={16} /></span>
        <span className="web-search-provider-title">
          <b>{t(provider.label)}</b>
          <small title={t(provider.summary)}>{t(provider.summary)}</small>
        </span>
        {active && <span className="web-search-provider-active">{t('当前生效')}</span>}
        <span className={`web-search-provider-state ${state}`}>{t(stateLabel)}</span>
        <ChevronDown className={`web-search-provider-chevron${open ? ' open' : ''}`} size={15} aria-hidden="true" />
      </button>
      <label className="switch web-search-provider-switch" title={t('启用 {{name}}', { name: t(provider.label) })}>
        <input
          type="checkbox"
          checked={!disabled}
          onChange={event => onToggleEnabled(event.target.checked)}
          aria-label={t('启用 {{name}}', { name: t(provider.label) })}
        />
        <i />
        <span className="sr-only">{t('启用 {{name}}', { name: t(provider.label) })}</span>
      </label>
    </header>
    {open && <div className="web-search-provider-body" id={bodyId}>
      <div className="web-search-provider-fields">
        {provider.fields.map(fieldKey => <WebSearchField
          key={fieldKey}
          fieldKey={fieldKey}
          value={value[fieldKey]}
          change={change}
          secretInputs={secretInputs}
          setSecretInputs={setSecretInputs}
          secretStatus={secretStatus}
        />)}
      </div>
      {disabled && <p className="web-search-provider-note">{t('已禁用；不会参与自动、固定或模型指定。')}</p>}
    </div>}
  </article>;
}

export function WebSearchSettingsPanel({
  value,
  change,
  secretInputs,
  setSecretInputs,
  secretStatus,
}: SettingsPanelProps) {
  const order = useMemo(() => searchBackendOrder(value.provider_order), [value.provider_order]);
  const disabled = useMemo(
    () => new Set(Array.isArray(value.disabled_providers) ? value.disabled_providers.map(String) : []),
    [value.disabled_providers],
  );
  const configured = useMemo(
    () => new Set(PROVIDERS.filter(provider => providerConfigured(
      provider.id,
      value,
      secretInputs,
      secretStatus,
    )).map(provider => provider.id)),
    [secretInputs, secretStatus, value],
  );
  const [openProviders, setOpenProviders] = useState<Set<string>>(
    () => new Set(PROVIDERS.filter(provider => configured.has(provider.id)).map(provider => provider.id)),
  );
  const [orderOpen, setOrderOpen] = useState(false);
  const strategy = String(value.strategy || 'auto');
  const pinnedProvider = String(value.provider || 'ddgs');
  const enabledCount = PROVIDERS.filter(provider => !disabled.has(provider.id)).length;
  const configuredCount = configured.size;
  const available = order.filter(id => !disabled.has(id) && configured.has(id));
  const activeProvider = strategy === 'fixed' && available.includes(pinnedProvider)
    ? pinnedProvider
    : available[0] || '';
  const activeBackend = PROVIDER_BY_ID.get(activeProvider);
  const pinnedEnabled = !disabled.has(pinnedProvider);
  const pinnedConfigured = configured.has(pinnedProvider);
  const allExpanded = PROVIDERS.length > 0 && openProviders.size === PROVIDERS.length;
  const strategyPresentation = configFieldPresentation('web_search.strategy');
  const providerPresentation = configFieldPresentation('web_search.provider');
  const timeoutPresentation = configFieldPresentation('web_search.timeout_seconds');

  useEffect(() => {
    if (strategy !== 'fixed') return;
    setOpenProviders(current => current.has(pinnedProvider) ? current : new Set(current).add(pinnedProvider));
  }, [pinnedProvider, strategy]);

  const toggleOpen = (id: string) => {
    setOpenProviders(current => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleProvider = (id: string, enabled: boolean) => {
    change('disabled_providers', order.filter(item => (item === id ? !enabled : disabled.has(item))));
  };

  return <div className="web-search-settings">
    <section className="web-search-hero">
      <div className="web-search-hero-mark" aria-hidden="true"><Globe size={25} /></div>
      <div className="web-search-hero-copy">
        <span>{t('联网搜索')}</span>
        <h3>{t('搜索策略与后端凭据')}</h3>
        <p>{t('自动模式按顺序选择第一个已启用且已配置的后端；固定模式优先使用指定后端。保存后立即生效。')}</p>
      </div>
      <div className="web-search-hero-facts">
        <span>
          <b className={activeBackend ? '' : 'empty'}>{activeBackend ? t(activeBackend.label) : t('没有可用后端')}</b>
          {t('当前生效')}
        </span>
        <span><b>{configuredCount}</b>{t('已配置后端')}</span>
        <span><b>{enabledCount}/{PROVIDERS.length}</b>{t('已启用')}</span>
      </div>
    </section>

    <section className="web-search-section">
      <header>
        <div className="web-search-section-title">
          <SlidersHorizontal size={18} />
          <div>
            <b>{t('搜索策略')}</b>
            <small>{t('决定 search_web 如何在多个后端之间选择，以及单次请求的等待上限。')}</small>
          </div>
        </div>
      </header>
      <div className="web-search-policy-grid">
        <label className="field">
          <span>{t(CONFIG_LABELS['web_search.strategy'])}</span>
          <select value={strategy} onChange={event => change('strategy', event.target.value)}>
            {!strategyPresentation.choices?.some(choice => choice.value === strategy) && <option value={strategy}>{strategy}</option>}
            {strategyPresentation.choices?.map(choice => <option key={choice.value} value={choice.value}>{t(choice.label)}</option>)}
          </select>
          {strategyPresentation.hint && <small>{t(strategyPresentation.hint)}</small>}
        </label>
        <label className="field">
          <span>{t(CONFIG_LABELS['web_search.provider'])}</span>
          <select value={pinnedProvider} onChange={event => change('provider', event.target.value)}>
            {!order.includes(pinnedProvider) && <option value={pinnedProvider}>{pinnedProvider}</option>}
            {order.map(id => {
              const backend = PROVIDER_BY_ID.get(id);
              if (!backend) return null;
              const suffix = !disabled.has(id)
                ? configured.has(id) ? '' : ` · ${t('未配置')}`
                : ` · ${t('已禁用')}`;
              return <option key={id} value={id}>{t(backend.label)}{suffix}</option>;
            })}
          </select>
          {providerPresentation.hint && <small>{t(providerPresentation.hint)}</small>}
        </label>
        <label className="field">
          <span>{t(CONFIG_LABELS['web_search.timeout_seconds'])}</span>
          <input
            type="number"
            min={timeoutPresentation.minimum}
            max={timeoutPresentation.maximum}
            step={timeoutPresentation.step ?? 'any'}
            value={Number(value.timeout_seconds ?? 30)}
            onChange={event => change('timeout_seconds', Number(event.target.value))}
          />
          {timeoutPresentation.hint && <small>{t(timeoutPresentation.hint)}</small>}
        </label>
      </div>
      {strategy === 'fixed' && (!pinnedEnabled || !pinnedConfigured) && <div className="notice amber web-search-warning">
        <TriangleAlert size={16} />
        <span>{t('固定后端当前不可用，搜索会按自动顺序回退到下一个已配置后端。')}</span>
      </div>}
    </section>

    <section className="web-search-section">
      <header>
        <div className="web-search-section-title">
          <ListOrdered size={18} />
          <div>
            <b>{t('自动顺序')}</b>
            <small>{t('点击“调整顺序”后拖动排序；关闭的后端不会参与自动、固定或模型指定。')}</small>
          </div>
        </div>
        <div className="web-search-section-actions">
          <button
            type="button"
            className="ghost mini"
            aria-expanded={orderOpen}
            onClick={() => setOrderOpen(current => !current)}
          >
            {t(orderOpen ? '收起顺序' : '调整顺序')}
          </button>
        </div>
      </header>
      {orderOpen
        ? <SearchBackendOrderEditor value={value} onChange={change} heading={false} />
        : <div className="web-search-order-summary" role="list" aria-label={t('当前自动顺序')}>
          {order.map((id, index) => {
            const backend = PROVIDER_BY_ID.get(id);
            if (!backend) return null;
            const className = [
              disabled.has(id) ? 'disabled' : '',
              id === activeProvider ? 'active' : '',
            ].filter(Boolean).join(' ');
            return <span className={className} role="listitem" key={id}>
              <i>{index + 1}</i>{t(backend.label)}
            </span>;
          })}
        </div>}
    </section>

    <section className="web-search-section">
      <header>
        <div className="web-search-section-title">
          <PlugZap size={18} />
          <div>
            <b>{t('后端凭据')}</b>
            <small>{t('只有已启用且已配置的后端才会被使用；未配置密钥的后端会被自动跳过。')}</small>
          </div>
        </div>
        <div className="web-search-section-actions">
          <button
            type="button"
            className="ghost mini"
            onClick={() => setOpenProviders(allExpanded ? new Set() : new Set(PROVIDERS.map(provider => provider.id)))}
          >
            {t(allExpanded ? '全部收起' : '全部展开')}
          </button>
        </div>
      </header>
      <div className="web-search-provider-list">
        {PROVIDERS.map(provider => <ProviderCard
          key={provider.id}
          provider={provider}
          value={value}
          open={openProviders.has(provider.id)}
          active={provider.id === activeProvider}
          disabled={disabled.has(provider.id)}
          configured={configured.has(provider.id)}
          onToggleOpen={() => toggleOpen(provider.id)}
          onToggleEnabled={enabled => toggleProvider(provider.id, enabled)}
          change={change}
          secretInputs={secretInputs}
          setSecretInputs={setSecretInputs}
          secretStatus={secretStatus}
        />)}
      </div>
    </section>
  </div>;
}
