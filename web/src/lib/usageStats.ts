import type {
  UsageModelStats,
  UsageProviderModelStats,
  UsageWindowStats,
} from '../api/types';

export type UsageWindowKey = 'today' | 'last_7_days' | 'lifetime';

export const USAGE_WINDOWS: Array<{ key: UsageWindowKey; label: string }> = [
  { key: 'today', label: '今日' },
  { key: 'last_7_days', label: '7日' },
  { key: 'lifetime', label: '累计' },
];

export const USAGE_SCOPE_LABELS: Record<string, string> = {
  main: '主线',
  summary: '摘要',
  vision: '视觉',
  bubble: 'Bubble',
  subconscious: '潜意识',
  mem0: 'mem0',
  unknown: '未分类',
};

export const USAGE_SCOPE_ORDER = [
  'main',
  'summary',
  'vision',
  'bubble',
  'subconscious',
  'mem0',
];

export function formatTokenUnits(value?: number | null): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(2)}K`;
  return Math.round(n).toLocaleString();
}

export function formatCount(value?: number | null): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  return Math.round(n).toLocaleString();
}

export function formatCacheRate(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

export function clampPercent(value: number): string {
  if (!Number.isFinite(value)) return '0%';
  return `${Math.max(0, Math.min(100, value)).toFixed(1)}%`;
}

export function totalFromModelStats(stats?: UsageModelStats): number {
  return Number(stats?.total_tokens ?? 0) || 0;
}

export function usageModelLabel(
  fallback: string,
  stats: UsageModelStats | UsageProviderModelStats,
): string {
  const item = stats as UsageProviderModelStats;
  return item.provider && item.model ? `${item.provider}/${item.model}` : fallback;
}

export function usageScopeEntries(stats: UsageWindowStats): Array<[string, UsageWindowStats]> {
  const scopes = stats.by_scope || {};
  const orderedKeys = [
    ...USAGE_SCOPE_ORDER,
    ...Object.keys(scopes)
      .filter(key => !USAGE_SCOPE_ORDER.includes(key) && key !== 'unknown')
      .sort(),
  ];
  if (scopes.unknown) orderedKeys.push('unknown');

  return orderedKeys
    .filter((key, index, array) => array.indexOf(key) === index)
    .map(key => [key, scopes[key] || {}]);
}

export function usageScopeClassName(name: string): string {
  return USAGE_SCOPE_ORDER.includes(name) || name === 'unknown'
    ? `scope-${name}`
    : 'scope-unknown';
}
