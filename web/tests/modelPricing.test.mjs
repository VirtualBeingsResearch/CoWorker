import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  costEntries,
  formatCostSummary,
  formatCurrencyAmount,
  usageCost,
} from '../src/lib/usageStats.ts';
import { validateModelPrices } from '../src/admin/settings/modelPricing.ts';

const analytics = await readFile(new URL('../src/admin/UsageAnalytics.tsx', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('formats and orders independent currency estimates', () => {
  assert.deepEqual(costEntries({ USD: 1.25, CNY: 8.5, invalid: Number.NaN }), [
    ['CNY', 8.5],
    ['USD', 1.25],
  ]);
  assert.match(formatCurrencyAmount('USD', 1.25, 'en'), /\$1\.25/);
  assert.match(formatCostSummary({ USD: 1.25, CNY: 8.5 }, 'en'), /CN¥8\.50.*\$1\.25/);
  assert.equal(usageCost({ estimated_costs: { USD: 0.375 } }, 'USD'), 0.375);
  assert.equal(usageCost({ estimated_costs: { USD: 0.375 } }, 'CNY'), 0);
});

test('offers token and currency trends with matching period comparisons', () => {
  assert.match(analytics, /type UsageTrendMetric = 'tokens' \| `currency:\$\{string\}`/);
  assert.match(analytics, /<TrendMetricToggle currencies=\{currencies\} metric=\{activeTrendMetric\}/);
  assert.match(analytics, /const currentTrendValue = trendValue\(windowStats, activeTrendMetric\)/);
  assert.match(analytics, /const previousTrendValue = previousStats \? trendValue\(previousStats, activeTrendMetric\)/);
});

test('exports dynamic currency columns and explicit pricing coverage', () => {
  assert.match(analytics, /'priced_tokens',[\s\S]*'unpriced_tokens',[\s\S]*'pricing_coverage'/);
  assert.match(analytics, /costEntries\(windowStats\?\.estimated_costs\)/);
  assert.match(analytics, /estimated_cost_\$\{currency\.toLowerCase\(\)\}/);
  assert.match(analytics, /item\.estimated_costs\?\.\[currency\] \?\? ''/);
});

test('validates price identity, currency, rates, and duplicate pairs before saving', () => {
  const price = {
    provider: 'openai',
    model: 'gpt-5.2',
    currency: 'usd',
    input_per_million: 1.75,
    output_per_million: 14,
    cached_input_per_million: null,
  };
  assert.equal(validateModelPrices([price]), null);
  assert.equal(validateModelPrices([{ ...price, provider: ' ' }]), 'identity');
  assert.equal(validateModelPrices([{ ...price, currency: 'US' }]), 'currency');
  assert.equal(validateModelPrices([{ ...price, input_per_million: -1 }]), 'rates');
  assert.equal(validateModelPrices([{ ...price, output_per_million: Number.POSITIVE_INFINITY }]), 'rates');
  assert.equal(validateModelPrices([{ ...price, cached_input_per_million: -0.1 }]), 'cached_rate');
  assert.equal(validateModelPrices([price, { ...price }]), 'duplicate');
  assert.match(adminApp, /const validationError = validateModelPrices\(value\)/);
  assert.match(adminApp, /validationMessage && <p className="field-error" role="alert">/);
});
