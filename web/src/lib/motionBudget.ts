export type MotionBudgetState = 'active' | 'paused' | 'reduced';

export function motionBudgetState(
  visibilityState: DocumentVisibilityState,
  prefersReducedMotion: boolean,
): MotionBudgetState {
  if (prefersReducedMotion) return 'reduced';
  return visibilityState === 'visible' ? 'active' : 'paused';
}

export function motionBudgetIsActive(
  visibilityState: DocumentVisibilityState,
  prefersReducedMotion: boolean,
): boolean {
  return motionBudgetState(visibilityState, prefersReducedMotion) === 'active';
}
