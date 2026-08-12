import { useEffect, useState } from 'react';
import { motionBudgetIsActive } from '../lib/motionBudget';

function currentMotionState(media: MediaQueryList, pageFocused: boolean): boolean {
  return motionBudgetIsActive(
    document.visibilityState,
    pageFocused,
    media.matches,
  );
}

/** Pause ambient work when the page cannot present it, matching a particle engine's pause-on-blur budget. */
export function usePageMotionBudget(): boolean {
  const [active, setActive] = useState(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return false;
    return currentMotionState(
      window.matchMedia('(prefers-reduced-motion: reduce)'),
      document.visibilityState === 'visible',
    );
  });

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    let pageFocused = document.visibilityState === 'visible';

    const apply = () => {
      const next = currentMotionState(media, pageFocused);
      document.documentElement.dataset.motion = next ? 'active' : 'paused';
      setActive(current => current === next ? current : next);
    };
    const pause = () => {
      pageFocused = false;
      apply();
    };
    const resume = () => {
      pageFocused = true;
      apply();
    };
    const onVisibilityChange = () => {
      pageFocused = document.visibilityState === 'visible';
      apply();
    };

    apply();
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', resume);
    window.addEventListener('blur', pause);
    window.addEventListener('pageshow', resume);
    window.addEventListener('pagehide', pause);
    media.addEventListener('change', apply);

    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', resume);
      window.removeEventListener('blur', pause);
      window.removeEventListener('pageshow', resume);
      window.removeEventListener('pagehide', pause);
      media.removeEventListener('change', apply);
      delete document.documentElement.dataset.motion;
    };
  }, []);

  return active;
}
