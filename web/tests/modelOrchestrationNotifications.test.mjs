import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('model orchestration reports switch, save, and catalog refresh outcomes', () => {
  assert.match(adminApp, /next\.active_changed/);
  assert.match(adminApp, /主线模型已切换至 \{\{provider\}\}\/\{\{model\}\}/);
  assert.match(adminApp, /主线模型未变化/);
  assert.match(adminApp, /模型编排已保存并热更新/);
  assert.match(adminApp, /failedProviders\.length/);
  assert.match(adminApp, /notice success/);
});

test('all model orchestration model fields use provider catalog comboboxes', () => {
  assert.match(adminApp, /const summaryModelOptions = modelOptionsFor\(draft\.summary\.provider \|\| draft\.active\.provider\)/);
  assert.match(adminApp, /const visionModelOptions = modelOptionsFor\(draft\.vision\.provider\)/);
  assert.match(adminApp, /const mem0ModelOptions = modelOptionsFor\(draft\.mem0\.provider \|\| draft\.active\.provider\)/);
  assert.match(adminApp, /<EditableCombobox id="summary-model-input"[^>]+options=\{summaryModelOptions\}/);
  assert.match(adminApp, /<EditableCombobox id="vision-model-input"[^>]+options=\{visionModelOptions\}/);
  assert.match(adminApp, /<EditableCombobox id="mem0-model-input"[^>]+options=\{mem0ModelOptions\}/);
});
