export interface MergedAbortSignalResult {
  signal: AbortSignal;
  cleanup: () => void;
}

/**
 * Creates a merged AbortSignal that aborts whenever either:
 * 1. The optional parent navigation `navSignal` aborts, OR
 * 2. The specified `timeoutMs` timer fires.
 *
 * Provides a `cleanup()` function that safely clears timers and event listeners.
 */
export function createMergedAbortSignal(
  navSignal?: AbortSignal,
  timeoutMs: number = 8000
): MergedAbortSignalResult {
  const controller = new AbortController();

  let timeoutId: any = setTimeout(() => {
    if (!controller.signal.aborted) {
      controller.abort(new Error(`Request timed out after ${timeoutMs}ms`));
    }
  }, timeoutMs);

  const onNavAbort = () => {
    if (!controller.signal.aborted) {
      controller.abort(navSignal?.reason || new Error('Navigation aborted'));
    }
  };

  if (navSignal) {
    if (navSignal.aborted) {
      onNavAbort();
    } else {
      navSignal.addEventListener('abort', onNavAbort, { once: true });
    }
  }

  const cleanup = () => {
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    if (navSignal) {
      navSignal.removeEventListener('abort', onNavAbort);
    }
  };

  return { signal: controller.signal, cleanup };
}
