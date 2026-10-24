import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const styles = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const i18n = await readFile(new URL('../src/i18n/catalog.ts', import.meta.url), 'utf8');
const configPresentation = await readFile(
  new URL('../src/admin/settings/configFieldPresentation.ts', import.meta.url),
  'utf8',
);

test('keeps live status actions and long model names readable', () => {
  assert.match(adminApp, /<span className="overview-name"/);
  assert.match(adminApp, /className="overview-model-value"/);
  assert.match(styles, /\.overview-status-actions button \{[^}]*white-space:\s*nowrap;/);
  assert.match(
    styles,
    /@media \(max-width: 620px\) \{\s*\.overview-status-strip \{ grid-template-columns: minmax\(0, 1fr\); \}/,
  );
});

test('keeps usage window labels and responsibility filters readable', () => {
  assert.match(styles, /\.usage-analytics-windows button \{[^}]*white-space:\s*nowrap;/);
  assert.match(styles, /\.usage-scope-filter > div \{ flex-wrap: wrap; overflow: visible; \}/);
  assert.match(styles, /\.usage-analytics-metrics \{ grid-template-columns: repeat\(2, minmax\(0, 1fr\)\); \}/);
});

test('floats the settings save bar only while the current group is dirty', () => {
  assert.match(
    styles,
    /\.config-panel:has\(> \.panel-actions \.save-state\.dirty\) > \.panel-actions \{\s*position: sticky;/,
  );
  assert.match(styles, /\.config-panel > \.panel-actions \{\s*position: static;/);
});

test('keeps compact tabs on one row instead of wrapping the last tab', () => {
  assert.match(
    styles,
    /\.tabbar:not\(\.compact\) \{\s*display: grid;\s*grid-template-columns: none;\s*grid-auto-flow: column;/,
  );
  assert.match(styles, /\.memory-tabs \{ grid-template-columns: repeat\(3, minmax\(0, 1fr\)\) !important; \}/);
});

test('uses a dedicated release empty modifier instead of the shared empty state', () => {
  assert.match(adminApp, /latest \? 'ready' : 'is-empty'/);
  assert.match(styles, /\.release-hero\.is-empty \{ border-style: dashed; \}/);
  assert.doesNotMatch(styles, /\.release-hero\.empty \{/);
});

test('identity saving is guarded by dirty state and reports failures', () => {
  assert.match(adminApp, /disabled=\{saving \|\| !identityDirty\}/);
  assert.ok(adminApp.includes("setSaveError(error instanceof Error ? error.message : t('保存失败'))"));
});

test('localizes every runtime settings field', () => {
  assert.match(adminApp, /'memory\.backend': '记忆后端'/);
  assert.match(adminApp, /'memory\.summary_budget_retries': '摘要预算重试次数'/);
  assert.match(adminApp, /'memory\.summary_budget_tolerance': '摘要预算容差'/);
  assert.match(adminApp, /'api\.compat_tool_result_fold_chars': '工具结果折叠阈值（字符）'/);
  assert.match(i18n, /'记忆后端': 'Memory backend'/);
  assert.match(i18n, /'记忆抽取 Provider（mem0）': 'Memory extraction Provider \(mem0\)'/);
});

test('gives memory backend and summary budgets structured controls', () => {
  assert.match(configPresentation, /path === 'memory\.backend'[\s\S]*?editor: 'choice'/);
  assert.match(configPresentation, /'memory\.summary_budget_retries'/);
  assert.match(configPresentation, /'memory\.summary_budget_tolerance'/);
  assert.match(configPresentation, /'api\.compat_tool_result_fold_chars'/);
});

test('keeps settings actions and single settings full-width on phones', () => {
  assert.match(styles, /\.config-panel > \.panel-actions \{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; \}/);
  assert.match(styles, /\.config-fields > \.field:nth-child\(2\):last-child \{ grid-column: 1 \/ -1; width: min\(100%, 560px\); \}/);
});
