import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { bootstrapTimezoneAdvice } from '../src/admin/bootstrapTimezone.ts';

const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const adminCss = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');

test('recommends TZ without creating a runtime timezone setting', () => {
  assert.deepEqual(bootstrapTimezoneAdvice('Asia/Shanghai'), {
    available: true,
    detectedTimezone: 'Asia/Shanghai',
    recommendation: 'TZ=Asia/Shanghai',
  });
});

test('does not recommend an environment override when detection is unavailable', () => {
  assert.deepEqual(bootstrapTimezoneAdvice(''), {
    available: false,
    detectedTimezone: '',
    recommendation: '',
  });
});

test('keeps browser timezone detection outside bootstrap configuration', () => {
  assert.match(adminApp, /configurationBaseline\] = useState<Json>\(\(\) => structuredClone\(configurationDefaults\)\)/);
  assert.match(adminApp, /data\.server_timezone/);
  assert.doesNotMatch(adminApp, /changeConfiguration\('i18n', 'timezone'/);
  assert.doesNotMatch(adminApp, /configuration\.i18n\?\.timezone/);
});

test('renders name examples as non-interactive illustrations', () => {
  assert.match(adminApp, /nameExamples\.map\(example => <span/);
  assert.doesNotMatch(adminApp, /nameExamples\.map\(example => <button/);
});

test('keeps the timezone recommendation visible when translated text wraps', () => {
  assert.match(adminApp, /<span><span>\{t\('仅提醒 · 建议'\)\}<\/span><code>\{timezoneAdvice\.recommendation\}<\/code><\/span>/);
  assert.match(adminCss, /\.bootstrap-timezone-guidance > span \{[^}]*flex-wrap: wrap;/);
  assert.match(adminCss, /\.bootstrap-timezone-guidance code \{[^}]*white-space: nowrap;/);
  assert.doesNotMatch(adminCss, /\.bootstrap-timezone-guidance > span \{[^}]*text-overflow: ellipsis;/);
});

test('keeps the translated active-mode recommendation readable', () => {
  assert.match(adminCss, /\.bootstrap-mode-inline > span b \{[^}]*text-wrap: balance;[^}]*white-space: normal;/);
  assert.doesNotMatch(adminCss, /\.bootstrap-mode-inline > span b \{[^}]*text-overflow: ellipsis;/);
});
