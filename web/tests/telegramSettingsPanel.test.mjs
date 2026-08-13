import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  defaultTelegramDisplayName,
  generateTelegramInstanceId,
  TELEGRAM_INSTANCE_ID_PATTERN,
} from '../src/admin/settings/telegramInstanceId.ts';

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

test('prefills a valid four-character Telegram instance ID', () => {
  const generated = generateTelegramInstanceId([]);

  assert.equal(generated.length, 4);
  assert.match(generated, TELEGRAM_INSTANCE_ID_PATTERN);
  assert.match(panel, /useState\(\s*\(\) => generateTelegramInstanceId/);
  assert.match(panel, /onChange=\{event => setInstanceId\(event\.target\.value\)\}/);
});

test('avoids configured Telegram instance IDs even after random collisions', () => {
  assert.equal(generateTelegramInstanceId(['aaaa'], () => 0), 'aaab');
});

test('derives an editable default display name from the instance ID', () => {
  assert.equal(defaultTelegramDisplayName('k7m2'), 'Telegram k7m2');
  assert.match(panel, /display_name: defaultTelegramDisplayName\(normalizedId\)/);
});
