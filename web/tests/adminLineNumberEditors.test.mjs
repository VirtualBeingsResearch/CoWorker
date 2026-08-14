import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const component = await readFile(new URL('../src/admin/LineNumberTextarea.tsx', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const adminCss = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');
const channelAccess = await readFile(new URL('../src/admin/settings/panels/ChannelAccessSettingsPanel.tsx', import.meta.url), 'utf8');

test('the shared line editor marks blank lines and keeps its gutter synchronized', () => {
  assert.match(component, /text \? lines\.filter\(line => !line\.trim\(\)\)\.length : 0/);
  assert.match(component, /className=\{!line\.trim\(\) \? 'blank' : ''\}/);
  assert.match(component, /gutter\.current\.scrollTop = event\.currentTarget\.scrollTop/);
  assert.match(component, /\{\{lines\}\} 行 · \{\{blank\}\} 个空白行/);
});

test('the shared line editor keeps its gutter compact', () => {
  assert.match(adminCss, /\.line-number-editor \{[^}]*grid-template-columns: auto minmax\(0, 1fr\);/s);
  assert.match(adminCss, /\.line-number-gutter \{[^}]*min-width: 20px;[^}]*padding: var\(--line-number-padding-y\) 4px;/s);
});

test('line-oriented administration fields use the shared editor', () => {
  assert.match(adminApp, /<LineNumberTextarea className="code-area short"/);
  assert.match(adminApp, /setFallbackText\(e\.target\.value\)/);
  assert.match(adminApp, /<LineNumberTextarea className=\{`code-area compact/);
  assert.match(adminApp, /wrapperClassName="system-prompt-line-number-field"/);
  assert.match(adminApp, /个性化备注（每行一条）'\)}><LineNumberTextarea/);
  assert.match(adminApp, /wrapperClassName="source-line-number-field"/);
  assert.match(adminApp, /\{\{count\}\} 个空白行/);
  assert.match(channelAccess, /<LineNumberTextarea aria-label=\{t\('批量添加规则'\)\}/);
});
