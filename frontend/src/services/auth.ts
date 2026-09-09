import { signal } from '@preact/signals';
import { PendingInviteSession, User } from '../types';
import { api } from './api';

const PENDING_INVITE_KEY = 'studiotower_pending_invite';
const INVITE_TTL_MS = 15 * 60 * 1000; // 15-minute strict TTL

export function setPendingInviteSession(session: { token: string; target_space_name?: string; target_role?: string }): void {
  if (typeof sessionStorage === 'undefined') return;
  const payload: PendingInviteSession = {
    token: session.token,
    issued_at: Date.now(),
    target_space_name: session.target_space_name,
    target_role: session.target_role,
  };
  sessionStorage.setItem(PENDING_INVITE_KEY, JSON.stringify(payload));
}

export function getPendingInviteSession(): PendingInviteSession | null {
  if (typeof sessionStorage === 'undefined') return null;
  const raw = sessionStorage.getItem(PENDING_INVITE_KEY);
  if (!raw) return null;
  try {
    const data: PendingInviteSession = JSON.parse(raw);
    if (!data.token || !data.issued_at) {
      sessionStorage.removeItem(PENDING_INVITE_KEY);
      return null;
    }
    // Strict 15-minute TTL check
    if (Date.now() - data.issued_at > INVITE_TTL_MS) {
      sessionStorage.removeItem(PENDING_INVITE_KEY);
      return null;
    }
    return data;
  } catch {
    sessionStorage.removeItem(PENDING_INVITE_KEY);
    return null;
  }
}

export function clearPendingInviteSession(): void {
  if (typeof sessionStorage === 'undefined') return;
  sessionStorage.removeItem(PENDING_INVITE_KEY);
}

export function getAuthMode(): 'firebase' | 'dev' {
  if (import.meta.env.DEV) {
    if (typeof localStorage !== 'undefined' && localStorage.getItem('studiotower_dev_token')) {
      return 'dev';
    }
    if (import.meta.env.VITE_AUTH_MODE === 'dev') {
      return 'dev';
    }
  }
  return 'firebase';
}

export const authMode = getAuthMode();

export const currentUserSignal = signal<User | null>(null);
export const authLoadingSignal = signal<boolean>(true);

export type AuthOutcome =
  | { status: 'success'; user: User }
  | { status: 'redirecting' }
  | { status: 'cancelled' }
  | { status: 'error'; error: any };

interface FirebaseConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket?: string;
  messagingSenderId?: string;
  appId?: string;
}

export function getValidatedFirebaseConfig(): FirebaseConfig {
  const apiKey = import.meta.env.VITE_FIREBASE_API_KEY;
  const projectId = import.meta.env.VITE_FIREBASE_PROJECT_ID;
  const appId = import.meta.env.VITE_FIREBASE_APP_ID;

  if (!apiKey || !projectId || !appId) {
    const missing = [
      !apiKey ? 'VITE_FIREBASE_API_KEY' : null,
      !projectId ? 'VITE_FIREBASE_PROJECT_ID' : null,
      !appId ? 'VITE_FIREBASE_APP_ID' : null,
    ].filter(Boolean);
    throw new Error(
      `Firebase configuration is missing required environment variables: ${missing.join(', ')}. ` +
      `Ensure frontend/.env or environment variables are properly configured.`
    );
  }

  const authDomain = import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || `${projectId}.firebaseapp.com`;
  const storageBucket = import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || `${projectId}.appspot.com`;
  const messagingSenderId = import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || '';

  return {
    apiKey,
    authDomain,
    projectId,
    storageBucket,
    messagingSenderId,
    appId,
  };
}

let firebaseAppInitialized = false;

const signOutHooks: Array<() => void> = [];

export function registerSignOutHook(hook: () => void): () => void {
  signOutHooks.push(hook);
  return () => {
    const idx = signOutHooks.indexOf(hook);
    if (idx >= 0) signOutHooks.splice(idx, 1);
  };
}

export class AuthManager {
  private devToken: string | null = null;
  private refreshPromise: Promise<string | null> | null = null;
  private authStateInitialized = false;

  constructor() {
    if (import.meta.env.DEV && typeof localStorage !== 'undefined') {
      this.devToken = localStorage.getItem('studiotower_dev_token');
    }
  }

  /**
   * Initializes the application authentication lifecycle during bootstrap.
   */
  async initAuth(): Promise<void> {
    try {
      if (import.meta.env.DEV && getAuthMode() !== 'firebase') {
        const devToken = typeof localStorage !== 'undefined' ? localStorage.getItem('studiotower_dev_token') : null;
        if (devToken) {
          this.setDevToken(devToken);
          try {
            const me = await api.getMe();
            currentUserSignal.value = me;
          } catch {
            this.setDevToken(null);
            currentUserSignal.value = null;
          }
        } else {
          currentUserSignal.value = null;
        }
        authLoadingSignal.value = false;
      } else {
        await this.initFirebase();
      }
    } catch (err: any) {
      console.error('initAuth error:', err);
      authLoadingSignal.value = false;
    }
  }

  async initFirebase(): Promise<any> {
    if (getAuthMode() !== 'firebase') return null;
    try {
      const config = getValidatedFirebaseConfig();
      const { initializeApp, getApps } = await import('firebase/app');
      const { getAuth, onAuthStateChanged } = await import('firebase/auth');

      if (!firebaseAppInitialized && getApps().length === 0) {
        initializeApp(config);
        firebaseAppInitialized = true;
      }

      const auth = getAuth();
      if (!this.authStateInitialized) {
        this.authStateInitialized = true;

        // Check redirect result if user returned from Google redirect sign-in
        try {
          const { getRedirectResult } = await import('firebase/auth');
          const redirectRes = await getRedirectResult(auth);
          if (redirectRes && redirectRes.user) {
            try {
              const me = await api.getMe();
              currentUserSignal.value = me;
            } catch (e) {
              console.warn('Failed to fetch user profile after redirect login:', e);
            }
          }
        } catch (redirErr: any) {
          console.warn('getRedirectResult notice:', redirErr.message);
        }

        // Safety Net Timeout: Dismiss loading screen after 3s if Firebase Auth is waiting
        const fallbackTimer = setTimeout(() => {
          authLoadingSignal.value = false;
        }, 3000);

        onAuthStateChanged(auth, async (fbUser) => {
          clearTimeout(fallbackTimer);
          if (fbUser) {
            try {
              const me = await api.getMe();
              currentUserSignal.value = me;
            } catch (e) {
              console.warn('Failed to fetch user profile:', e);
              currentUserSignal.value = null;
            }
          } else {
            currentUserSignal.value = null;
          }
          authLoadingSignal.value = false;
        });
      }
      return auth;
    } catch (err: any) {
      console.error('Firebase initialization error:', err.message);
      authLoadingSignal.value = false;
      return null;
    }
  }

  private currentAuthAttemptId: string | null = null;
  private activeAuthFlow: 'none' | 'popup' | 'redirect' = 'none';

  /**
   * Initiates Google Authentication with single-flight attempt fencing,
   * 12-second watchdog timeout, and popup-blocked fallback to redirect flow.
   * Deliberately ignores popup-closed-by-user without triggering unwanted redirects.
   *
   * Note on browser popup lifecycle:
   * Native browser popup promises cannot be forcibly cancelled or aborted via JavaScript.
   * If a watchdog timeout or fallback redirect triggers, this state machine updates
   * both currentAuthAttemptId and activeAuthFlow so that any late resolution from the
   * underlying popup is safely ignored and discarded.
   */
  async signInWithGoogle(mode: 'popup' | 'redirect' = 'popup'): Promise<AuthOutcome> {
    if (getAuthMode() !== 'firebase') {
      console.warn('Google sign-in is only available in Firebase auth mode');
      return { status: 'error', error: new Error('Google sign-in is only available in Firebase auth mode') };
    }

    const { getAuth, GoogleAuthProvider, signInWithPopup, signInWithRedirect } = await import('firebase/auth');
    const auth = getAuth();
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({ prompt: 'select_account' });

    const attemptId = `attempt_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
    this.currentAuthAttemptId = attemptId;
    authLoadingSignal.value = true;

    if (mode === 'redirect') {
      this.activeAuthFlow = 'redirect';
      await signInWithRedirect(auth, provider);
      return { status: 'redirecting' };
    }

    // Popup flow with 12s watchdog timer & popup-blocked fallback
    this.activeAuthFlow = 'popup';
    let watchdogTimer: any = null;
    try {
      const popupPromise = signInWithPopup(auth, provider);
      const timeoutPromise = new Promise<never>((_, reject) => {
        watchdogTimer = setTimeout(() => {
          const timeoutErr: any = new Error('Google Auth popup timed out');
          timeoutErr.code = 'auth/popup-watchdog-timeout';
          reject(timeoutErr);
        }, 12000);
      });

      const userCredential = await Promise.race([popupPromise, timeoutPromise]);
      clearTimeout(watchdogTimer);

      // Verify attempt and flow state: discard late resolution if superseded or redirected
      if (this.currentAuthAttemptId !== attemptId || this.activeAuthFlow !== 'popup') {
        return { status: 'cancelled' };
      }

      if (userCredential.user) {
        try {
          const me = await api.getMe();
          currentUserSignal.value = me;
          this.activeAuthFlow = 'none';
          return { status: 'success', user: me };
        } catch (e: any) {
          console.warn('Failed to fetch user profile after popup login:', e);
          this.activeAuthFlow = 'none';
          return { status: 'error', error: e };
        }
      }
      this.activeAuthFlow = 'none';
      return { status: 'cancelled' };
    } catch (err: any) {
      clearTimeout(watchdogTimer);
      if (this.currentAuthAttemptId !== attemptId) {
        // Attempt was superseded
        return { status: 'cancelled' };
      }

      if (err.code === 'auth/popup-closed-by-user') {
        // User deliberately closed the popup: do NOT redirect, cancel loading cleanly
        console.info('Google login popup closed by user.');
        this.activeAuthFlow = 'none';
        authLoadingSignal.value = false;
        return { status: 'cancelled' };
      }

      if (err.code === 'auth/popup-blocked' || err.code === 'auth/popup-watchdog-timeout') {
        console.warn(`Popup ${err.code === 'auth/popup-blocked' ? 'blocked' : 'timed out'}; falling back to redirect flow.`);
        this.activeAuthFlow = 'redirect';
        this.currentAuthAttemptId = null;
        await signInWithRedirect(auth, provider);
        return { status: 'redirecting' };
      }

      console.error('Google sign-in error:', err);
      this.activeAuthFlow = 'none';
      authLoadingSignal.value = false;
      return { status: 'error', error: err };
    } finally {
      if (this.currentAuthAttemptId === attemptId && this.activeAuthFlow === 'none') {
        authLoadingSignal.value = false;
      }
    }
  }

  /**
   * Signs in using email and password with Firebase Authentication.
   * Enables judge accounts and testing without personal Google credentials.
   */
  async signInWithEmailPassword(email: string, password: string): Promise<AuthOutcome> {
    const attemptId = ++this.currentAuthAttemptId;
    this.activeAuthFlow = 'popup';
    authLoadingSignal.value = true;
    try {
      if (getAuthMode() === 'firebase') {
        const { getAuth, signInWithEmailAndPassword } = await import('firebase/auth');
        const auth = getAuth();
        const userCredential = await signInWithEmailAndPassword(auth, email.trim(), password);
        let me: User | null = null;
        try {
          me = await api.getMe();
          currentUserSignal.value = me;
        } catch (e: any) {
          console.warn('Failed to fetch user profile after email sign in:', e);
        }
        this.activeAuthFlow = 'none';
        authLoadingSignal.value = false;
        return {
          status: 'success',
          user: me || {
            uid: userCredential.user.uid,
            email: userCredential.user.email || email,
            display_name: userCredential.user.displayName || email.split('@')[0],
          },
        };
      } else {
        throw new Error('Email/password sign-in requires Firebase auth mode');
      }
    } catch (err: any) {
      console.error('Email sign-in error:', err);
      this.activeAuthFlow = 'none';
      authLoadingSignal.value = false;
      return { status: 'error', error: err };
    } finally {
      if (this.currentAuthAttemptId === attemptId && this.activeAuthFlow === 'none') {
        authLoadingSignal.value = false;
      }
    }
  }

  /**
   * Authoritatively signs in using an authentic Firebase Custom Token.
   * Leverages the application-bundled Firebase SDK.
   */
  async signInWithCustomToken(customToken: string): Promise<any> {
    if (getAuthMode() !== 'firebase') {
      throw new Error('signInWithCustomToken is only available in Firebase auth mode');
    }
    const { getAuth, signInWithCustomToken } = await import('firebase/auth');
    const auth = getAuth();
    authLoadingSignal.value = true;
    try {
      const userCredential = await signInWithCustomToken(auth, customToken);
      let me = null;
      if (userCredential.user) {
        try {
          me = await api.getMe();
          currentUserSignal.value = me;
        } catch (e: any) {
          console.warn('Failed to fetch user profile after custom token sign in:', e);
        }
      }
      return {
        uid: userCredential.user.uid,
        email: userCredential.user.email,
        profile: me,
      };
    } finally {
      authLoadingSignal.value = false;
    }
  }

  setDevToken(token: string | null): void {
    this.devToken = token;
    if (import.meta.env.DEV && typeof localStorage !== 'undefined') {
      if (token) {
        localStorage.setItem('studiotower_dev_token', token);
      } else {
        localStorage.removeItem('studiotower_dev_token');
      }
    }
  }

  async getToken(forceRefresh: boolean = false): Promise<string | null> {
    if (import.meta.env.DEV && getAuthMode() === 'dev') {
      const localDevToken = typeof localStorage !== 'undefined' ? localStorage.getItem('studiotower_dev_token') : null;
      return localDevToken || this.devToken;
    }

    try {
      const { getAuth } = await import('firebase/auth');
      const auth = getAuth();
      if (auth.currentUser) {
        return await auth.currentUser.getIdToken(forceRefresh);
      }
    } catch (err) {
      console.warn('Could not get Firebase token:', err);
    }

    // Strictly DO NOT fallback to dev tokens in Firebase mode
    return null;
  }

  async handle401Refresh(): Promise<string | null> {
    if (this.refreshPromise) {
      return this.refreshPromise;
    }

    this.refreshPromise = (async () => {
      try {
        if (getAuthMode() === 'firebase') {
          const { getAuth } = await import('firebase/auth');
          const auth = getAuth();
          if (auth.currentUser) {
            return await auth.currentUser.getIdToken(true);
          }
        }
        return null;
      } finally {
        this.refreshPromise = null;
      }
    })();

    return this.refreshPromise;
  }

  async signOut(): Promise<void> {
    clearPendingInviteSession();
    if (getAuthMode() === 'firebase') {
      try {
        const { getAuth, signOut } = await import('firebase/auth');
        const auth = getAuth();
        await signOut(auth);
      } catch (err) {
        console.warn('Firebase sign out error:', err);
      }
    } else if (import.meta.env.DEV) {
      this.setDevToken(null);
    }
    currentUserSignal.value = null;
    for (const hook of signOutHooks) {
      try {
        hook();
      } catch (e) {
        console.warn('Sign out hook failed:', e);
      }
    }
  }
}

export const authManager = new AuthManager();
