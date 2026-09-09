import { JSX } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import { ActionProposal, Message } from '../types';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { messagesSignal, runsSignal, activeViewSignal } from '../services/store';

interface ActionProposalCardProps {
  proposal: ActionProposal;
  message: Message;
  spaceId: string;
}

export function ActionProposalCard({ proposal, message, spaceId }: ActionProposalCardProps): JSX.Element {
  const [isConfirming, setIsConfirming] = useState(false);
  const matchingRun = runsSignal.value.find(
    (r) => r.action_id === proposal.action_id || (message.run_id && r.run_id === message.run_id)
  );
  const activeRunId = message.run_id || matchingRun?.run_id;
  const [isConfirmed, setIsConfirmed] = useState(Boolean(activeRunId));
  const [isExpired, setIsExpired] = useState(false);
  const [timeLeft, setTimeLeft] = useState<string>('');

  useEffect(() => {
    if (activeRunId) {
      setIsConfirmed(true);
      setIsExpired(false);
    }
  }, [activeRunId, runsSignal.value]);

  useEffect(() => {
    if (isConfirmed || activeRunId) return;

    const updateCountdown = () => {
      const expTime = new Date(proposal.expires_at).getTime();
      const now = Date.now();
      const diff = expTime - now;

      if (diff <= 0) {
        setIsExpired(true);
        setTimeLeft('Expired');
      } else {
        const secs = Math.ceil(diff / 1000);
        const mins = Math.floor(secs / 60);
        const remSecs = secs % 60;
        setTimeLeft(`${mins}:${remSecs < 10 ? '0' : ''}${remSecs}`);
      }
    };

    updateCountdown();
    const interval = setInterval(updateCountdown, 1000);
    return () => clearInterval(interval);
  }, [proposal.expires_at, isConfirmed, activeRunId]);

  const handleConfirm = async () => {
    if (isConfirming || isConfirmed || isExpired) return;

    setIsConfirming(true);
    try {
      const res = await api.confirmAction(spaceId, proposal.action_id);
      setIsConfirmed(true);
      showToast(`Action confirmed! Executed Run ${res.run_id}`, 'success');

      // Update message in store with run_id
      messagesSignal.value = messagesSignal.value.map((m) =>
        m.message_id === message.message_id ? { ...m, run_id: res.run_id } : m
      );

      // Append confirmation message from server if returned
      if (res.message && !messagesSignal.value.some((m) => m.message_id === res.message.message_id)) {
        messagesSignal.value = [...messagesSignal.value, res.message];
      }

      // Fetch fresh runs
      try {
        const runs = await api.listRuns(spaceId);
        runsSignal.value = runs;
      } catch {
        // non-blocking
      }
    } catch (err: any) {
      showToast(`Confirmation failed: ${err.message || 'Error executing action'}`, 'error');
    } finally {
      setIsConfirming(false);
    }
  };

  return (
    <div
      class="action-proposal-card"
      data-action-id={proposal.action_id}
      style={{
        marginTop: '10px',
        padding: '12px 16px',
        backgroundColor: 'var(--bg-card, rgba(59, 130, 246, 0.08))',
        border: '1px solid var(--border-accent, rgba(59, 130, 246, 0.25))',
        borderRadius: '8px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '6px' }}>
        <span style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--accent-primary, #60a5fa)' }}>
          ⚡ Action Proposal: {proposal.title}
        </span>
        {!isConfirmed && !isExpired && (
          <span
            style={{
              fontSize: '0.75rem',
              color: 'var(--text-muted, #94a3b8)',
              background: 'var(--bg-badge, rgba(0,0,0,0.2))',
              padding: '2px 8px',
              borderRadius: '12px',
            }}
          >
            Expires in {timeLeft}
          </span>
        )}
      </div>

      <p style={{ margin: '0 0 10px', fontSize: '0.85rem', color: 'var(--text-secondary, #cbd5e1)', lineHeight: '1.4' }}>
        {proposal.description}
      </p>

      <div style={{ fontSize: '0.78rem', color: 'var(--text-muted, #94a3b8)', marginBottom: '8px' }}>
        Output format: {proposal.output_format ? proposal.output_format.toUpperCase() : 'Legacy default'}
      </div>

      {((proposal.sources && proposal.sources.length > 0) || (proposal.source_file_ids && proposal.source_file_ids.length > 0)) && (
        <div style={{ fontSize: '0.78rem', color: 'var(--text-muted, #94a3b8)', marginBottom: '10px' }}>
          📎 Sources: {proposal.sources ? proposal.sources.length : proposal.source_file_ids.length} files
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        {isConfirmed ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                fontSize: '0.8rem',
                color: 'var(--color-success, #4ade80)',
                background: 'rgba(74, 222, 128, 0.12)',
                padding: '4px 10px',
                borderRadius: '6px',
                border: '1px solid rgba(74, 222, 128, 0.3)',
              }}
            >
              ✓ Action Confirmed (Run `{activeRunId || 'active'}`)
            </span>
            <button
              type="button"
              id="btn-view-runs"
              class="btn-compact btn-secondary"
              style={{ fontSize: '0.75rem', padding: '3px 8px' }}
              onClick={() => {
                activeViewSignal.value = 'runs';
              }}
            >
              View Runs →
            </button>
          </div>
        ) : isExpired ? (
          <span
            style={{
              fontSize: '0.8rem',
              color: 'var(--color-danger, #f87171)',
              background: 'rgba(248, 113, 113, 0.12)',
              padding: '4px 10px',
              borderRadius: '6px',
              border: '1px solid rgba(248, 113, 113, 0.3)',
            }}
          >
            ✕ Proposal Expired
          </span>
        ) : (
          <button
            type="button"
            id="btn-confirm-action-proposal"
            class="btn-compact btn-primary btn-confirm-action-proposal"
            data-action-id={proposal.action_id}
            disabled={isConfirming}
            onClick={handleConfirm}
            style={{ fontSize: '0.85rem', padding: '6px 14px', borderRadius: '6px' }}
          >
            {isConfirming ? '⏳ Executing...' : '✓ Confirm & Execute'}
          </button>
        )}
      </div>
    </div>
  );
}
