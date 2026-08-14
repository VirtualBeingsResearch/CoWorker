export type ModelPriceValidationError =
  | 'identity'
  | 'currency'
  | 'rates'
  | 'cached_rate'
  | 'duplicate';

type ModelPriceDraft = Record<string, unknown>;

export const COMMON_MODEL_PRICE_CURRENCIES = [
  'CNY',
  'USD',
  'EUR',
  'JPY',
  'HKD',
  'GBP',
  'TWD',
  'KRW',
  'SGD',
  'AUD',
  'CAD',
  'CHF',
  'INR',
] as const;

export function modelPriceCurrencyLabel(
  currency: string,
  language: 'zh' | 'en' = 'zh',
): string {
  const { code, displayName, symbol } = modelPriceCurrencyDetails(currency, language);
  return [code, displayName, symbol]
    .filter((value, index, values) => value && values.indexOf(value) === index)
    .join(' · ');
}

export function modelPriceCurrencyDetails(
  currency: string,
  language: 'zh' | 'en' = 'zh',
): { code: string; displayName: string; symbol: string } {
  const code = currency.trim().toUpperCase();
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  let displayName = code;
  let symbol = code;
  try {
    displayName = new Intl.DisplayNames([locale], { type: 'currency' }).of(code) || code;
  } catch { /* Fall back to the currency code. */ }
  try {
    symbol = new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: code,
      currencyDisplay: 'symbol',
    }).formatToParts(0).find(part => part.type === 'currency')?.value || code;
  } catch { /* Fall back to the currency code. */ }
  return { code, displayName, symbol };
}

export function validateModelPrices(value: unknown): ModelPriceValidationError | null {
  const prices = Array.isArray(value) ? value : [];
  const keys: string[] = [];
  for (const item of prices) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return 'identity';
    const price = item as ModelPriceDraft;
    const provider = String(price.provider || '').trim();
    const model = String(price.model || '').trim();
    const currency = String(price.currency || '').trim().toUpperCase();
    if (!provider || !model) return 'identity';
    if (!/^[A-Z]{3}$/.test(currency)) return 'currency';
    for (const key of ['input_per_million', 'output_per_million']) {
      if (!isNonNegativeNumber(price[key])) return 'rates';
    }
    const cachedRate = price.cached_input_per_million;
    if (cachedRate !== null
      && cachedRate !== ''
      && cachedRate !== undefined
      && !isNonNegativeNumber(cachedRate)) return 'cached_rate';
    keys.push(`${provider}\u0000${model}`);
  }
  if (new Set(keys).size !== keys.length) return 'duplicate';
  return null;
}

function isNonNegativeNumber(value: unknown) {
  return value !== ''
    && value !== null
    && value !== undefined
    && Number.isFinite(Number(value))
    && Number(value) >= 0;
}
