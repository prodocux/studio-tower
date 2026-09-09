import { JSX } from 'preact';
import { useState } from 'preact/hooks';
import { Modal } from '../primitives/Modal';
import { activeModalSignal, spacesSignal, activeSpaceIdSignal } from '../../services/store';
import { api } from '../../services/api';
import { showToast } from '../../services/toast';

export function JoinSpaceModal(): JSX.Element | null {
  const isOpen = activeModalSignal.value === 'join_space';
  const [tokenInput, setTokenInput] = useState('');
  const [isJoining, setIsJoining] = useState(false);

  const handleClose = () => {
    if (!isJoining) {
      activeModalSignal.value = null;
      setTokenInput('');
    }
  };

  const handleJoin = async (e: JSX.TargetedEvent) => {
    e.preventDefault();
    const raw = tokenInput.trim();
    if (!raw || isJoining) return;

    // Extract token if full URL is pasted
    let cleanToken = raw;
    if (raw.includes('/invites/')) {
      cleanToken = raw.split('/invites/')[1].split('/')[0].split('?')[0];
    }

    setIsJoining(true);
    try {
      const joinedSpace = await api.acceptInvite(cleanToken);
      // Add space if not already in list
      if (!spacesSignal.value.some((s) => s.space_id === joinedSpace.space_id)) {
        spacesSignal.value = [...spacesSignal.value, joinedSpace];
      }
      activeSpaceIdSignal.value = joinedSpace.space_id;
      showToast(`Successfully joined "${joinedSpace.name}"!`, 'success');
      handleClose();
    } catch (err: any) {
      showToast(`Failed to join space: ${err.message}`, 'error');
    } finally {
      setIsJoining(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title="Join Space with Invite Link">
      <form onSubmit={handleJoin} class="space-modal-form">
        <div class="form-group">
          <label class="form-label" htmlFor="invite-token-input">
            Invitation Link or Token
          </label>
          <input
            id="invite-token-input"
            class="form-input"
            placeholder="Paste invite token (e.g. inv_7a9f8b...)"
            value={tokenInput}
            onInput={(e) => setTokenInput((e.target as HTMLInputElement).value)}
            disabled={isJoining}
            autoFocus
          />
        </div>

        <div class="modal-actions">
          <button type="button" class="btn-secondary" onClick={handleClose} disabled={isJoining}>
            Cancel
          </button>
          <button type="submit" class="btn-primary" disabled={!tokenInput.trim() || isJoining}>
            {isJoining ? 'Joining...' : 'Join Space'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
