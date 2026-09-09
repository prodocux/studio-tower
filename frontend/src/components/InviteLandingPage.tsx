import { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import {
  currentUserSignal,
  authManager,
  setPendingInviteSession,
  clearPendingInviteSession,
} from '../services/auth';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { InvitePreviewResponse, Space } from '../types';

interface InviteLandingPageProps {
  token: string;
  onJoined: (space: Space) => void;
  onCancel: () => void;
}

export function InviteLandingPage({ token, onJoined, onCancel }: InviteLandingPageProps): JSX.Element {
  const currentUser = currentUserSignal.value;
  const [preview, setPreview] = useState<InvitePreviewResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);

  // Fetch preview on mount with AbortController
  useEffect(() => {
    const controller = new AbortController();
    setIsLoading(true);
    setErrorMessage(null);
    setErrorCode(null);

    api.getInvitePreview(token, controller.signal)
      .then((data) => {
        setPreview(data);
      })
      .catch((err: any) => {
        if (controller.signal.aborted) return;
        let msg = err.message || 'Failed to load invitation.';
        let code = 'INVITE_ERROR';

        if (err.detail && typeof err.detail === 'object') {
          code = err.detail.code || 'INVITE_ERROR';
          msg = err.detail.message || msg;
        } else if (err.status === 404) {
          code = 'INVITE_NOT_FOUND';
          msg = 'This invitation link was not found or is invalid.';
        } else if (err.status === 410) {
          code = 'INVITE_EXPIRED';
          msg = 'This invitation has expired, been revoked, or reached its usage limit.';
        }

        setErrorCode(code);
        setErrorMessage(msg);
        // Purge any stale pending invite session on definitive failure
        clearPendingInviteSession();
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [token]);

  // Unauthenticated user clicks "Sign in with Google to Accept"
  const handleSignInToAccept = async () => {
    if (!preview) return;
    setIsProcessing(true);

    // Save intent to sessionStorage with 15-min TTL
    setPendingInviteSession({
      token,
      target_space_name: preview.space_name,
      target_role: preview.role,
    });

    try {
      const outcome = await authManager.signInWithGoogle('popup');
      if (outcome.status === 'cancelled') {
        // User closed or cancelled popup: purge pending invite immediately
        clearPendingInviteSession();
      } else if (outcome.status === 'error') {
        // Error during auth: purge pending invite immediately
        clearPendingInviteSession();
        showToast(`Login failed: ${outcome.error?.message || 'Authentication error'}`, 'error');
      }
    } catch (err: any) {
      clearPendingInviteSession();
      showToast(`Login failed: ${err.message}`, 'error');
    } finally {
      setIsProcessing(false);
    }
  };

  // Authenticated user confirms acceptance
  const handleAcceptInvite = async () => {
    setIsProcessing(true);
    try {
      const space = await api.acceptInvite(token);
      clearPendingInviteSession();
      showToast(`Successfully joined "${space.name}"!`, 'success');
      onJoined(space);
    } catch (err: any) {
      clearPendingInviteSession();
      const detailMsg = err.detail?.message || err.message || 'Failed to accept invitation';
      setErrorMessage(detailMsg);
      showToast(detailMsg, 'error');
    } finally {
      setIsProcessing(false);
    }
  };

  const handleDecline = () => {
    clearPendingInviteSession();
    onCancel();
  };

  return (
    <div class="login-gate-container">
      <div class="login-card" style={{ maxWidth: '480px', textAlign: 'left' }}>
        {/* Brand Header */}
        <div class="logo-badge" style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span class="logo-icon">🎬</span>
          <div>
            <h1 class="app-title" style={{ fontSize: '20px', margin: 0 }}>StudioTower</h1>
            <p class="app-tagline" style={{ margin: 0, fontSize: '12px' }}>Workspace Invitation</p>
          </div>
        </div>

        {/* Loading State */}
        {isLoading && (
          <div style={{ padding: '32px 0', textAlign: 'center', color: 'var(--text-secondary)' }}>
            <p style={{ fontSize: '14px' }}>Validating invitation link...</p>
          </div>
        )}

        {/* Error State */}
        {!isLoading && errorMessage && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', margin: '16px 0' }}>
            <div style={{
              background: 'rgba(244, 63, 94, 0.1)',
              border: '1px solid var(--accent-rose)',
              borderRadius: '8px',
              padding: '16px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--accent-rose)', fontWeight: 600, marginBottom: '6px' }}>
                <span>⚠️</span>
                <span>Invitation Unavailable</span>
                {errorCode && (
                  <span style={{ fontSize: '11px', background: 'rgba(244, 63, 94, 0.2)', padding: '2px 6px', borderRadius: '4px', fontFamily: 'var(--font-mono)' }}>
                    {errorCode}
                  </span>
                )}
              </div>
              <p style={{ fontSize: '13px', color: 'var(--text-primary)', margin: 0 }}>{errorMessage}</p>
            </div>

            <button
              type="button"
              class="btn-secondary"
              style={{ width: '100%', justifyContent: 'center' }}
              onClick={handleDecline}
            >
              Return to Home
            </button>
          </div>
        )}

        {/* Active Preview State */}
        {!isLoading && !errorMessage && preview && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            <div>
              <span style={{ fontSize: '12px', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--accent-cyan)', fontWeight: 600 }}>
                You've been invited to join
              </span>
              <h2 style={{ fontSize: '22px', fontWeight: 700, color: 'var(--text-primary)', marginTop: '4px', marginBottom: '8px' }}>
                {preview.space_name}
              </h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Role assignment:</span>
                <span style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: '12px',
                  background: 'rgba(6, 182, 212, 0.15)',
                  border: '1px solid rgba(6, 182, 212, 0.3)',
                  color: 'var(--accent-cyan)',
                  textTransform: 'uppercase',
                  letterSpacing: '0.05em',
                }}>
                  {preview.role}
                </span>
              </div>
            </div>

            {preview.target_email_masked && (
              <div style={{
                padding: '10px 14px',
                background: 'var(--bg-tertiary)',
                border: '1px solid var(--border-subtle)',
                borderRadius: '8px',
                fontSize: '12px',
                color: 'var(--text-secondary)',
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
              }}>
                <span>🔒</span>
                <span>
                  This invite is restricted to <strong style={{ color: 'var(--text-primary)' }}>{preview.target_email_masked}</strong>.
                </span>
              </div>
            )}

            {/* User Intent Action Area */}
            {currentUser ? (
              // Authenticated user confirmation card
              <div style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '14px',
                background: 'var(--bg-tertiary)',
                border: '1px solid var(--border-subtle)',
                padding: '16px',
                borderRadius: '8px',
              }}>
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                  Signed in as: <strong style={{ color: 'var(--text-primary)' }}>{currentUser.email}</strong>
                  {currentUser.display_name && ` (${currentUser.display_name})`}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', paddingTop: '4px' }}>
                  <button
                    type="button"
                    id="btn-confirm-accept-invite"
                    class="btn-primary btn-large"
                    style={{ width: '100%', justifyContent: 'center' }}
                    onClick={handleAcceptInvite}
                    disabled={isProcessing}
                  >
                    {isProcessing ? 'Joining Space...' : `Accept & Join ${preview.space_name}`}
                  </button>
                  <button
                    type="button"
                    class="btn-secondary"
                    style={{ width: '100%', justifyContent: 'center' }}
                    onClick={handleDecline}
                    disabled={isProcessing}
                  >
                    Decline
                  </button>
                </div>
              </div>
            ) : (
              // Unauthenticated visitor with explicit consent button
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <p style={{ fontSize: '12px', color: 'var(--text-secondary)', textAlign: 'center', margin: 0 }}>
                  Sign in with your Google account to confirm identity and accept this workspace invitation.
                </p>
                <button
                  type="button"
                  id="btn-google-accept-invite"
                  class="btn-primary btn-large"
                  style={{ width: '100%', justifyContent: 'center' }}
                  onClick={handleSignInToAccept}
                  disabled={isProcessing}
                >
                  <span>🔑</span>
                  <span>{isProcessing ? 'Connecting...' : 'Sign in with Google to Accept'}</span>
                </button>

                <button
                  type="button"
                  class="btn-secondary"
                  style={{ width: '100%', justifyContent: 'center' }}
                  onClick={handleDecline}
                  disabled={isProcessing}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
