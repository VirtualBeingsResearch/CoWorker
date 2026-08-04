import { useMemo, useState, type CSSProperties } from 'react';
import {
  Activity,
  Bot,
  ChevronDown,
  ChevronUp,
  Database,
  Hammer,
  Timer,
  TriangleAlert,
} from 'lucide-react';

import type {
  UsageModelStats,
  UsageProviderModelStats,
  UsageStats,
} from '../api/types';
import { t } from '../i18n/admin';
import {
  USAGE_SCOPE_LABELS,
  USAGE_WINDOWS,
  clampPercent,
  formatCacheRate,
  formatCount,
  formatTokenUnits,
  totalFromModelStats,
  usageModelLabel,
  usageScopeClassName,
  usageScopeEntries,
  type UsageWindowKey,
} from '../lib/usageStats';

type AdminUsageOverviewProps = {
  stats: UsageStats | null;
  loading: boolean;
  error: string;
};

function formatDurationSeconds(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
  const rounded = Math.round(value);
  if (rounded >= 60) {
    const minutes = Math.floor(rounded / 60);
    const seconds = rounded % 60;
    return t('{{minutes}}分 {{seconds}}秒', {
      minutes,
      seconds: String(seconds).padStart(2, '0'),
    });
  }
  if (value < 10 && Math.abs(value - rounded) >= 0.05) {
    return t('{{seconds}}秒', { seconds: value.toFixed(1) });
  }
  return t('{{seconds}}秒', { seconds: rounded });
}

export function AdminUsageOverview({ stats, loading, error }: AdminUsageOverviewProps) {
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('today');
  const [expanded, setExpanded] = useState(false);
  const windowStats = stats?.[windowKey];
  const sourceEntries = useMemo(
    () => windowStats
      ? usageScopeEntries(windowStats).filter(([, item]) => (
        Number(item.total_tokens || 0) > 0 || Number(item.llm_calls || 0) > 0
      ))
      : [],
    [windowStats],
  );
  const modelEntries = useMemo(() => {
    if (!windowStats) return [];
    const buckets: Record<string, UsageModelStats | UsageProviderModelStats> =
      windowStats.by_provider_model || windowStats.by_model || {};
    return Object.entries(buckets)
      .filter(([, item]) => Number(item.llm_calls || 0) > 0 || totalFromModelStats(item) > 0)
      .sort(([, left], [, right]) => (
        totalFromModelStats(right) - totalFromModelStats(left)
        || Number(right.llm_calls || 0) - Number(left.llm_calls || 0)
      ))
      .slice(0, 4);
  }, [windowStats]);
  const hasCalls = Number(windowStats?.llm_calls || 0) > 0;
  const hasTokens = Number(windowStats?.total_tokens || 0) > 0;
  const sourceTokenTotal = sourceEntries.reduce(
    (sum, [, item]) => sum + Number(item.total_tokens || 0),
    0,
  );
  const maxModelValue = Math.max(
    1,
    ...modelEntries.map(([, item]) => (
      hasTokens ? totalFromModelStats(item) : Number(item.llm_calls || 0)
    )),
  );
  const detailsId = 'admin-usage-details';

  return <section className="admin-panel admin-usage-panel" aria-label={t('模型 Token 用量')}>
    <header>
      <div><h2>{t('模型 Token 用量')}</h2><p>{t('本地运行统计，不等同于 Provider 账单。')}</p></div>
      <div className="admin-usage-windows" aria-label={t('统计窗口')}>
        {USAGE_WINDOWS.map(item => <button
          type="button"
          className={windowKey === item.key ? 'active' : ''}
          aria-pressed={windowKey === item.key}
          onClick={() => setWindowKey(item.key)}
          key={item.key}
        >{t(item.label)}</button>)}
      </div>
    </header>
    {loading && !stats ? <div className="admin-usage-state" role="status"><Activity size={18} />{t('正在读取模型用量…')}</div>
      : error && !stats ? <div className="admin-usage-state error" role="alert"><TriangleAlert size={18} />{error}</div>
        : !windowStats ? <div className="admin-usage-state error" role="alert"><TriangleAlert size={18} />{t('用量统计暂不可用')}</div>
          : <>
            <div className="admin-usage-metrics">
              <article className="total"><Database size={17} /><span>{t('总 Token')}</span><strong>{formatTokenUnits(windowStats.total_tokens)}</strong><small>{t('{{count}} 次模型响应', { count: formatCount(windowStats.llm_calls) })}</small></article>
              <article><span>{t('输入 Token')}</span><strong>{formatTokenUnits(windowStats.input_tokens)}</strong><small>{t('已记录输入')}</small></article>
              <article><span>{t('输出 Token')}</span><strong>{formatTokenUnits(windowStats.output_tokens)}</strong><small>{t('已记录输出')}</small></article>
              <article><span>{t('缓存 Token')}</span><strong>{formatTokenUnits(windowStats.cached_tokens)}</strong><small>{t('命中率 {{rate}}', { rate: formatCacheRate(windowStats.cache_rate) })}</small></article>
              <article><Bot size={16} /><span>{t('调用与工具')}</span><strong>{formatCount(windowStats.llm_calls)}</strong><small>{t('{{count}} 次工具调用', { count: formatCount(windowStats.tool_calls) })}</small></article>
            </div>
            {!hasCalls && <div className="admin-usage-notice"><Activity size={16} /><span>{t('这个统计窗口尚未采集到模型调用。')}</span></div>}
            {hasCalls && !hasTokens && <div className="admin-usage-notice amber"><TriangleAlert size={16} /><span>{t('已有模型调用，但 Provider 没有返回可记录的 Token。')}</span></div>}
            <div className="admin-usage-glance">
              <div className="admin-usage-source-glance">
                <span>{t('来源拆分')}</span>
                <div className="admin-usage-source-track" aria-hidden="true">
                  {hasTokens && sourceEntries.length ? sourceEntries.map(([name, item]) => <i
                    className={usageScopeClassName(name)}
                    style={{ '--w': clampPercent((Number(item.total_tokens || 0) / Math.max(1, sourceTokenTotal)) * 100) } as CSSProperties}
                    key={name}
                  />) : <i className="scope-empty" style={{ '--w': '100%' } as CSSProperties} />}
                </div>
                <small>{sourceEntries.length
                  ? sourceEntries.slice(0, 3).map(([name]) => t(USAGE_SCOPE_LABELS[name] || name)).join(' · ')
                  : t('尚无来源用量')}</small>
              </div>
              <div className="admin-usage-glance-fact"><Hammer size={15} /><span>{t('工具')}</span><b>{formatCount(windowStats.tool_calls)}</b></div>
              <div className="admin-usage-glance-fact"><Timer size={15} /><span>{t('平均思考')}</span><b>{formatDurationSeconds(windowStats.avg_thinking_seconds)}</b></div>
              <button
                type="button"
                className="admin-usage-expand"
                aria-expanded={expanded}
                aria-controls={detailsId}
                onClick={() => setExpanded(value => !value)}
              >{t(expanded ? '收起详情' : '查看拆分')}{expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}</button>
            </div>
            {expanded && <div className="admin-usage-details" id={detailsId}>
              <section>
                <header><span>{t('模型用量')}</span><b>{t(hasTokens ? '按 Token' : '按调用')}</b></header>
                <div className="admin-usage-model-list">{modelEntries.length ? modelEntries.map(([key, item]) => {
                  const value = hasTokens ? totalFromModelStats(item) : Number(item.llm_calls || 0);
                  const label = usageModelLabel(key, item);
                  return <article key={key} title={label}>
                    <div><span>{label}</span><b>{hasTokens ? formatTokenUnits(item.total_tokens) : formatCount(item.llm_calls)}</b></div>
                    <div className="admin-usage-model-track"><i style={{ '--w': clampPercent((value / maxModelValue) * 100) } as CSSProperties} /></div>
                    <small>{t('{{calls}} 次调用 · 缓存 {{rate}}', { calls: formatCount(item.llm_calls), rate: formatCacheRate(item.cache_rate) })}</small>
                  </article>;
                }) : <p>{t('暂无模型调用')}</p>}</div>
              </section>
              <section>
                <header><span>{t('来源用量')}</span><b>{t('按职责')}</b></header>
                <div className="admin-usage-source-list">{sourceEntries.length ? sourceEntries.map(([name, item]) => <article key={name}>
                  <i className={usageScopeClassName(name)} />
                  <span>{t(USAGE_SCOPE_LABELS[name] || name)}</span>
                  <b>{formatTokenUnits(item.total_tokens)}</b>
                  <small>{t('{{count}} 次调用', { count: formatCount(item.llm_calls) })}</small>
                </article>) : <p>{t('尚无来源用量')}</p>}</div>
              </section>
            </div>}
          </>}
  </section>;
}
