export type ModelPriceValidationError =
  | 'identity'
  | 'currency'
  | 'rates'
  | 'cached_rate'
  | 'duplicate';

type ModelPriceDraft = Record<string, unknown>;

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
