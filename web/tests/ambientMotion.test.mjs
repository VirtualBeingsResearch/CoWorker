import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const reducedMotion = styles.slice(styles.lastIndexOf('@media (prefers-reduced-motion: reduce)'));

test('keeps core ambient motion alive at a reduced amplitude', () => {
  assert.match(reducedMotion, /\.avatar-wrap::before\s*\{\s*animation:\s*reduced-avatar-drift/);
  assert.match(reducedMotion, /\.sprite-btn\s*\{\s*animation:\s*reduced-sprite-bob/);
  assert.match(reducedMotion, /\.sprite-orbit\s*\{\s*animation-duration:/);
  assert.match(reducedMotion, /\.sprite-pulse\s*\{\s*animation:\s*reduced-sprite-ring/);
  assert.doesNotMatch(reducedMotion, /animation:\s*none/);
});
