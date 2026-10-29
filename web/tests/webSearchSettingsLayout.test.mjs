import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const panel = await readFile(
  new URL('../src/admin/settings/panels/WebSearchSettingsPanel.tsx', import.meta.url),
  'utf8',
);
const searchBackends = await readFile(
  new URL('../src/admin/settings/searchBackends.tsx', import.meta.url),
  'utf8',
);
const registry = await readFile(
  new URL('../src/admin/settings/registry.ts', import.meta.url),
  'utf8',
);
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const styles = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');
const catalog = await readFile(new URL('../src/i18n/catalog.ts', import.meta.url), 'utf8');

const WEB_SEARCH_FIELDS = [
  'strategy',
  'provider',
  'timeout_seconds',
  'ddgs_backend',
  'bocha_api_key',
  'zhipu_api_key',
  'zhipu_use_llm_key',
  'zhipu_api_base',
  'zhipu_search_engine',
  'zhipu_content_size',
  'zhipu_prime_api_key',
  'zhipu_prime_use_llm_key',
  'zhipu_prime_api_base',
  'zhipu_prime_content_size',
  'zhipu_prime_location',
  'deepseek_api_key',
  'deepseek_use_llm_key',
  'deepseek_api_base',
  'qianfan_api_key',
  'qianfan_api_base',
  'linkai_api_key',
  'linkai_api_base',
  'anysearch_api_key',
  'anysearch_anonymous',
  'anysearch_zone',
  'anysearch_language',
  'serply_api_key',
  'tavily_api_key',
  'tavily_search_depth',
  'searxng_url',
  'searxng_language',
  'searxng_categories',
  'keenable_api_key',
  'keenable_anonymous',
];

const WEB_SEARCH_BACKENDS = [
  'bocha',
  'qianfan',
  'zhipu',
  'zhipu_prime',
  'deepseek',
  'linkai',
  'anysearch',
  'serply',
  'tavily',
  'searxng',
  'keenable',
  'ddgs',
];

test('registers the dedicated web search settings panel', () => {
  assert.match(registry, /web_search:\s*\{/);
  assert.match(registry, /component:\s*WebSearchSettingsPanel/);
});

test('renders every web search field inside the structured panel', () => {
  for (const field of WEB_SEARCH_FIELDS) {
    assert.ok(panel.includes(`'${field}'`), `missing web search field: ${field}`);
  }
  assert.ok(searchBackends.includes("'provider_order'"));
  assert.ok(searchBackends.includes("'disabled_providers'"));
});

test('covers every search backend with a credential card', () => {
  for (const backend of WEB_SEARCH_BACKENDS) {
    assert.ok(panel.includes(`${backend}: {`), `missing provider details: ${backend}`);
    assert.ok(searchBackends.includes(`id: '${backend}'`), `missing backend entry: ${backend}`);
  }
});

test('separates strategy, priority, and credentials into distinct sections', () => {
  assert.match(panel, /className="web-search-hero"/);
  assert.match(panel, /className="web-search-section"/);
  assert.match(panel, /className="web-search-policy-grid"/);
  assert.match(panel, /className="web-search-provider-list"/);
  assert.match(panel, /className="web-search-order-summary"/);
  assert.match(panel, /<SearchBackendOrderEditor value=\{value\} onChange=\{change\} heading=\{false\} \/>/);
  assert.match(panel, /web-search-provider-state/);
  assert.match(panel, /web-search-provider-active/);
});

test('keeps the panel responsive and readable', () => {
  assert.match(styles, /\.web-search-hero \{/);
  assert.match(styles, /\.web-search-section \{/);
  assert.match(styles, /\.web-search-provider-list \{/);
  assert.match(styles, /\.web-search-provider\.is-active \{/);
  assert.match(styles, /\.web-search-provider-state\.ok \{/);
  assert.match(
    styles,
    /@media \(max-width: 980px\) \{[\s\S]*?\.web-search-provider-list \{ grid-template-columns: 1fr; \}/,
  );
  assert.match(
    styles,
    /@media \(max-width: 560px\) \{[\s\S]*?\.web-search-provider-fields \{ grid-template-columns: 1fr; \}/,
  );
});

test('moves shared config labels out of the monolithic admin app', () => {
  assert.doesNotMatch(adminApp, /const CONFIG_LABELS: Record<string, string>/);
  assert.match(adminApp, /from '\.\/settings\/configLabels'/);
});

test('localizes the new web search layout copy', () => {
  for (const phrase of [
    '搜索策略与后端凭据',
    '当前生效',
    '已配置后端',
    '没有可用后端',
    '搜索策略',
    '后端凭据',
    '全部展开',
    '全部收起',
    '已禁用',
    '无需密钥',
    '已配置',
    '固定后端当前不可用，搜索会按自动顺序回退到下一个已配置后端。',
    '已禁用；不会参与自动、固定或模型指定。',
    '中文网页搜索，适合资讯与通用检索。',
    'GLM Coding Plan 的联网搜索 MCP，密钥与开放平台不通用。',
    '无需密钥的 DuckDuckGo 搜索，始终可用。',
  ]) {
    assert.ok(catalog.includes(`'${phrase}':`), `missing English translation: ${phrase}`);
  }
});
