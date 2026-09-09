import { signal } from '@preact/signals';
import { ToastItem, ToastType } from '../types';

export const toastsSignal = signal<ToastItem[]>([]);

export function showToast(message: string, type: ToastType = 'info', durationMs: number = 3000): void {
  const id = `toast_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`;
  const item: ToastItem = { id, message, type, durationMs };
  toastsSignal.value = [...toastsSignal.value, item];

  if (durationMs > 0) {
    setTimeout(() => {
      dismissToast(id);
    }, durationMs);
  }
}

export function dismissToast(id: string): void {
  toastsSignal.value = toastsSignal.value.filter((t) => t.id !== id);
}
