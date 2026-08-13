import { useState, type MouseEvent } from 'react';
import {
  Activity,
  ArrowUpRight,
  Bot,
  CircleDollarSign,
  Database,
  TriangleAlert,
} from 'lucide-react';

import type { UsageStats } from '../api/types';
import { t, useAdminI18n } from '../i18n/admin';
import {
  USAGE_WINDOWS,
  formatCacheRate,
  formatCostSummary,
  formatCount,
  formatTokenUnits,
  type UsageWindowKey,
} from '../lib/usageStats';

type AdminUsageOverviewProps = {
  stats: UsageStats | null;
  loading: boolean;
  error: string;
  analyticsHref: string;
  onOpenAnalytics: (event: MouseEvent<HTMLAnchorElement>) => void;
};

export function AdminUsageOverview({
  stats,
  loading,
  error,
  analyticsHref,
  onOpenAnalytics,
}: AdminUsageOverviewProps) {
  const { language } = useAdminI18n();
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('today');
  const windowStats = stats?.[windowKey];
  const hasCalls = Number(windowStats?.llm_calls || 0) > 0;
  const untracked = Number(windowStats?.untracked_calls || 0);
  const estimated = Number(windowStats?.estimated_calls || 0);
  const executionIssues = Number(windowStats?.tool_errors || 0)
    + Number(windowStats?.skill_load_errors || 0)
    + Number(windowStats?.bubble_errors || 0)
    + Number(windowStats?.bubble_timeouts || 0);

  return <section className="admin-panel admin-usage-panel" aria-label={t('运行数据概览')}>
    <header>
      <div><h2>{t('运行数据概览')}</h2><p>{t('资源消耗、执行结果与数据可信度')}</p></div>
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
    {loading && !stats ? <div className="admin-usage-state" role="status"><Activity size={18} />{t('正在读取运行分析…')}</div>
      : error && !stats ? <div className="admin-usage-state error" role="alert"><TriangleAlert size={18} />{error}</div>
        : !windowStats ? <div className="admin-usage-state error" role="alert"><TriangleAlert size={18} />{t('运行分析暂不可用')}</div>
          : <>
            <div className="admin-usage-metrics compact">
              <article className="total"><Database size={17} /><span>{t('总 Token')}</span><strong>{formatTokenUnits(windowStats.total_tokens)}</strong><small>{t('{{count}} 次模型响应', { count: formatCount(windowStats.llm_calls) })}</small></article>
              <article className="cost"><CircleDollarSign size={17} /><span>{t('预估消费')}</span><strong>{formatCostSummary(windowStats.estimated_costs, language)}</strong><small>{t('定价覆盖 {{rate}} · {{count}} 未定价 Token', { rate: formatCacheRate(windowStats.pricing_coverage), count: formatTokenUnits(windowStats.unpriced_tokens) })}</small></article>
              <article><span>{t('输入 Token')}</span><strong>{formatTokenUnits(windowStats.input_tokens)}</strong><small>{t('已记录输入')}</small></article>
              <article><span>{t('输出 Token')}</span><strong>{formatTokenUnits(windowStats.output_tokens)}</strong><small>{t('已记录输出')}</small></article>
              <article><Bot size={16} /><span>{t('缓存 Token 占比')}</span><strong>{formatCacheRate(windowStats.cache_rate)}</strong><small>{t('{{count}} 缓存 Token', { count: formatTokenUnits(windowStats.cached_tokens) })}</small></article>
            </div>
            {!hasCalls && <div className="admin-usage-notice"><Activity size={16} /><span>{t('这个统计窗口尚未采集到模型调用。')}</span></div>}
            <div className="admin-usage-quality">
              <div>
                <span>{t('数据可信度')}</span>
                <strong>{t('{{exact}} 精确 · {{estimated}} 估算 · {{unknown}} 未追踪', {
                  exact: formatCount(windowStats.exact_calls),
                  estimated: formatCount(estimated),
                  unknown: formatCount(untracked),
                })}</strong>
              </div>
              <div className={executionIssues ? 'attention' : ''}>
                <span>{t('执行关注项')}</span>
                <strong>{executionIssues ? t('{{count}} 项需检查', { count: formatCount(executionIssues) }) : t('暂无异常')}</strong>
              </div>
              <a className="admin-usage-open" href={analyticsHref} onClick={onOpenAnalytics}>{t('打开运行分析')}<ArrowUpRight size={13} /></a>
            </div>
          </>}
  </section>;
}
