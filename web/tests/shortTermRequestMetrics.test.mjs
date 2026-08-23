import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('assistant messages show cached tokens and request duration', () => {
  assert.match(adminApp, /message\.usage\.cached_tokens/);
  assert.match(adminApp, /formatRequestDuration\(message\.duration_ms\)/);
  assert.match(adminApp, /duration_ms: event\.duration_ms/);
  assert.match(adminApp, /输入 \{\{input\}\} \/ 输出 \{\{output\}\} \/ 缓存 \{\{cached\}\} token/);
  assert.match(adminApp, /耗时 \{\{duration\}\}/);
});
