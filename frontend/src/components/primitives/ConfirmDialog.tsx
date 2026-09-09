import { JSX } from 'preact';
import { signal } from '@preact/signals';
import { Modal } from './Modal';

interface ConfirmState {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  isDestructive: boolean;
  withInput?: boolean;
  inputPlaceholder?: string;
  inputValue?: string;
  onConfirm: (inputVal?: string) => void;
  onCancel: () => void;
}

export const confirmSignal = signal<ConfirmState>({
  isOpen: false,
  title: '',
  message: '',
  confirmLabel: 'Confirm',
  cancelLabel: 'Cancel',
  isDestructive: false,
  onConfirm: () => {},
  onCancel: () => {},
});

export function promptConfirm(opts: {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  isDestructive?: boolean;
  withInput?: boolean;
  inputPlaceholder?: string;
}): Promise<{ confirmed: boolean; value?: string }> {
  return new Promise((resolve) => {
    confirmSignal.value = {
      isOpen: true,
      title: opts.title,
      message: opts.message,
      confirmLabel: opts.confirmLabel || 'Confirm',
      cancelLabel: opts.cancelLabel || 'Cancel',
      isDestructive: !!opts.isDestructive,
      withInput: opts.withInput,
      inputPlaceholder: opts.inputPlaceholder,
      inputValue: '',
      onConfirm: (val) => {
        confirmSignal.value = { ...confirmSignal.value, isOpen: false };
        resolve({ confirmed: true, value: val });
      },
      onCancel: () => {
        confirmSignal.value = { ...confirmSignal.value, isOpen: false };
        resolve({ confirmed: false });
      },
    };
  });
}

export function ConfirmDialog(): JSX.Element | null {
  const state = confirmSignal.value;
  if (!state.isOpen) return null;

  return (
    <Modal
      isOpen={state.isOpen}
      onClose={state.onCancel}
      title={state.title}
      maxWidth="460px"
    >
      <div class="confirm-dialog-body">
        <p class="confirm-message">{state.message}</p>

        {state.withInput && (
          <div class="form-group" style={{ marginTop: '12px' }}>
            <input
              id="confirm-input-field"
              class="form-input"
              placeholder={state.inputPlaceholder || 'Enter value...'}
              value={state.inputValue || ''}
              onInput={(e) => {
                confirmSignal.value = {
                  ...confirmSignal.value,
                  inputValue: (e.target as HTMLInputElement).value,
                };
              }}
              autoFocus
            />
          </div>
        )}

        <div class="modal-actions" style={{ marginTop: '20px' }}>
          <button class="btn-secondary" onClick={state.onCancel}>
            {state.cancelLabel}
          </button>
          <button
            class={state.isDestructive ? 'btn-danger' : 'btn-primary'}
            onClick={() => state.onConfirm(state.inputValue)}
          >
            {state.confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
