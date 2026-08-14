export type TimestampValue = Date | string | number | null | undefined;

type LocalTimestampParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  millisecond: number;
};

const NAIVE_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?$/;
const ABSOLUTE_TIMESTAMP = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const FIXED_OFFSET = /^([+-])(\d{2}):(\d{2})$/;
const timezoneFormatters = new Map<string, Intl.DateTimeFormat>();
let serverTimezone = '';

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

function fixedOffsetMilliseconds(timezone: string): number | null {
  const match = FIXED_OFFSET.exec(timezone);
  if (!match) return null;
  const hours = Number(match[2]);
  const minutes = Number(match[3]);
  if (hours > 23 || minutes > 59) return null;
  const sign = match[1] === '+' ? 1 : -1;
  return sign * (hours * 60 + minutes) * 60_000;
}

function formatterFor(timezone: string): Intl.DateTimeFormat | null {
  const cached = timezoneFormatters.get(timezone);
  if (cached) return cached;
  try {
    const formatter = new Intl.DateTimeFormat('en-CA-u-ca-iso8601-nu-latn', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    });
    formatter.format(new Date(0));
    timezoneFormatters.set(timezone, formatter);
    return formatter;
  } catch {
    return null;
  }
}

/** Set the server timezone used to interpret offset-free API timestamps. */
export function setServerTimezone(value: unknown): boolean {
  const timezone = typeof value === 'string' ? value.trim() : '';
  if (timezone && (fixedOffsetMilliseconds(timezone) != null || formatterFor(timezone))) {
    serverTimezone = timezone;
    return true;
  }
  serverTimezone = '';
  return false;
}

function parseLocalTimestamp(value: string): LocalTimestampParts | null {
  const match = NAIVE_TIMESTAMP.exec(value);
  if (!match) return null;
  const parts: LocalTimestampParts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
    second: Number(match[6] || 0),
    millisecond: Number((match[7] || '').slice(0, 3).padEnd(3, '0')),
  };
  const clock = new Date(Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  ));
  if (
    clock.getUTCFullYear() !== parts.year
    || clock.getUTCMonth() !== parts.month - 1
    || clock.getUTCDate() !== parts.day
    || clock.getUTCHours() !== parts.hour
    || clock.getUTCMinutes() !== parts.minute
    || clock.getUTCSeconds() !== parts.second
  ) return null;
  return parts;
}

function clockMilliseconds(parts: LocalTimestampParts): number {
  return Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  );
}

function zonedParts(epochMilliseconds: number, timezone: string): LocalTimestampParts | null {
  const formatter = formatterFor(timezone);
  if (!formatter) return null;
  const values: Record<string, number> = {};
  for (const part of formatter.formatToParts(new Date(epochMilliseconds))) {
    if (part.type !== 'literal') values[part.type] = Number(part.value);
  }
  if (!['year', 'month', 'day', 'hour', 'minute', 'second'].every(key => Number.isFinite(values[key]))) {
    return null;
  }
  return {
    year: values.year,
    month: values.month,
    day: values.day,
    hour: values.hour,
    minute: values.minute,
    second: values.second,
    millisecond: new Date(epochMilliseconds).getUTCMilliseconds(),
  };
}

function sameWallTime(left: LocalTimestampParts, right: LocalTimestampParts): boolean {
  return left.year === right.year
    && left.month === right.month
    && left.day === right.day
    && left.hour === right.hour
    && left.minute === right.minute
    && left.second === right.second
    && left.millisecond === right.millisecond;
}

function dateInServerTimezone(parts: LocalTimestampParts): Date | null {
  const wallClock = clockMilliseconds(parts);
  const fixedOffset = fixedOffsetMilliseconds(serverTimezone);
  if (fixedOffset != null) return new Date(wallClock - fixedOffset);
  if (!serverTimezone) return null;

  let epoch = wallClock;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const observed = zonedParts(epoch, serverTimezone);
    if (!observed) return null;
    const offset = clockMilliseconds(observed) - epoch;
    const candidate = wallClock - offset;
    if (candidate === epoch) break;
    epoch = candidate;
  }
  const resolved = zonedParts(epoch, serverTimezone);
  return resolved && sameWallTime(resolved, parts) ? new Date(epoch) : null;
}

/** Parse an API timestamp; offset-free strings belong to the configured server timezone. */
export function parseTimestamp(value: TimestampValue): Date | null {
  if (value == null || value === '') return null;
  if (value instanceof Date) {
    const copy = new Date(value.getTime());
    return Number.isFinite(copy.getTime()) ? copy : null;
  }
  if (typeof value === 'string' && !ABSOLUTE_TIMESTAMP.test(value)) {
    const parts = parseLocalTimestamp(value.trim());
    return parts ? dateInServerTimezone(parts) : null;
  }
  const date = new Date(value);
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
  const date = new Date(value.trim());
  return Number.isFinite(date.getTime()) ? date.toISOString() : '';
}

/** Normalize an offset-bearing API timestamp for URLs and requests. */
export function toAbsoluteIso(value: TimestampValue): string {
  return parseTimestamp(value)?.toISOString() ?? '';
}
