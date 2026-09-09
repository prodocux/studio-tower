import { JSX } from 'preact';
import { toastsSignal, dismissToast } from '../../services/toast';

export function ToastContainer(): JSX.Element | null {
  const toasts = toastsSignal.value;
  if (toasts.length === 0) return null;

  return (
    <div class="toast-container" role="region" aria-label="Notifications" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} class={`toast-card toast-${toast.type}`}>
          <span class="toast-icon">
            {toast.type === 'success' && '✅'}
            {toast.type === 'error' && '❌'}
            {toast.type === 'warning' && '⚠️'}
            {toast.type === 'info' && 'ℹ️'}
          </span>
          <span class="toast-message">{toast.message}</span>
          <button
            class="toast-close-btn"
            onClick={() => dismissToast(toast.id)}
            aria-label="Dismiss notification"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
