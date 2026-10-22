import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
  ENGLISH_SINGULAR_OVERRIDES,
  selectEnglishPhrase,
} from '../src/i18n/plural.ts';

const sourceRoot = fileURLToPath(new URL('../src', import.meta.url));
const i18nSource = await readFile(new URL('../src/i18n/catalog.ts', import.meta.url), 'utf8');
const i18nEngine = await readFile(new URL('../src/i18n/admin.tsx', import.meta.url), 'utf8');
const adminAppSource = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const cjk = /[\u3400-\u9fff]/;

function catalogKeys(source) {
  return new Set([...source.matchAll(/^\s*'((?:[^'\\]|\\.)*)'\s*:/gm)].map(match => match[1]));
}

function catalogStringValues(source) {
  return [...source.matchAll(/^\s*'((?:[^'\\]|\\.)*)'\s*:\s*'((?:[^'\\]|\\.)*)'/gm)]
    .map(match => ({ key: match[1], value: match[2] }));
}

const englishStart = i18nSource.indexOf('const ENGLISH_PHRASES');
const statusStart = i18nSource.indexOf('const STATUS_ENGLISH_PHRASES');
const englishCatalog = i18nSource.slice(englishStart, statusStart);
const statusCatalog = i18nSource.slice(statusStart);
const translatedKeys = catalogKeys(i18nSource);
const englishValues = [
  ...catalogStringValues(englishCatalog),
  ...catalogStringValues(statusCatalog),
];

function blockValues(source, marker) {
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `${marker} must exist`);
  const end = source.indexOf('\n};', start);
  assert.notEqual(end, -1, `${marker} must be a complete object`);
  return source.slice(start, end);
}

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) files.push(...await walk(path));
    else if (/\.(ts|tsx)$/.test(entry.name)) files.push(path);
  }
  return files;
}

test('selects the English singular override only for one item', () => {
  assert.equal(
    selectEnglishPhrase('{{count}} 项能力', '{{count}} capabilities', { count: 1 }),
    '{{count}} capability',
  );
  assert.equal(
    selectEnglishPhrase('{{count}} 项能力', '{{count}} capabilities', { count: '1' }),
    '{{count}} capability',
  );
  assert.equal(
    selectEnglishPhrase('{{count}} 项能力', '{{count}} capabilities', { count: 0 }),
    '{{count}} capabilities',
  );
  assert.equal(
    selectEnglishPhrase('{{count}} 项能力', '{{count}} capabilities', { count: 2 }),
    '{{count}} capabilities',
  );
  assert.equal(
    selectEnglishPhrase('{{count}} 项能力', '{{count}} capabilities'),
    '{{count}} capabilities',
  );
});

test('keeps singular overrides aligned with the catalog', () => {
  for (const [key, value] of Object.entries(ENGLISH_SINGULAR_OVERRIDES)) {
    assert.ok(translatedKeys.has(key), `missing catalog key: ${key}`);
    assert.match(key, /\{\{count\}\}/);
    assert.match(value, /\{\{count\}\}/);
    assert.ok(!cjk.test(value), `singular override should be English: ${key}`);
  }
});

test('keeps every English catalog value free of untranslated Chinese', () => {
  const leaks = englishValues.filter(item => cjk.test(item.value));
  assert.deepEqual(leaks, []);
});

test('translates every Chinese string passed to t()', async () => {
  const files = (await walk(sourceRoot)).filter(path => !path.includes('/i18n/'));
  const missing = [];
  for (const file of files) {
    const source = await readFile(file, 'utf8');
    for (const match of source.matchAll(/\bt\(\s*(['"])((?:\\.|(?!\1).)*)\1/gs)) {
      const key = match[2];
      if (cjk.test(key) && !translatedKeys.has(key)) {
        const line = source.slice(0, match.index).split('\n').length;
        missing.push(`${file.replace(sourceRoot, 'src')}:${line} ${key}`);
      }
    }
  }
  assert.deepEqual([...new Set(missing)], []);
});

test('translates every label rendered through the admin label maps', () => {
  const blocks = [
    blockValues(adminAppSource, 'const CONFIG_LABELS: Record<string, string> = {'),
    blockValues(adminAppSource, 'const BOOTSTRAP_CONFIG_GROUP_LABELS: Record<string, string> = {'),
    blockValues(adminAppSource, 'const BOOTSTRAP_CONFIG_GROUP_NOTES: Record<string, string> = {'),
    blockValues(adminAppSource, 'const GROUP_LABELS: Record<string, string> = {'),
  ];
  const navBlock = adminAppSource.slice(
    adminAppSource.indexOf('const NAV: Array'),
    adminAppSource.indexOf('const DEFAULT_SECTION_BY_WORKSPACE'),
  );
  blocks.push(navBlock);

  const missing = [];
  for (const block of blocks) {
    for (const match of block.matchAll(/(?:^|:)\s*'((?:[^'\\]|\\.)*)'/gm)) {
      const label = match[1];
      if (cjk.test(label) && !translatedKeys.has(label)) missing.push(label);
    }
  }
  assert.deepEqual([...new Set(missing)], []);
});

test('prefers the catalog for the active surface and falls back to the other', () => {
  assert.match(i18nEngine, /activeSurface = surface;/);
  assert.match(
    i18nEngine,
    /const primary = activeSurface === 'admin' \? ENGLISH_PHRASES : STATUS_ENGLISH_PHRASES;/,
  );
  assert.match(
    i18nEngine,
    /const secondary = activeSurface === 'admin' \? STATUS_ENGLISH_PHRASES : ENGLISH_PHRASES;/,
  );
});
