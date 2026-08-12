import assert from 'node:assert/strict';
import test from 'node:test';

import { DEFAULT_FRAME_MS, frameSmoothingAlpha } from '../src/lib/animationFrame.ts';

function remainingDistance(frameMs, frameCount) {
  let remaining = 1;
  for (let index = 0; index < frameCount; index += 1) {
    remaining *= 1 - frameSmoothingAlpha(frameMs);
  }
  return remaining;
}

test('matches the previous easing response at 60 Hz', () => {
  assert.ok(Math.abs(frameSmoothingAlpha(DEFAULT_FRAME_MS) - 0.18) < 1e-12);
});

test('keeps the same scroll response at 60 Hz and 120 Hz', () => {
  const at60Hz = remainingDistance(1000 / 60, 6);
  const at120Hz = remainingDistance(1000 / 120, 12);
  assert.ok(Math.abs(at60Hz - at120Hz) < 1e-12);
});

test('bounds a long frame instead of jumping straight to the target', () => {
  assert.equal(frameSmoothingAlpha(0), 0);
  assert.equal(frameSmoothingAlpha(Number.NaN), 0);
  assert.equal(frameSmoothingAlpha(500), frameSmoothingAlpha(64));
  assert.ok(frameSmoothingAlpha(500) < 1);
});
