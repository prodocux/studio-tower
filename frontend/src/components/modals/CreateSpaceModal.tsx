import { JSX } from 'preact';
import { useState } from 'preact/hooks';
import { Modal } from '../primitives/Modal';
import { activeModalSignal, spacesSignal, activeSpaceIdSignal } from '../../services/store';
import { api } from '../../services/api';
import { showToast } from '../../services/toast';

export function CreateSpaceModal(): JSX.Element | null {
  const isOpen = activeModalSignal.value === 'create_space';
  const [name, setName] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleClose = () => {
    if (!isSubmitting) {
      activeModalSignal.value = null;
      setName('');
    }
  };

  const handleSubmit = async (e: JSX.TargetedEvent) => {
    e.preventDefault();
    if (!name.trim() || isSubmitting) return;

    setIsSubmitting(true);
    try {
      const newSpace = await api.createSpace(name.trim());
      spacesSignal.value = [...spacesSignal.value, newSpace];
      activeSpaceIdSignal.value = newSpace.space_id;
      showToast(`Created space "${newSpace.name}"`, 'success');
      handleClose();
    } catch (err: any) {
      showToast(`Failed to create space: ${err.message}`, 'error');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title="Create Shared Production Space">
      <form onSubmit={handleSubmit} class="space-modal-form">
        <div class="form-group">
          <label class="form-label" htmlFor="space-name-input">
            Space Name
          </label>
          <input
            id="space-name-input"
            class="form-input"
            placeholder="e.g. Project Bersama - Unit 1"
            value={name}
            onInput={(e) => setName((e.target as HTMLInputElement).value)}
            disabled={isSubmitting}
            autoFocus
          />
        </div>

        <div class="modal-actions">
          <button type="button" class="btn-secondary" onClick={handleClose} disabled={isSubmitting}>
            Cancel
          </button>
          <button type="submit" class="btn-primary" disabled={!name.trim() || isSubmitting}>
            {isSubmitting ? 'Creating...' : 'Create Space'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
