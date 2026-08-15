import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const adminApp = readFileSync(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('renders managed provider types from the runtime catalog', () => {
  assert.match(adminApp, /const providerCatalog: Json\[\] = Array\.isArray\(data\.provider_catalog\)/);
  assert.match(adminApp, /const providerTypes = providerCatalog\.map/);
  assert.doesNotMatch(
    adminApp,
    /\['openai', 'anthropic', 'deepseek', 'qwen', 'zhipu', 'minimax'\]\.map/,
  );
});

test('allows model discovery and setup without a key for keyless providers', () => {
  assert.match(adminApp, /const apiKeyRequired = selectedCatalog\.requires_api_key !== false/);
  assert.match(adminApp, /required=\{apiKeyRequired\}/);
  assert.match(adminApp, /\(apiKeyRequired && !apiKey\.trim\(\)\)/);
  assert.match(adminApp, /requiresApiKey && !apiKey\.trim\(\) && !providerName\.trim\(\)/);
});
