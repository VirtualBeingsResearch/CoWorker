import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatTime,
  localDateKey,
  localDateTimeInputToIso,
  parseTimestamp,
  toAbsoluteIso,
  toLocalDateTimeInput,
} from '../src/lib/dateTime.ts';

const originalTimezone = process.env.TZ;

test.after(() => {
  if (originalTimezone === undefined) delete process.env.TZ;
  else process.env.TZ = originalTimezone;
});

test('converts absolute timestamps to the browser current time zone', () => {
  process.env.TZ = 'Asia/Shanghai';

  assert.equal(
    toLocalDateTimeInput('2026-08-13T00:30:45.123Z'),
    '2026-08-13T08:30:45.123',
  );
  assert.equal(
    formatTime('2026-08-13T00:30:45.123Z', 'en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }),
    '08:30',
  );
  assert.equal(localDateKey('2026-08-13T18:30:00Z'), '2026-08-14');

  process.env.TZ = 'America/Los_Angeles';
  assert.equal(
    toLocalDateTimeInput('2026-08-13T00:30:45.123Z'),
    '2026-08-12T17:30:45.123',
  );
  assert.equal(
    formatTime('2026-08-13T00:30:45.123Z', 'en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }),
    '17:30',
  );
  assert.equal(localDateKey('2026-08-13T18:30:00Z'), '2026-08-13');
});

test('turns datetime-local values into absolute instants', () => {
  process.env.TZ = 'Asia/Shanghai';

  assert.equal(
    localDateTimeInputToIso('2026-08-13T08:30'),
    '2026-08-13T00:30:00.000Z',
  );
  assert.equal(
    toAbsoluteIso('2026-08-13T08:30:00+08:00'),
    '2026-08-13T00:30:00.000Z',
  );
});

test('rejects empty and invalid timestamp values', () => {
  assert.equal(parseTimestamp('not-a-timestamp'), null);
  assert.equal(toAbsoluteIso(''), '');
  assert.equal(toLocalDateTimeInput(undefined), '');
});
