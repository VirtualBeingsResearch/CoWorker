const REFERENCE_FRAME_MS = 1000 / 60;
const REFERENCE_ALPHA = 0.18;
const SMOOTHING_TIME_CONSTANT_MS = -REFERENCE_FRAME_MS / Math.log(1 - REFERENCE_ALPHA);

/**
 * Convert elapsed frame time into a refresh-rate-independent exponential easing step.
 * The curve matches the previous 18% step at 60 Hz while behaving identically at 120 Hz.
 */
export function frameSmoothingAlpha(elapsedMs: number): number {
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return 0;
  const boundedElapsed = Math.min(elapsedMs, 64);
  return 1 - Math.exp(-boundedElapsed / SMOOTHING_TIME_CONSTANT_MS);
}

export const DEFAULT_FRAME_MS = REFERENCE_FRAME_MS;
