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

test('model routing separates preference from last execution and saves recovery settings', () => {
  assert.match(adminApp, /首选模型/);
  assert.match(adminApp, /draft\.last_used\.provider/);
  assert.match(adminApp, /routing: draft\.routing/);
  assert.match(adminApp, /draft\.health\.map/);
  assert.match(adminApp, /recovery_successes/);
  assert.match(adminApp, /冷却结束后按需验证恢复/);
});

test('unconfirmed legacy candidates require an explicit main-model switch', () => {
  assert.match(adminApp, /draft\.preference\?\.status === 'pending'/);
  assert.match(adminApp, /历史候选（尚未启用）/);
  assert.match(adminApp, /旧配置缺少可确认的首选，模型调用已暂停/);
  assert.match(adminApp, /next\.restart_required/);
  assert.match(adminApp, /首选已保存，正在重启恢复模型服务/);
});
