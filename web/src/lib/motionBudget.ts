export function motionBudgetIsActive(
  visibilityState: DocumentVisibilityState,
  hasFocus: boolean,
  prefersReducedMotion: boolean,
): boolean {
  return visibilityState === 'visible' && hasFocus && !prefersReducedMotion;
}
