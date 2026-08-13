import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const styles = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');
const panel = await readFile(
  new URL('../src/admin/settings/panels/TelegramSettingsPanel.tsx', import.meta.url),
  'utf8',
);

test('uses the shared admin input style for Telegram fields', () => {
  assert.match(styles, /\.admin-input,\s*\n\.field input,/);
  assert.match(styles, /\.admin-input:focus,\s*\n\.field/);
  assert.equal(panel.match(/className="admin-input"/g)?.length, 5);
});

test('explains when local Bot API Server mode is appropriate', () => {
  assert.match(panel, /自托管 Bot API Server/);
  assert.match(panel, /使用官方 API 时保持关闭/);
});
