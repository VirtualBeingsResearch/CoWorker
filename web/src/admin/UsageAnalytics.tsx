import { useMemo, useState, type CSSProperties } from 'react';
import {
  Activity,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  Database,
  Download,
  FileJson,
  Hammer,
  Orbit,
  RefreshCw,
  Sparkles,
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

type AttentionItem = {
  tone: 'danger' | 'amber' | 'info';
  title: string;
  detail: string;
};

const DETAIL_LIMIT = 8;

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

function comparisonFor(current: number, previous?: number | null) {
  if (typeof previous !== 'number' || !Number.isFinite(previous)) {
    return { label: '—', detail: t('暂无可比基线') };
  }
  if (previous === 0) {
    if (current === 0) return { label: '0%', detail: t('与上一周期持平') };
    return { label: t('新增'), detail: t('上一周期为零') };
  }
  const rate = ((current - previous) / previous) * 100;
  return {
    label: `${rate > 0 ? '+' : ''}${rate.toFixed(1)}%`,
    detail: t('上一周期 {{count}} Token', { count: formatTokenUnits(previous) }),
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
    'exact_calls',
    'estimated_calls',
    'untracked_calls',
    'tool_calls',
    'tool_successes',
    'tool_errors',
    'skill_load_attempts',
    'skill_load_errors',
    'automatic_skill_loads',
    'bubble_runs',
    'bubble_done',
    'bubble_errors',
    'bubble_timeouts',
  ];
  const rows = (stats.daily || []).map(item => columns.map(column => (
    csvCell(column === 'date' ? item.date : finite(item[column as keyof UsageWindowStats] as number))
  )).join(','));
  const stamp = (stats.generated_at || new Date().toISOString()).slice(0, 10);
  downloadText(`coworker-runtime-${stamp}.csv`, `\uFEFF${columns.join(',')}\n${rows.join('\n')}\n`, 'text/csv;charset=utf-8');
}

function exportJson(stats: UsageStats) {
  const stamp = (stats.generated_at || new Date().toISOString()).slice(0, 10);
  downloadText(
    `coworker-runtime-${stamp}.json`,
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
  tone?: 'tool' | 'skill' | 'autonomy';
}) {
  return <article className={`usage-analytics-metric${tone ? ` metric-${tone}` : ''}`}>
    <Icon size={16} />
    <span>{label}</span>
    <strong>{value}</strong>
    <small>{detail}</small>
  </article>;
}

function DetailToggle({
  expanded,
  label,
  total,
  onToggle,
}: {
  expanded: boolean;
  label: string;
  total: number;
  onToggle: () => void;
}) {
  if (total <= DETAIL_LIMIT) return null;
  const accessibleLabel = expanded
    ? t('{{section}}：收起', { section: label })
    : t('{{section}}：查看全部 {{count}} 项', { section: label, count: formatCount(total) });
  return <button type="button" className="usage-list-toggle" onClick={onToggle} aria-expanded={expanded} aria-label={accessibleLabel}>
    {expanded ? t('收起') : t('查看全部 {{count}} 项', { count: formatCount(total) })}
    {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
  </button>;
}

function attentionItems(stats: UsageWindowStats): AttentionItem[] {
  const items: AttentionItem[] = [];
  const untracked = finite(stats.untracked_calls);
  const estimated = finite(stats.estimated_calls);
  const toolErrors = finite(stats.tool_errors);
  const skillErrors = finite(stats.skill_load_errors);
  const bubbleErrors = finite(stats.bubble_errors);
  const bubbleTimeouts = finite(stats.bubble_timeouts);
  if (untracked > 0) items.push({
    tone: 'amber',
    title: t('Token 数据不完整'),
    detail: t('{{count}} 次模型调用没有可记录的 Token。', { count: formatCount(untracked) }),
  });
  if (estimated > 0) items.push({
    tone: 'info',
    title: t('包含本地估算'),
    detail: t('{{count}} 次调用使用估算 Token，已与精确值分开。', { count: formatCount(estimated) }),
  });
  if (toolErrors > 0) items.push({
    tone: 'danger',
    title: t('工具返回错误'),
    detail: t('{{count}} 次工具调用返回错误。', { count: formatCount(toolErrors) }),
  });
  if (skillErrors > 0) items.push({
    tone: 'danger',
    title: t('技能加载失败'),
    detail: t('{{count}} 次显式技能加载失败。', { count: formatCount(skillErrors) }),
  });
  if (bubbleErrors + bubbleTimeouts > 0) items.push({
    tone: 'danger',
    title: t('自主执行未正常完成'),
    detail: t('{{errors}} 次错误 · {{timeouts}} 次超时', {
      errors: formatCount(bubbleErrors),
      timeouts: formatCount(bubbleTimeouts),
    }),
  });
  return items;
}

export function AdminUsageAnalytics({ stats, loading, error, onReload }: AdminUsageAnalyticsProps) {
  const { language } = useAdminI18n();
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('last_7_days');
  const [expandedModels, setExpandedModels] = useState(false);
  const [expandedTools, setExpandedTools] = useState(false);
  const [expandedSkills, setExpandedSkills] = useState(false);
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
  const peakDaily = visibleDaily.reduce<(UsageWindowStats & { date: string }) | null>((peak, item) => (
    !peak || finite(item.total_tokens) > finite(peak.total_tokens) ? item : peak
  ), null);
  const sourceEntries = useMemo(() => windowStats
    ? usageScopeEntries(windowStats)
      .filter(([, item]) => finite(item.total_tokens) > 0 || finite(item.llm_calls) > 0)
      .sort(([, left], [, right]) => (
        finite(right.total_tokens) - finite(left.total_tokens)
        || finite(right.llm_calls) - finite(left.llm_calls)
      ))
    : [], [windowStats]);
  const allModelEntries = useMemo(() => {
    if (!windowStats) return [];
    const buckets: Record<string, UsageModelStats | UsageProviderModelStats> =
      windowStats.by_provider_model || windowStats.by_model || {};
    return Object.entries(buckets)
      .filter(([, item]) => finite(item.llm_calls) > 0 || totalFromModelStats(item) > 0)
      .sort(([, left], [, right]) => (
        totalFromModelStats(right) - totalFromModelStats(left)
        || finite(right.llm_calls) - finite(left.llm_calls)
      ));
  }, [windowStats]);
  const allToolEntries = useMemo(() => Object.entries(windowStats?.tool_outcomes || {})
    .sort(([, left], [, right]) => finite(right.calls) - finite(left.calls)), [windowStats]);
  const allSkillEntries = useMemo(() => Object.entries(windowStats?.skills || {})
    .sort(([, left], [, right]) => (
      finite(right.explicit_attempts) + finite(right.automatic_loads)
      - finite(left.explicit_attempts) - finite(left.automatic_loads)
    )), [windowStats]);
  const modelEntries = expandedModels ? allModelEntries : allModelEntries.slice(0, DETAIL_LIMIT);
  const toolEntries = expandedTools ? allToolEntries : allToolEntries.slice(0, DETAIL_LIMIT);
  const skillEntries = expandedSkills ? allSkillEntries : allSkillEntries.slice(0, DETAIL_LIMIT);

  if (loading && !stats) return <div className="state-box"><span className="state-pulse" aria-hidden="true"><i /><i /><i /></span><span>{t('正在读取运行分析…')}</span></div>;
  if (error && !stats) return <div className="state-box error" role="alert"><TriangleAlert size={17} /><span>{error}</span></div>;
  if (!stats || !windowStats) return <div className="state-box error" role="alert"><TriangleAlert size={17} /><span>{t('运行分析暂不可用')}</span></div>;

  const totalTokens = finite(windowStats.total_tokens);
  const llmCalls = finite(windowStats.llm_calls);
  const exactCalls = finite(windowStats.exact_calls);
  const estimatedCalls = finite(windowStats.estimated_calls);
  const untrackedCalls = finite(windowStats.untracked_calls);
  const attention = attentionItems(windowStats);
  const generatedAt = stats.generated_at
    ? new Date(stats.generated_at).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US')
    : '—';
  const bubbleScopes = [
    ['bubble', windowStats.by_scope?.bubble],
    ['subconscious', windowStats.by_scope?.subconscious],
  ] as const;

  return <div className="page-stack usage-analytics-page">
    <section className="admin-panel usage-analytics-hero">
      <header>
        <div>
          <p className="eyebrow">{t('运行分析')}</p>
          <h2>{t('资源消耗与执行结果')}</h2>
          <p>{t('本地脱敏聚合，不等同于 Provider 账单或结果质量评价。')}</p>
        </div>
        <div className="usage-analytics-actions">
          <button type="button" onClick={() => exportCsv(stats)}><Download size={14} />CSV</button>
          <button type="button" onClick={() => exportJson(stats)}><FileJson size={14} />JSON</button>
          <button type="button" className="icon-btn" onClick={() => void onReload()} disabled={loading} title={t('刷新运行分析')} aria-label={t('刷新运行分析')}><RefreshCw size={15} /></button>
        </div>
      </header>
      <div className="usage-analytics-toolbar">
        <div className="usage-analytics-windows" aria-label={t('统计窗口')}>
          {ADMIN_USAGE_WINDOWS.map(item => <button
            type="button"
            className={windowKey === item.key ? 'active' : ''}
            aria-pressed={windowKey === item.key}
            onClick={() => setWindowKey(item.key)}
            key={item.key}
          >{t(item.label)}</button>)}
        </div>
        <div className="usage-window-context">
          <span title={comparison.detail}>{t('较上一周期')} <b>{comparison.label}</b></span>
          <span>{t('缓存 Token 占比')} <b>{formatCacheRate(windowStats.cache_rate)}</b></span>
        </div>
      </div>
      <div className="usage-analytics-metrics">
        <MetricCard label={t('总 Token')} value={formatTokenUnits(totalTokens)} detail={t('输入 {{input}} / 输出 {{output}}', { input: formatTokenUnits(windowStats.input_tokens), output: formatTokenUnits(windowStats.output_tokens) })} icon={Database} />
        <MetricCard label={t('模型调用')} value={formatCount(llmCalls)} detail={t('单次平均 {{count}} Token', { count: formatOptionalTokenUnits(windowStats.avg_tokens_per_call) })} icon={Bot} />
        <MetricCard label={t('缓存 Token')} value={formatTokenUnits(windowStats.cached_tokens)} detail={t('命中率 {{rate}}', { rate: formatCacheRate(windowStats.cache_rate) })} icon={Activity} />
        <MetricCard label={t('工具执行')} value={formatCount(windowStats.tool_calls)} detail={t('{{success}} 成功 · {{errors}} 错误 · {{pending}} 未结算', {
          success: formatCount(windowStats.tool_successes),
          errors: formatCount(windowStats.tool_errors),
          pending: formatCount(windowStats.tool_incomplete),
        })} icon={Hammer} tone="tool" />
        <MetricCard label={t('技能加载')} value={formatCount(finite(windowStats.skill_load_attempts) + finite(windowStats.automatic_skill_loads))} detail={t('{{explicit}} 显式 · {{automatic}} 自动 · {{errors}} 失败', {
          explicit: formatCount(windowStats.skill_load_attempts),
          automatic: formatCount(windowStats.automatic_skill_loads),
          errors: formatCount(windowStats.skill_load_errors),
        })} icon={Sparkles} tone="skill" />
        <MetricCard label={t('自主执行')} value={formatCount(windowStats.bubble_runs)} detail={t('{{done}} 完成 · {{errors}} 错误 · {{timeouts}} 超时', {
          done: formatCount(windowStats.bubble_done),
          errors: formatCount(windowStats.bubble_errors),
          timeouts: formatCount(windowStats.bubble_timeouts),
        })} icon={Orbit} tone="autonomy" />
      </div>
      <div className="usage-quality">
        <div className="usage-quality-copy">
          <span>{t('数据可信度')}</span>
          <strong>{llmCalls ? t('{{exact}} 精确 · {{estimated}} 估算 · {{unknown}} 未追踪', {
            exact: formatCount(exactCalls),
            estimated: formatCount(estimatedCalls),
            unknown: formatCount(untrackedCalls),
          }) : t('暂无模型调用')}</strong>
        </div>
        <div className="usage-quality-track" aria-hidden="true">
          <i className="exact" style={{ '--w': clampPercent(llmCalls ? exactCalls / llmCalls * 100 : 0) } as CSSProperties} />
          <i className="estimated" style={{ '--w': clampPercent(llmCalls ? estimatedCalls / llmCalls * 100 : 0) } as CSSProperties} />
          <i className="unknown" style={{ '--w': clampPercent(llmCalls ? untrackedCalls / llmCalls * 100 : 100) } as CSSProperties} />
        </div>
        <div className="usage-quality-legend">
          <span><i className="exact" />{t('精确')} {formatCacheRate(windowStats.exact_coverage)}</span>
          <span><i className="estimated" />{t('本地估算')} {formatCacheRate(llmCalls ? estimatedCalls / llmCalls : null)}</span>
          <span><i className="unknown" />{t('未追踪')} {formatCacheRate(llmCalls ? untrackedCalls / llmCalls : null)}</span>
        </div>
      </div>
      <div className={`usage-attention${attention.length ? '' : ' clear'}`} aria-label={attention.length ? t('需关注') : t('暂无异常')}>
        <header>{attention.length ? <CircleAlert size={15} /> : <CheckCircle2 size={15} />}<span>{attention.length ? t('需关注') : t('暂无异常')}</span>{attention.length > 0 && <b>{attention.length}</b>}</header>
        {attention.length > 0 && <div>{attention.map(item => <article className={item.tone} key={item.title}>
          <strong>{item.title}</strong><small>{item.detail}</small>
        </article>)}</div>}
      </div>
    </section>

    <div className="usage-resource-dashboard">
      <section className="admin-panel usage-trend-panel">
      <header>
        <div><h2>{t('{{days}} 日趋势', { days: visibleDaily.length })}</h2><p>{t('按本地日期汇总输入与输出 Token')}</p></div>
        <div className="usage-trend-head-meta">
          {peakDaily && <span>{t('峰值')} <b>{formatTokenUnits(peakDaily.total_tokens)}</b> · {peakDaily.date.slice(5).replace('-', '/')}</span>}
          <div className="usage-trend-legend"><span><i className="input" />{t('输入')}</span><span><i className="output" />{t('输出')}</span></div>
        </div>
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

      <section className="admin-panel usage-model-table-panel">
        <header><div><h2>{t('模型与 Provider')}</h2><p>{t('资源消耗驱动，按当前窗口 Token 排序')}</p></div><div className="usage-panel-actions"><b>{allModelEntries.length}</b><DetailToggle expanded={expandedModels} label={t('模型与 Provider')} total={allModelEntries.length} onToggle={() => setExpandedModels(value => !value)} /></div></header>
        <div className="usage-model-table-wrap"><table className="usage-model-table">
          <thead><tr><th>{t('模型')}</th><th>{t('调用')}</th><th>Token</th><th>{t('单次平均')}</th><th>{t('缓存 Token 占比')}</th><th>{t('精确')}</th></tr></thead>
          <tbody>{modelEntries.length ? modelEntries.map(([key, item]) => <tr key={key}>
            <td title={usageModelLabel(key, item)}>{usageModelLabel(key, item)}</td>
            <td>{formatCount(item.llm_calls)}</td>
            <td className="usage-model-token"><span>{formatTokenUnits(item.total_tokens)}</span><i aria-hidden="true" style={{ '--w': clampPercent(totalTokens ? totalFromModelStats(item) / totalTokens * 100 : 0) } as CSSProperties} /></td>
            <td>{formatOptionalTokenUnits(item.avg_tokens_per_call)}</td>
            <td>{formatCacheRate(item.cache_rate)}</td>
            <td>{formatCacheRate(item.exact_coverage)}</td>
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
            <div className="usage-source-track" aria-hidden="true"><span style={{ '--w': clampPercent(share * 100) } as CSSProperties} /></div>
          </article>;
        }) : <p>{t('尚无来源用量')}</p>}</div>
      </section>
    </div>

    <div className="usage-execution-grid">
      <section className="admin-panel usage-tools-panel">
        <header>
          <div><h2>{t('工具执行')}</h2><p>{t('按调用结果统计，不推断效率')}</p></div>
          <div className="usage-panel-actions"><span className="usage-panel-total"><Hammer size={15} />{formatCount(windowStats.tool_calls)}</span><DetailToggle expanded={expandedTools} label={t('工具执行')} total={allToolEntries.length} onToggle={() => setExpandedTools(value => !value)} /></div>
        </header>
        <div className="usage-outcome-summary">
          <span className="success"><CheckCircle2 size={13} />{t('{{count}} 成功', { count: formatCount(windowStats.tool_successes) })}</span>
          <span className="error"><TriangleAlert size={13} />{t('{{count}} 错误', { count: formatCount(windowStats.tool_errors) })}</span>
          <span><CircleAlert size={13} />{t('{{count}} 未结算', { count: formatCount(windowStats.tool_incomplete) })}</span>
        </div>
        <div className="usage-execution-list">{toolEntries.length ? toolEntries.map(([name, item]) => {
          const calls = Math.max(1, finite(item.calls));
          return <article className={finite(item.errors) > 0 ? 'has-error' : finite(item.incomplete) > 0 ? 'has-pending' : ''} key={name}>
            <div><strong title={name}>{name}</strong><b>{formatCount(item.calls)}</b></div>
            <div className="usage-outcome-track" aria-hidden="true">
              <i className="success" style={{ '--w': clampPercent(finite(item.successes) / calls * 100) } as CSSProperties} />
              <i className="error" style={{ '--w': clampPercent(finite(item.errors) / calls * 100) } as CSSProperties} />
              <i className="pending" style={{ '--w': clampPercent(finite(item.incomplete) / calls * 100) } as CSSProperties} />
            </div>
            <small>{t('{{success}} 成功 · {{errors}} 错误 · {{pending}} 未结算', {
              success: formatCount(item.successes),
              errors: formatCount(item.errors),
              pending: formatCount(item.incomplete),
            })}</small>
          </article>;
        }) : <p>{t('暂无工具调用')}</p>}</div>
      </section>

      <section className="admin-panel usage-skills-panel">
        <header>
          <div><h2>{t('技能加载')}</h2><p>{t('显式请求与 Palace 自动加载分开统计')}</p></div>
          <div className="usage-panel-actions"><span className="usage-panel-total"><Sparkles size={15} />{formatCount(finite(windowStats.skill_load_attempts) + finite(windowStats.automatic_skill_loads))}</span><DetailToggle expanded={expandedSkills} label={t('技能加载')} total={allSkillEntries.length} onToggle={() => setExpandedSkills(value => !value)} /></div>
        </header>
        <div className="usage-outcome-summary">
          <span className="success"><CheckCircle2 size={13} />{t('{{count}} 显式成功', { count: formatCount(windowStats.skill_load_successes) })}</span>
          <span className="error"><TriangleAlert size={13} />{t('{{count}} 显式失败', { count: formatCount(windowStats.skill_load_errors) })}</span>
          <span><Orbit size={13} />{t('{{count}} 自动加载', { count: formatCount(windowStats.automatic_skill_loads) })}</span>
        </div>
        <div className="usage-skill-list">{skillEntries.length ? skillEntries.map(([name, item]) => <article className={finite(item.explicit_errors) > 0 ? 'has-error' : finite(item.explicit_incomplete) > 0 ? 'has-pending' : ''} key={name}>
          <strong title={name}>{name}</strong>
          <span>{t('{{success}} / {{attempts}} 显式成功', {
            success: formatCount(item.explicit_successes),
            attempts: formatCount(item.explicit_attempts),
          })}</span>
          <b>{t('{{count}} 自动', { count: formatCount(item.automatic_loads) })}</b>
          {(finite(item.explicit_errors) > 0 || finite(item.explicit_incomplete) > 0) && <small>{t('{{errors}} 失败 · {{pending}} 未结算', {
            errors: formatCount(item.explicit_errors),
            pending: formatCount(item.explicit_incomplete),
          })}</small>}
        </article>) : <p>{t('暂无技能加载记录')}</p>}</div>
      </section>
    </div>

    <section className="admin-panel usage-bubble-panel">
      <header>
        <div><h2>{t('自主执行')}</h2><p>{t('Bubble 与潜意识的技术终态，不评价结果质量')}</p></div>
        <span className="usage-panel-total"><Orbit size={15} />{formatCount(windowStats.bubble_runs)}</span>
      </header>
      <div className="usage-bubble-statuses">
        <span className="success"><b>{formatCount(windowStats.bubble_done)}</b>{t('完成')}</span>
        <span className="error"><b>{formatCount(windowStats.bubble_errors)}</b>{t('错误')}</span>
        <span className="amber"><b>{formatCount(windowStats.bubble_timeouts)}</b>{t('超时')}</span>
        <span><b>{formatCount(windowStats.bubble_cancelled)}</b>{t('取消')}</span>
      </div>
      <div className="usage-bubble-modes">{bubbleScopes.map(([name, item]) => <article key={name}>
        <div><i className={usageScopeClassName(name)} /><strong>{t(USAGE_SCOPE_LABELS[name])}</strong><b>{formatCount(item?.bubble_runs)}</b></div>
        <p>{t('{{done}} 完成 · {{errors}} 错误 · {{timeouts}} 超时', {
          done: formatCount(item?.bubble_done),
          errors: formatCount(item?.bubble_errors),
          timeouts: formatCount(item?.bubble_timeouts),
        })}</p>
        <small>{t('平均 {{duration}} · {{cycles}} 轮 · {{resumes}} 次恢复', {
          duration: formatDurationSeconds(item?.avg_bubble_seconds),
          cycles: finite(item?.avg_bubble_cycles).toFixed(item?.avg_bubble_cycles ? 1 : 0),
          resumes: formatCount(item?.bubble_resumes),
        })}</small>
      </article>)}</div>
    </section>

    <footer className="usage-analytics-foot">
      <span>{t('开始追踪：{{date}}', { date: stats.tracking_since || '—' })}</span>
      <span>{t('生成时间：{{date}}', { date: generatedAt })}</span>
      <span>{t('仅保存脱敏聚合，不向前端返回日志正文')}</span>
    </footer>
  </div>;
}
