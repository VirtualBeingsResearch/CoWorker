import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const styles = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('keeps oversized advanced initialization groups inside a real scroll container', () => {
  assert.match(
    styles,
    /\.bootstrap-advanced-scroll\s*\{[^}]*grid-template-rows:\s*auto minmax\(0, 1fr\);[^}]*overflow:\s*hidden;/,
  );
  assert.match(styles, /\.bootstrap-advanced-dialog \.bootstrap-config-workbench\s*\{[^}]*min-height:\s*0;/);
  assert.match(
    styles,
    /\.bootstrap-advanced-dialog \.bootstrap-config-panel\s*\{[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;/,
  );
  assert.match(adminApp, /panelRef\.current\?\.scrollTo\(\{ top: 0, behavior: 'auto' \}\)/);
  assert.match(adminApp, /className="bootstrap-config-panel" ref=\{panelRef\}/);
});
