import { useMemo, useState, type CSSProperties } from 'react';
import {
  Activity,
  BarChart3,
  Bot,
  Database,
  Download,
  FileJson,
  Gauge,
  Hammer,
  RefreshCw,
  Timer,
  TriangleAlert,
} from 'lucide-react';

import type {
  UsageModelStats,
  UsageProviderModelStats,
  UsageStats,
  UsageWindowStats,
} from '../api/types';
import { t, useAdminI18n } from '../i18n/admin';
import {
  ADMIN_USAGE_WINDOWS,
  USAGE_SCOPE_LABELS,
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

type AdminUsageAnalyticsProps = {
  stats: UsageStats | null;
  loading: boolean;
  error: string;
  onReload: () => void | Promise<void>;
};

type Comparison = {
  label: string;
  detail: string;
  tone: 'up' | 'down' | 'flat';
};

function finite(value?: number | null): number {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function formatDurationSeconds(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
  const rounded = Math.round(value);
  if (rounded >= 60) {
    return t('{{minutes}}分 {{seconds}}秒', {
      minutes: Math.floor(rounded / 60),
      seconds: String(rounded % 60).padStart(2, '0'),
    });
  }
  if (value < 10 && Math.abs(value - rounded) >= 0.05) {
    return t('{{seconds}}秒', { seconds: value.toFixed(1) });
  }
  return t('{{seconds}}秒', { seconds: rounded });
}

function formatOptionalTokenUnits(value?: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatTokenUnits(value) : '—';
}

function comparisonFor(current: number, previous?: number | null): Comparison {
  if (typeof previous !== 'number' || !Number.isFinite(previous)) {
    return { label: '—', detail: t('暂无可比基线'), tone: 'flat' };
  }
  if (previous === 0) {
    if (current === 0) return { label: '0%', detail: t('与上一周期持平'), tone: 'flat' };
    return { label: t('新增'), detail: t('上一周期为零'), tone: 'up' };
  }
  const rate = ((current - previous) / previous) * 100;
  return {
    label: `${rate > 0 ? '+' : ''}${rate.toFixed(1)}%`,
    detail: t('上一周期 {{count}} Token', { count: formatTokenUnits(previous) }),
    tone: rate > 0 ? 'up' : rate < 0 ? 'down' : 'flat',
  };
}

function csvCell(value: string | number): string {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadText(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function exportCsv(stats: UsageStats) {
  const columns = [
    'date',
    'input_tokens',
    'output_tokens',
    'cached_tokens',
    'total_tokens',
    'llm_calls',
    'tracked_calls',
    'untracked_calls',
    'estimated_calls',
    'tool_calls',
    'thinking_seconds',
  ];
  const rows = (stats.daily || []).map(item => columns.map(column => (
    csvCell(column === 'date' ? item.date : finite(item[column as keyof UsageWindowStats] as number))
  )).join(','));
  const stamp = (stats.generated_at || new Date().toISOString()).slice(0, 10);
  downloadText(`coworker-usage-${stamp}.csv`, `\uFEFF${columns.join(',')}\n${rows.join('\n')}\n`, 'text/csv;charset=utf-8');
}

function exportJson(stats: UsageStats) {
  const stamp = (stats.generated_at || new Date().toISOString()).slice(0, 10);
  downloadText(
    `coworker-usage-${stamp}.json`,
    `${JSON.stringify(stats, null, 2)}\n`,
    'application/json;charset=utf-8',
  );
}

function MetricCard({
  label,
  value,
  detail,
  icon: Icon,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  icon: typeof Database;
  tone?: Comparison['tone'];
}) {
  return <article className={`usage-analytics-metric${tone ? ` ${tone}` : ''}`}>
    <Icon size={16} />
    <span>{label}</span>
    <strong>{value}</strong>
    <small>{detail}</small>
  </article>;
}

export function AdminUsageAnalytics({ stats, loading, error, onReload }: AdminUsageAnalyticsProps) {
  const { language } = useAdminI18n();
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('last_7_days');
  const windowStats = stats?.[windowKey];
  const previousStats = windowKey === 'lifetime' ? undefined : stats?.previous?.[windowKey];
  const comparison = comparisonFor(
    finite(windowStats?.total_tokens),
    previousStats ? finite(previousStats.total_tokens) : undefined,
  );
  const daily = stats?.daily || [];
  const visibleDaily = windowKey === 'today' || windowKey === 'last_7_days'
    ? daily.slice(-7)
    : daily;
  const maxDailyTokens = Math.max(1, ...visibleDaily.map(item => finite(item.total_tokens)));
  const sourceEntries = useMemo(() => windowStats
    ? usageScopeEntries(windowStats)
      .filter(([, item]) => finite(item.total_tokens) > 0 || finite(item.llm_calls) > 0)
      .sort(([, left], [, right]) => (
        finite(right.total_tokens) - finite(left.total_tokens)
        || finite(right.llm_calls) - finite(left.llm_calls)
      ))
    : [], [windowStats]);
  const modelEntries = useMemo(() => {
    if (!windowStats) return [];
    const buckets: Record<string, UsageModelStats | UsageProviderModelStats> =
      windowStats.by_provider_model || windowStats.by_model || {};
    return Object.entries(buckets)
      .filter(([, item]) => finite(item.llm_calls) > 0 || totalFromModelStats(item) > 0)
      .sort(([, left], [, right]) => (
        totalFromModelStats(right) - totalFromModelStats(left)
        || finite(right.llm_calls) - finite(left.llm_calls)
      ))
      .slice(0, 8);
  }, [windowStats]);
  const toolEntries = useMemo(() => Object.entries(windowStats?.tools || {})
    .sort(([, left], [, right]) => finite(right) - finite(left))
    .slice(0, 8), [windowStats]);
  const maxToolCalls = Math.max(1, ...toolEntries.map(([, count]) => finite(count)));

  if (loading && !stats) return <div className="state-box"><span className="state-pulse" aria-hidden="true"><i /><i /><i /></span><span>{t('正在读取模型用量…')}</span></div>;
  if (error && !stats) return <div className="state-box error" role="alert"><TriangleAlert size={17} /><span>{error}</span></div>;
  if (!stats || !windowStats) return <div className="state-box error" role="alert"><TriangleAlert size={17} /><span>{t('用量统计暂不可用')}</span></div>;

  const totalTokens = finite(windowStats.total_tokens);
  const trackedCalls = finite(windowStats.tracked_calls);
  const untrackedCalls = finite(windowStats.untracked_calls);
  const estimatedCalls = finite(windowStats.estimated_calls);
  const coverage = windowStats.tracking_coverage;
  const generatedAt = stats.generated_at
    ? new Date(stats.generated_at).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US')
    : '—';

  return <div className="page-stack usage-analytics-page">
    <section className="admin-panel usage-analytics-hero">
      <header>
        <div>
          <p className="eyebrow">{t('用量分析')}</p>
          <h2>{t('模型调用与 Token 趋势')}</h2>
          <p>{t('本地运行统计，不等同于 Provider 账单。')}</p>
        </div>
        <div className="usage-analytics-actions">
          <button type="button" onClick={() => exportCsv(stats)}><Download size={14} />CSV</button>
          <button type="button" onClick={() => exportJson(stats)}><FileJson size={14} />JSON</button>
          <button type="button" className="icon-btn" onClick={() => void onReload()} disabled={loading} title={t('刷新用量分析')} aria-label={t('刷新用量分析')}><RefreshCw size={15} /></button>
        </div>
      </header>
      <div className="usage-analytics-windows" aria-label={t('统计窗口')}>
        {ADMIN_USAGE_WINDOWS.map(item => <button
          type="button"
          className={windowKey === item.key ? 'active' : ''}
          aria-pressed={windowKey === item.key}
          onClick={() => setWindowKey(item.key)}
          key={item.key}
        >{t(item.label)}</button>)}
      </div>
      <div className="usage-analytics-metrics">
        <MetricCard label={t('总 Token')} value={formatTokenUnits(totalTokens)} detail={t('输入 {{input}} / 输出 {{output}}', { input: formatTokenUnits(windowStats.input_tokens), output: formatTokenUnits(windowStats.output_tokens) })} icon={Database} />
        <MetricCard label={t('较上一周期')} value={comparison.label} detail={comparison.detail} icon={BarChart3} tone={comparison.tone} />
        <MetricCard label={t('单次平均')} value={formatOptionalTokenUnits(windowStats.avg_tokens_per_call)} detail={t('按已追踪调用计算')} icon={Gauge} />
        <MetricCard label={t('缓存命中')} value={formatCacheRate(windowStats.cache_rate)} detail={t('{{count}} 缓存 Token', { count: formatTokenUnits(windowStats.cached_tokens) })} icon={Activity} />
        <MetricCard label={t('Token 覆盖率')} value={formatCacheRate(coverage)} detail={t('{{tracked}} / {{total}} 次调用', { tracked: formatCount(trackedCalls), total: formatCount(windowStats.llm_calls) })} icon={Bot} />
        <MetricCard label={t('调用活动')} value={formatCount(windowStats.llm_calls)} detail={t('{{tools}} 工具 · {{thinking}} 平均思考', { tools: formatCount(windowStats.tool_calls), thinking: formatDurationSeconds(windowStats.avg_thinking_seconds) })} icon={Timer} />
      </div>
      {finite(windowStats.llm_calls) > 0 && (untrackedCalls > 0
        ? <div className="admin-usage-notice amber"><TriangleAlert size={16} /><span>{t('{{count}} 次模型调用没有可记录的 Token；当前合计可能偏低。', { count: formatCount(untrackedCalls) })}</span></div>
        : <div className="admin-usage-notice"><Activity size={16} /><span>{t('这个窗口内的模型调用都已记录 Token。')}</span></div>)}
      {estimatedCalls > 0 && <div className="usage-analytics-estimate-note">{t('其中 {{count}} 次使用本地估算值。', { count: formatCount(estimatedCalls) })}</div>}
    </section>

    <section className="admin-panel usage-trend-panel">
      <header>
        <div><h2>{t('{{days}} 日趋势', { days: visibleDaily.length })}</h2><p>{t('按本地日期汇总输入与输出 Token')}</p></div>
        <div className="usage-trend-legend"><span><i className="input" />{t('输入')}</span><span><i className="output" />{t('输出')}</span></div>
      </header>
      <div className="usage-trend-chart" role="img" aria-label={t('{{days}} 日 Token 趋势', { days: visibleDaily.length })}>
        {visibleDaily.map((item, index) => {
          const input = finite(item.input_tokens);
          const output = finite(item.output_tokens);
          const total = finite(item.total_tokens);
          const showLabel = visibleDaily.length <= 7 || index % 5 === 0 || index === visibleDaily.length - 1;
          return <article aria-label={t('{{date}}：输入 {{input}}，输出 {{output}}', { date: item.date, input: formatCount(input), output: formatCount(output) })} key={item.date}>
            <b>{formatTokenUnits(total)}</b>
            <div className="usage-trend-column"><div className="usage-trend-stack" style={{ '--h': clampPercent((total / maxDailyTokens) * 100) } as CSSProperties}>
              {input > 0 && <i className="input" style={{ flexGrow: input }} />}
              {output > 0 && <i className="output" style={{ flexGrow: output }} />}
            </div></div>
            <span>{showLabel ? item.date.slice(5).replace('-', '/') : ''}</span>
          </article>;
        })}
      </div>
    </section>

    <div className="usage-analytics-grid">
      <section className="admin-panel usage-model-table-panel">
        <header><div><h2>{t('模型与 Provider')}</h2><p>{t('按当前窗口 Token 排序')}</p></div><b>{modelEntries.length}</b></header>
        <div className="usage-model-table-wrap"><table className="usage-model-table">
          <thead><tr><th>{t('模型')}</th><th>{t('调用')}</th><th>Token</th><th>{t('单次平均')}</th><th>{t('缓存')}</th><th>{t('覆盖率')}</th></tr></thead>
          <tbody>{modelEntries.length ? modelEntries.map(([key, item]) => <tr key={key}>
            <td title={usageModelLabel(key, item)}>{usageModelLabel(key, item)}</td>
            <td>{formatCount(item.llm_calls)}</td>
            <td>{formatTokenUnits(item.total_tokens)}</td>
            <td>{formatOptionalTokenUnits(item.avg_tokens_per_call)}</td>
            <td>{formatCacheRate(item.cache_rate)}</td>
            <td>{formatCacheRate(item.tracking_coverage)}</td>
          </tr>) : <tr><td colSpan={6}>{t('暂无模型调用')}</td></tr>}</tbody>
        </table></div>
      </section>

      <section className="admin-panel usage-source-panel">
        <header><div><h2>{t('职责来源')}</h2><p>{t('谁消耗了模型 Token')}</p></div><b>{sourceEntries.length}</b></header>
        <div className="usage-source-cards">{sourceEntries.length ? sourceEntries.map(([name, item]) => {
          const share = totalTokens > 0 ? finite(item.total_tokens) / totalTokens : 0;
          return <article key={name}>
            <i className={usageScopeClassName(name)} />
            <span>{t(USAGE_SCOPE_LABELS[name] || name)}</span>
            <strong>{formatTokenUnits(item.total_tokens)}</strong>
            <small>{t('{{share}} · {{calls}} 次调用', { share: formatCacheRate(share), calls: formatCount(item.llm_calls) })}</small>
          </article>;
        }) : <p>{t('尚无来源用量')}</p>}</div>
      </section>

      <section className="admin-panel usage-tools-panel">
        <header><div><h2>{t('工具排行')}</h2><p>{t('按当前窗口调用次数')}</p></div><Hammer size={16} /></header>
        <div className="usage-tools-list">{toolEntries.length ? toolEntries.map(([name, count]) => <article key={name}>
          <div><span title={name}>{name}</span><b>{formatCount(count)}</b></div>
          <div><i style={{ '--w': clampPercent((finite(count) / maxToolCalls) * 100) } as CSSProperties} /></div>
        </article>) : <p>{t('暂无工具调用')}</p>}</div>
      </section>
    </div>

    <footer className="usage-analytics-foot">
      <span>{t('开始追踪：{{date}}', { date: stats.tracking_since || '—' })}</span>
      <span>{t('生成时间：{{date}}', { date: generatedAt })}</span>
      <span>{t('统计保存在本地运行目录中')}</span>
    </footer>
  </div>;
}
