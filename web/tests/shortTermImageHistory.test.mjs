import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');
const adminCss = await readFile(new URL('../src/admin/admin.css', import.meta.url), 'utf8');

test('short-term history loads protected image previews only for open messages', () => {
  assert.match(adminApp, /function MemoryImagePreview/);
  assert.match(adminApp, /Authorization: `Bearer \$\{storedToken\(\)\}`/);
  assert.match(adminApp, /open && <MemoryImageGallery content=\{message\.content\}/);
  assert.match(adminApp, /open && <MemoryImageGallery content=\{call\.result\}/);
  assert.match(adminCss, /\.memory-image-preview img/);
});
