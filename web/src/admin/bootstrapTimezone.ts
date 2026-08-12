export function detectBrowserTimezone(): string {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof timezone === 'string' ? timezone.trim() : '';
  } catch {
    return '';
  }
}

export type BootstrapTimezoneAdvice = {
  available: boolean;
  detectedTimezone: string;
  recommendation: string;
};

export function bootstrapTimezoneAdvice(
  detectedTimezone = detectBrowserTimezone(),
): BootstrapTimezoneAdvice {
  const detected = detectedTimezone.trim();
  return {
    available: Boolean(detected),
    detectedTimezone: detected,
    recommendation: detected ? `TZ=${detected}` : '',
  };
}
