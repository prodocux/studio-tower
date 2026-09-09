import { JSX } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import { authManager } from '../services/auth';
import { ApiClient } from '../services/api';

export function TestAuthPage(): JSX.Element {
  const [customToken, setCustomToken] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authUid, setAuthUid] = useState<string | null>(null);

  const [restoredUid, setRestoredUid] = useState<string | null>(null);
  const [restoredVerified, setRestoredVerified] = useState(false);
  const [restoredError, setRestoredError] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const shouldVerifyRestored = params.get('verify-restored') === '1';

    const unsubscribe = authManager.onAuthStateChanged(async (user) => {
      if (user) {
        setRestoredUid(user.uid);
        if (shouldVerifyRestored) {
          try {
            const api = new ApiClient();
            // Authenticated API client automatically resolves ID token and sends Authorization: Bearer <token>
            const me = await api.getMe();
            const uidMatches = Boolean(me && me.uid === user.uid);
            const emailMatches = Boolean(
              !user.email || !me?.email || me.email.toLowerCase() === user.email.toLowerCase()
            );
            if (uidMatches && emailMatches) {
              setRestoredVerified(true);
            } else {
              setRestoredError(
                `Identity mismatch: Firebase UID '${user.uid}' vs /v1/me UID '${me?.uid}' (emails: '${user.email}' vs '${me?.email}')`
              );
            }
          } catch (err: any) {
            setRestoredError(err?.message || 'Failed to verify restored session with backend API');
          }
        }
      }
    });

    return () => unsubscribe();
  }, []);

  const handleSignIn = async (e: JSX.TargetedEvent<HTMLFormElement, Event>) => {
    e.preventDefault();
    if (!customToken.trim()) {
      setError('Custom token is required');
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const tokenToSubmit = customToken.trim();
      const user = await authManager.signInWithCustomToken(tokenToSubmit);
      setAuthUid(user.uid);
      setCustomToken(''); // Immediately wipe state and DOM to prevent leaking into traces/snapshots
    } catch (err: any) {
      // Avoid raw token or sensitive details in error message
      const rawMsg = err?.message || 'Custom token authentication failed';
      const sanitizedMsg = rawMsg
        .replace(/(ey[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)?)/g, '[REDACTED_TOKEN]')
        .replace(/(token=[^&\s]+)/gi, 'token=[REDACTED]');
      setError(sanitizedMsg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div class="test-auth-page" style={{ padding: '2rem', maxWidth: '600px', margin: '0 auto', fontFamily: 'sans-serif' }}>
      <h2>Staging Test Authentication Control Surface</h2>
      <p style={{ color: '#666', fontSize: '0.9rem' }}>
        This entry point is restricted to test and staging environments.
      </p>

      {restoredVerified && restoredUid && (
        <div
          id="restored-auth-success"
          data-uid={restoredUid}
          style={{ padding: '1rem', background: '#e6ffed', border: '1px solid #52c41a', borderRadius: '4px', marginBottom: '1rem' }}
        >
          <strong>Restored Session Verified!</strong> UID: <code>{restoredUid}</code>
          <div style={{ marginTop: '0.5rem' }}>
            <a href="/" id="btn-goto-app" style={{ color: '#1890ff', textDecoration: 'underline' }}>
              Proceed to StudioTower Workspace
            </a>
          </div>
        </div>
      )}

      {restoredError && (
        <div
          id="restored-auth-error"
          style={{ padding: '1rem', background: '#fff1f0', border: '1px solid #f5222d', borderRadius: '4px', marginBottom: '1rem', color: '#cf1322' }}
        >
          {restoredError}
        </div>
      )}

      {authUid && (
        <div
          id="test-auth-success"
          data-uid={authUid}
          style={{ padding: '1rem', background: '#e6ffed', border: '1px solid #52c41a', borderRadius: '4px', marginBottom: '1rem' }}
        >
          <strong>Authentication Succeeded!</strong> UID: <code>{authUid}</code>
          <div style={{ marginTop: '0.5rem' }}>
            <a href="/" id="btn-goto-app" style={{ color: '#1890ff', textDecoration: 'underline' }}>
              Proceed to StudioTower Workspace
            </a>
          </div>
        </div>
      )}

      {error && (
        <div
          id="test-auth-error"
          style={{ padding: '1rem', background: '#fff1f0', border: '1px solid #f5222d', borderRadius: '4px', marginBottom: '1rem', color: '#cf1322' }}
        >
          {error}
        </div>
      )}

      <form onSubmit={handleSignIn} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div>
          <label htmlFor="staging-custom-token-input" style={{ display: 'block', marginBottom: '0.5rem', fontWeight: 'bold' }}>
            Firebase Custom Token:
          </label>
          <textarea
            id="staging-custom-token-input"
            rows={4}
            value={customToken}
            onInput={(e) => setCustomToken((e.target as HTMLTextAreaElement).value)}
            placeholder="Paste short-lived staging Firebase custom token here..."
            style={{ width: '100%', fontFamily: 'monospace', padding: '0.5rem' }}
          />
        </div>

        <button
          type="submit"
          id="btn-submit-staging-token"
          disabled={loading || !customToken.trim()}
          style={{
            padding: '0.75rem 1.5rem',
            background: '#1890ff',
            color: '#fff',
            border: 'none',
            borderRadius: '4px',
            cursor: 'pointer',
            fontWeight: 'bold',
          }}
        >
          {loading ? 'Signing in with Custom Token...' : 'Authenticate with Custom Token'}
        </button>
      </form>
    </div>
  );
}
