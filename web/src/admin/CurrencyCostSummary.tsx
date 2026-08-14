import { useState } from 'react';

import type { AdminLanguage } from '../i18n/admin';
import { t } from '../i18n/admin';
import { costEntries, formatCostSummary, formatCurrencyAmount } from '../lib/usageStats';

export function CurrencyCostSummary({
  costs,
  language,
  limit = 2,
}: {
  costs?: Record<string, number> | null;
  language: AdminLanguage;
  limit?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const entries = costEntries(costs);
  if (!entries.length) return <span className="currency-cost-summary empty">—</span>;

  const collapsedLimit = Math.max(1, limit);
  const visibleEntries = expanded ? entries : entries.slice(0, collapsedLimit);
  const hiddenCount = Math.max(0, entries.length - collapsedLimit);
  const fullSummary = formatCostSummary(costs, language);

  return <span className="currency-cost-summary" title={fullSummary} aria-label={fullSummary}>
    {visibleEntries.map(([currency, amount]) => <span key={currency}>
      {formatCurrencyAmount(currency, amount, language)}
    </span>)}
    {hiddenCount > 0 && <button
      type="button"
      aria-expanded={expanded}
      aria-label={expanded ? t('收起') : t('显示全部 {{count}} 种币种', { count: entries.length })}
      onClick={() => setExpanded(current => !current)}
    >{expanded ? t('收起') : `+${hiddenCount}`}</button>}
  </span>;
}
