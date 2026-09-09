import { describe, it, expect, beforeEach } from 'vitest';
import { toastsSignal, showToast, dismissToast } from '../src/services/toast';
import { requestSequencer } from '../src/services/store';

describe('Toast and Store Signals', () => {
  beforeEach(() => {
    toastsSignal.value = [];
  });

  it('should add toast notification to toastsSignal', () => {
    showToast('Test notification', 'success', 0);
    expect(toastsSignal.value.length).toBe(1);
    expect(toastsSignal.value[0].message).toBe('Test notification');
    expect(toastsSignal.value[0].type).toBe('success');
  });

  it('should dismiss toast notification by ID', () => {
    showToast('Item to dismiss', 'error', 0);
    const id = toastsSignal.value[0].id;
    dismissToast(id);
    expect(toastsSignal.value.length).toBe(0);
  });

  it('should abort previous signal when requestSequencer triggers new signal', () => {
    const signal1 = requestSequencer.getNextSignal();
    expect(signal1.aborted).toBe(false);

    const signal2 = requestSequencer.getNextSignal();
    expect(signal1.aborted).toBe(true);
    expect(signal2.aborted).toBe(false);
  });
});
