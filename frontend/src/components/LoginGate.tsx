import { JSX } from 'preact';
import { useState } from 'preact/hooks';
import { getAuthMode, authManager, currentUserSignal } from '../services/auth';
import { api } from '../services/api';
import { showToast } from '../services/toast';

export function LoginGate(): JSX.Element {
  const [isLoading, setIsLoading] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isSubmittingEmail, setIsSubmittingEmail] = useState(false);

  const handleGoogleLogin = async () => {
    setIsLoading(true);
    try {
      if (getAuthMode() === 'firebase') {
        const outcome = await authManager.signInWithGoogle('popup');
        if (outcome.status === 'success') {
          showToast(`Welcome, ${outcome.user.display_name || outcome.user.email}!`, 'success');
        } else if (outcome.status === 'error') {
          showToast(`Login failed: ${outcome.error?.message || 'Authentication error'}`, 'error');
        }
        // 'cancelled' and 'redirecting' reset state cleanly without error toast
      } else if (import.meta.env.DEV) {
        const { loginAsDevUser } = await import('../services/devAuth');
        await loginAsDevUser('steven_01', 'steven@example.com', 'Steven Wu');
      }
    } catch (err: any) {
      showToast(`Login failed: ${err.message}`, 'error');
    } finally {
      setIsLoading(false);
    }
  };

  const handleEmailLogin = async (e: Event) => {
    e.preventDefault();
    if (!email.trim() || !password) {
      showToast('Please enter both email and password', 'warning');
      return;
    }
    setIsSubmittingEmail(true);
    try {
      if (getAuthMode() === 'firebase') {
        const outcome = await authManager.signInWithEmailPassword(email.trim(), password);
        if (outcome.status === 'success') {
          showToast(`Welcome, ${outcome.user.display_name || outcome.user.email}!`, 'success');
        } else if (outcome.status === 'error') {
          showToast(`Sign in failed: ${outcome.error?.message || 'Invalid email or password'}`, 'error');
        }
      } else if (import.meta.env.DEV) {
        const { loginAsDevUser } = await import('../services/devAuth');
        await loginAsDevUser('judge_demo', email.trim(), email.split('@')[0]);
      }
    } catch (err: any) {
      showToast(`Sign in failed: ${err.message}`, 'error');
    } finally {
      setIsSubmittingEmail(false);
    }
  };

  const handleDevSwitch = async (uid: string, email: string, name: string) => {
    if (import.meta.env.DEV && getAuthMode() === 'dev') {
      setIsLoading(true);
      const { loginAsDevUser } = await import('../services/devAuth');
      await loginAsDevUser(uid, email, name);
      setIsLoading(false);
    }
  };

  return (
    <div class="login-gate-container">
      <div class="login-card">
        <div class="logo-badge">
          <span class="logo-icon">🎬</span>
          <h1 class="app-title">StudioTower</h1>
        </div>
        <p class="app-tagline">Observable Production Control for Film Prep</p>

        <div class="auth-btn-group">
          <button
            id="btn-google-login"
            class="btn-primary btn-large"
            onClick={handleGoogleLogin}
            disabled={isLoading || isSubmittingEmail}
          >
            <span>🔑</span> {isLoading ? 'Authenticating...' : 'Sign in with Google'}
          </button>
        </div>

        <div class="login-divider">
          <span>or sign in with credentials</span>
        </div>

        <form class="email-login-form" onSubmit={handleEmailLogin}>
          <div class="form-group-login">
            <label class="login-label" htmlFor="input-email">Email</label>
            <input
              id="input-email"
              type="email"
              class="login-input"
              placeholder="judge-owner@studiotower.app"
              value={email}
              onInput={(e) => setEmail((e.target as HTMLInputElement).value)}
              disabled={isLoading || isSubmittingEmail}
              required
            />
          </div>
          <div class="form-group-login">
            <label class="login-label" htmlFor="input-password">Password</label>
            <input
              id="input-password"
              type="password"
              class="login-input"
              placeholder="••••••••"
              value={password}
              onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
              disabled={isLoading || isSubmittingEmail}
              required
            />
          </div>
          <button
            id="btn-email-login"
            type="submit"
            class="btn-secondary btn-large"
            style={{ width: '100%', marginTop: '4px', justifyContent: 'center' }}
            disabled={isLoading || isSubmittingEmail}
          >
            {isSubmittingEmail ? 'Signing in...' : 'Sign In with Email'}
          </button>
        </form>

        {/* Dev Switcher: Rendered strictly during development in dev auth mode */}
        {import.meta.env.DEV && getAuthMode() === 'dev' && (
          <div class="dev-auth-box">
            <span class="dev-select-label">Quick Dev Switcher (Multi-Role Test Accounts)</span>
            <div class="dev-user-pills">
              <button
                class="pill-btn"
                onClick={() => handleDevSwitch('steven_01', 'steven@example.com', 'Steven (Coordinator)')}
              >
                Steven (Coord)
              </button>
              <button
                class="pill-btn"
                onClick={() => handleDevSwitch('alice_02', 'alice@example.com', 'Alice (Owner)')}
              >
                Alice (Owner)
              </button>
              <button
                class="pill-btn"
                onClick={() => handleDevSwitch('bob_03', 'bob@example.com', 'Bob (Member)')}
              >
                Bob (Member)
              </button>
              <button
                class="pill-btn"
                onClick={() => handleDevSwitch('carol_04', 'carol@example.com', 'Carol (Member)')}
              >
                Carol (Member)
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
