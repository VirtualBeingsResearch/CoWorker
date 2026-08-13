export type TimestampValue = Date | string | number | null | undefined;

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

/** Parse an absolute API timestamp or a browser-local datetime input value. */
export function parseTimestamp(value: TimestampValue): Date | null {
  if (value == null || value === '') return null;
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}

export function timestampMillis(value: TimestampValue): number | null {
  return parseTimestamp(value)?.getTime() ?? null;
}

export function formatDateTime(
  value: TimestampValue,
  locales?: Intl.LocalesArgument,
  options?: Intl.DateTimeFormatOptions,
): string {
  const date = parseTimestamp(value);
  if (!date) return typeof value === 'string' && value ? value : '—';
  return date.toLocaleString(locales, options);
}

export function formatDate(
  value: TimestampValue,
  locales?: Intl.LocalesArgument,
  options?: Intl.DateTimeFormatOptions,
): string {
  const date = parseTimestamp(value);
  if (!date) return typeof value === 'string' && value ? value : '—';
  return date.toLocaleDateString(locales, options);
}

export function formatTime(
  value: TimestampValue,
  locales?: Intl.LocalesArgument,
  options?: Intl.DateTimeFormatOptions,
): string {
  const date = parseTimestamp(value);
  if (!date) return typeof value === 'string' && value ? value : '—';
  return date.toLocaleTimeString(locales, options);
}

/** Return YYYY-MM-DD using the browser's current time zone. */
export function localDateKey(value: TimestampValue = new Date()): string {
  const date = parseTimestamp(value);
  if (!date) return '';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Convert an absolute timestamp to the value expected by datetime-local. */
export function toLocalDateTimeInput(
  value: TimestampValue,
  precision: 'minute' | 'millisecond' = 'millisecond',
): string {
  const date = parseTimestamp(value);
  if (!date) return '';
  const base = `${localDateKey(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (precision === 'minute') return base;
  return `${base}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

/** Convert a datetime-local control value into an absolute RFC 3339 instant. */
export function localDateTimeInputToIso(value: string): string {
  return parseTimestamp(value.trim())?.toISOString() ?? '';
}

/** Normalize an offset-bearing API timestamp for URLs and requests. */
export function toAbsoluteIso(value: TimestampValue): string {
  return parseTimestamp(value)?.toISOString() ?? '';
}
