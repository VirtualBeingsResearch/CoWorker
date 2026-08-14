import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  costEntries,
  formatCostSummary,
  formatCurrencyAmount,
  usageCost,
} from '../src/lib/usageStats.ts';
import {
  COMMON_MODEL_PRICE_CURRENCIES,
  modelPriceCurrencyLabel,
  validateModelPrices,
} from '../src/admin/settings/modelPricing.ts';
import { filterEditableComboboxOptions } from '../src/admin/comboboxOptions.ts';

const analytics = await readFile(new URL('../src/admin/UsageAnalytics.tsx', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const editableCombobox = await readFile(new URL('../src/admin/EditableCombobox.tsx', import.meta.url), 'utf8');
const overview = await readFile(new URL('../src/admin/UsageOverview.tsx', import.meta.url), 'utf8');
const costSummary = await readFile(new URL('../src/admin/CurrencyCostSummary.tsx', import.meta.url), 'utf8');
const adminCss = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');

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

test('suggests common localized currencies while retaining custom codes', () => {
  assert.deepEqual(COMMON_MODEL_PRICE_CURRENCIES.slice(0, 6), [
    'CNY',
    'USD',
    'EUR',
    'JPY',
    'HKD',
    'GBP',
  ]);
  assert.ok(COMMON_MODEL_PRICE_CURRENCIES.every(currency => /^[A-Z]{3}$/.test(currency)));
  assert.match(modelPriceCurrencyLabel('USD', 'en'), /^USD · /);
  const options = COMMON_MODEL_PRICE_CURRENCIES.map(value => ({ value }));
  assert.equal(filterEditableComboboxOptions(options, null).length, COMMON_MODEL_PRICE_CURRENCIES.length);
  assert.deepEqual(filterEditableComboboxOptions(options, 'usd').map(option => option.value), ['USD']);
  assert.match(adminApp, /<EditableCombobox[\s\S]*model-price-currency-/);
  assert.match(adminApp, /<EditableCombobox[\s\S]*model-price-provider-/);
  assert.match(adminApp, /<ProviderModelField id="bootstrap-model-input"/);
  assert.match(adminApp, /'\/api\/admin\/provider-models'/);
  assert.match(adminApp, /provider_name: providerName/);
  assert.match(adminApp, /className="ghost provider-model-discover"/);
  assert.match(adminApp, /const THINKING_EFFORT_OPTIONS = \[/);
  assert.match(adminApp, /thinking: draft\.thinking/);
  assert.match(adminApp, /<ThinkingEffortField value=\{draft\.thinking\}/);
  assert.match(editableCombobox, /const openAll = \(\) =>/);
  assert.match(editableCombobox, /setQuery\(null\)/);
  assert.match(editableCombobox, /setHighlightedIndex\(selectedIndex\)/);
  assert.match(editableCombobox, /setHighlightedIndex\(-1\)/);
  assert.match(editableCombobox, /role="combobox"/);
  assert.match(editableCombobox, /role="listbox"/);
  assert.match(adminCss, /\.editable-combobox > input \{[^}]*width: 100%/);
  assert.doesNotMatch(adminApp, /provider-price-card-heading/);
  assert.match(adminApp, /provider-price-remove/);
  assert.match(adminApp, /可选择常用币种，也可输入其他三字母代码/);
});

test('keeps multi-currency spend compact and links summaries to pricing', () => {
  assert.match(costSummary, /entries\.slice\(0, collapsedLimit\)/);
  assert.match(costSummary, /aria-expanded=\{expanded\}/);
  assert.match(overview, /CurrencyCostSummary costs=\{windowStats\.estimated_costs\}/);
  assert.match(overview, /className="usage-pricing-shortcut" href=\{pricingHref\}/);
  assert.match(analytics, /featured actionHref=\{pricingHref\}/);
  assert.match(adminApp, /function sectionHref[\s\S]*url\.hash = '';/);
  assert.match(adminApp, /url\.hash = 'model-pricing'/);
  assert.match(adminApp, /scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\)/);
  assert.match(adminCss, /usage-analytics-metrics \{[^}]*repeat\(6/);
  assert.match(adminCss, /admin-usage-metrics\.compact \{[^}]*repeat\(6/);
});
