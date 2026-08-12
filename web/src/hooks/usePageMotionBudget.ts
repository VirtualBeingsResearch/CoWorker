import { useEffect, useState } from 'react';
import { motionBudgetIsActive, motionBudgetState } from '../lib/motionBudget';

function currentMotionState(media: MediaQueryList): boolean {
  return motionBudgetIsActive(
    document.visibilityState,
    media.matches,
  );
}

/** Pause ambient work only when the page cannot present it or the user requests reduced motion. */
export function usePageMotionBudget(): boolean {
  const [active, setActive] = useState(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return false;
    return currentMotionState(window.matchMedia('(prefers-reduced-motion: reduce)'));
  });

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');

    const apply = () => {
      const next = currentMotionState(media);
      document.documentElement.dataset.motion = motionBudgetState(document.visibilityState, media.matches);
      setActive(current => current === next ? current : next);
    };

    apply();
    document.addEventListener('visibilitychange', apply);
    window.addEventListener('pageshow', apply);
    media.addEventListener('change', apply);

    return () => {
      document.removeEventListener('visibilitychange', apply);
      window.removeEventListener('pageshow', apply);
      media.removeEventListener('change', apply);
      delete document.documentElement.dataset.motion;
    };
  }, []);

  return active;
}
