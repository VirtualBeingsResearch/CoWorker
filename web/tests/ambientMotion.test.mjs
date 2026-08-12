import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
const reducedMotion = styles.slice(styles.lastIndexOf('@media (prefers-reduced-motion: reduce)'));

test('keeps core identity and chat motion continuous in reduced-motion mode', () => {
  assert.match(reducedMotion, /\.avatar-wrap::before\s*\{\s*animation:\s*reduced-avatar-drift/);
  assert.match(reducedMotion, /\.kaomoji-face\s*\{\s*animation:\s*reduced-kaomoji-float/);
  assert.match(reducedMotion, /\.state-wave i\s*\{\s*animation:\s*reduced-state-wave/);
  assert.match(
    reducedMotion,
    /\.sprite-btn:not\(\.open\):not\(\.dragging\)\s*\{\s*animation:\s*reduced-sprite-bob/,
  );
  assert.match(reducedMotion, /\.sprite-orbit\s*\{\s*animation-duration:\s*14s/);
  assert.doesNotMatch(reducedMotion, /\.sprite-btn\.has-unread\s*\{\s*animation:\s*none/);
});

test('retains readable static fallbacks for high-frequency ledger effects', () => {
  assert.match(reducedMotion, /\.ledger \.le:is\(\.thinking, \.sleep\.active, \.tool_call\)[\s\S]*animation:\s*none/);
  assert.match(reducedMotion, /\.ledger \.le\.thinking \.le-text[\s\S]*background:\s*none/);
  assert.match(reducedMotion, /\.ledger \.le\.tool_call \.le-text[\s\S]*width:\s*auto/);
});
