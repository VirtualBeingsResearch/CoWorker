import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('emergency backups expose confirmed deletion and refresh the list', () => {
  assert.match(adminApp, /function BackupDelete/);
  assert.match(adminApp, /method: 'DELETE'/);
  assert.match(adminApp, /confirm_name: confirmationName/);
  assert.match(adminApp, /onDeleted\(\)/);
  assert.match(adminApp, /backups\.setData/);
});
