import assert from 'node:assert/strict';
import test from 'node:test';

import { withDetectedTimezone } from '../src/admin/bootstrapTimezone.ts';

test('fills an empty setup timezone from the browser', () => {
  const defaults = { i18n: { locale: 'zh-CN', timezone: '' } };
  const result = withDetectedTimezone(defaults, 'Asia/Shanghai');

  assert.equal(result.i18n.timezone, 'Asia/Shanghai');
  assert.equal(defaults.i18n.timezone, '');
});

test('preserves an explicitly configured setup timezone', () => {
  const defaults = { i18n: { locale: 'en', timezone: 'America/New_York' } };
  const result = withDetectedTimezone(defaults, 'Asia/Shanghai');

  assert.equal(result.i18n.timezone, 'America/New_York');
});

test('leaves host-timezone fallback intact when detection is unavailable', () => {
  const defaults = { i18n: { locale: 'en', timezone: '' } };
  const result = withDetectedTimezone(defaults, '');

  assert.equal(result.i18n.timezone, '');
});
