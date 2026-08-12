import assert from 'node:assert/strict';
import test from 'node:test';

import { motionBudgetIsActive } from '../src/lib/motionBudget.ts';

test('runs motion only while the page can present it', () => {
  assert.equal(motionBudgetIsActive('visible', true, false), true);
  assert.equal(motionBudgetIsActive('hidden', true, false), false);
  assert.equal(motionBudgetIsActive('visible', false, false), false);
});

test('honors the operating system reduced-motion preference', () => {
  assert.equal(motionBudgetIsActive('visible', true, true), false);
});
