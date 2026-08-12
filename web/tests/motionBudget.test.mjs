import assert from 'node:assert/strict';
import test from 'node:test';

import { motionBudgetIsActive, motionBudgetState } from '../src/lib/motionBudget.ts';

test('runs motion only while the page can present it', () => {
  assert.equal(motionBudgetIsActive('visible', false), true);
  assert.equal(motionBudgetIsActive('hidden', false), false);
});

test('honors the operating system reduced-motion preference', () => {
  assert.equal(motionBudgetIsActive('visible', true), false);
  assert.equal(motionBudgetState('visible', true), 'reduced');
});

test('distinguishes a hidden-page pause from reduced motion', () => {
  assert.equal(motionBudgetState('hidden', false), 'paused');
  assert.equal(motionBudgetState('visible', false), 'active');
});
