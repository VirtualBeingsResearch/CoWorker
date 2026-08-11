import type { Json } from './settings/types';

export function detectBrowserTimezone(): string {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof timezone === 'string' ? timezone.trim() : '';
  } catch {
    return '';
  }
}

export function withDetectedTimezone(
  defaults: Json,
  detectedTimezone = detectBrowserTimezone(),
): Json {
  const configuration = structuredClone(defaults);
  const current = String(configuration.i18n?.timezone || '').trim();
  const detected = detectedTimezone.trim();
  if (!current && detected) {
    configuration.i18n = { ...(configuration.i18n || {}), timezone: detected };
  }
  return configuration;
}
