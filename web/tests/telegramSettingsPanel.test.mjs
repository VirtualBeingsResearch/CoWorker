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
  assert.match(styles, /\.telegram-settings\s*\{[^}]*margin-top:\s*14px;/);
});

test('explains when local Bot API Server mode is appropriate', () => {
  assert.match(panel, /自托管机器人 API 服务器/);
  assert.doesNotMatch(panel, /Bot API/);
  assert.match(panel, /--local/);
  assert.match(panel, /共享文件路径/);
});
