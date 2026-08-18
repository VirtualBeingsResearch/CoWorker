import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatTime,
  localDateKey,
  localDateTimeInputToIso,
  parseTimestamp,
  pastedLogTimeToInput,
  setServerTimezone,
  toAbsoluteIso,
  toLocalDateTimeInput,
} from '../src/lib/dateTime.ts';

const originalTimezone = process.env.TZ;

test.after(() => {
  setServerTimezone('');
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

test('interprets naive API timestamps in the configured server timezone', () => {
  process.env.TZ = 'America/Los_Angeles';
  assert.equal(setServerTimezone('Asia/Shanghai'), true);

  assert.equal(
    toLocalDateTimeInput('2026-08-13T08:30:45.123'),
    '2026-08-12T17:30:45.123',
  );
  assert.equal(
    toAbsoluteIso('2026-08-13T08:30:45.123'),
    '2026-08-13T00:30:45.123Z',
  );
});

test('uses the server timezone rules for the timestamp date', () => {
  assert.equal(setServerTimezone('America/New_York'), true);

  assert.equal(toAbsoluteIso('2026-01-15T12:00:00'), '2026-01-15T17:00:00.000Z');
  assert.equal(toAbsoluteIso('2026-07-15T12:00:00'), '2026-07-15T16:00:00.000Z');
  assert.equal(toAbsoluteIso('2026-11-01T01:30:00'), '2026-11-01T05:30:00.000Z');
  assert.equal(toAbsoluteIso('2026-03-08T02:30:00'), '');
});

test('supports a fixed server offset when an IANA timezone is unavailable', () => {
  assert.equal(setServerTimezone('+05:30'), true);
  assert.equal(toAbsoluteIso('2026-08-13T08:30:00'), '2026-08-13T03:00:00.000Z');
});

test('turns datetime-local values into absolute instants', () => {
  process.env.TZ = 'Asia/Shanghai';
  setServerTimezone('America/New_York');

  assert.equal(
    localDateTimeInputToIso('2026-08-13T08:30'),
    '2026-08-13T00:30:00.000Z',
  );
  assert.equal(
    toAbsoluteIso('2026-08-13T08:30:00+08:00'),
    '2026-08-13T00:30:00.000Z',
  );
});

test('normalizes copied log timestamps for history filters', () => {
  process.env.TZ = 'Asia/Shanghai';
  setServerTimezone('Asia/Shanghai');

  assert.equal(
    pastedLogTimeToInput('2026-08-18T14:32:10.123456', 'start'),
    '2026-08-18T14:32:10.123',
  );
  assert.equal(
    pastedLogTimeToInput('2026/08/18 14:32:10', 'start'),
    '2026-08-18T14:32:10.000',
  );
  assert.equal(
    pastedLogTimeToInput('2026/8/18 14:32:10', 'start'),
    '2026-08-18T14:32:10.000',
  );
  assert.equal(
    pastedLogTimeToInput('2026年8月18日 14:32:10', 'start'),
    '2026-08-18T14:32:10.000',
  );
  assert.equal(
    pastedLogTimeToInput('2026-08-18', 'start'),
    '2026-08-18T00:00:00.000',
  );
  assert.equal(
    pastedLogTimeToInput('2026-08-18', 'end'),
    '2026-08-18T23:59:59.999',
  );
  assert.equal(pastedLogTimeToInput('not-a-log-time', 'start'), '');
});

test('rejects empty and invalid timestamp values', () => {
  assert.equal(setServerTimezone('Mars/Olympus'), false);
  assert.equal(parseTimestamp('not-a-timestamp'), null);
  assert.equal(parseTimestamp('2026-08-13T08:30:00'), null);
  assert.equal(toAbsoluteIso(''), '');
  assert.equal(toLocalDateTimeInput(undefined), '');
});
