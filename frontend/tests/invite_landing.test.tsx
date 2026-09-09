import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { InviteLandingPage } from '../src/components/InviteLandingPage';
import {
  setPendingInviteSession,
  getPendingInviteSession,
  clearPendingInviteSession,
  currentUserSignal,
  authManager,
} from '../src/services/auth';
import { api } from '../src/services/api';

describe('Invite Landing & Session Management', () => {
  beforeEach(() => {
    sessionStorage.clear();
    currentUserSignal.value = null;
    vi.restoreAllMocks();
  });

  describe('SessionStorage Invite Management', () => {
    it('stores and retrieves pending invite session within 15-minute TTL', () => {
      setPendingInviteSession({
        token: 'tok_active_abc',
        target_space_name: 'Project Bersama',
        target_role: 'coordinator',
      });

      const session = getPendingInviteSession();
      expect(session).not.toBeNull();
      expect(session?.token).toBe('tok_active_abc');
      expect(session?.target_space_name).toBe('Project Bersama');
      expect(session?.target_role).toBe('coordinator');
    });

    it('expires and purges pending invite session if older than 15 minutes', () => {
      // Manually store with timestamp 16 minutes ago
      const expiredPayload = {
        token: 'tok_expired_old',
        issued_at: Date.now() - 16 * 60 * 1000,
        target_space_name: 'Old Space',
      };
      sessionStorage.setItem('studiotower_pending_invite', JSON.stringify(expiredPayload));

      const session = getPendingInviteSession();
      expect(session).toBeNull();
      expect(sessionStorage.getItem('studiotower_pending_invite')).toBeNull();
    });

    it('clears pending invite session explicitly', () => {
      setPendingInviteSession({ token: 'tok_to_clear' });
      expect(getPendingInviteSession()).not.toBeNull();

      clearPendingInviteSession();
      expect(getPendingInviteSession()).toBeNull();
    });
  });

  describe('InviteLandingPage Component', () => {
    it('renders preview info successfully for an active invite', async () => {
      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'Stunt Choreography Unit',
        role: 'coordinator',
        target_email_masked: 'd***@cinema.org',
        status: 'active',
      });

      const { getByText } = render(
        <InviteLandingPage token="tok_123" onJoined={() => {}} onCancel={() => {}} />
      );

      await waitFor(() => {
        expect(getByText('Stunt Choreography Unit')).toBeDefined();
        expect(getByText('coordinator')).toBeDefined();
        expect(getByText('d***@cinema.org')).toBeDefined();
        expect(getByText('Sign in with Google to Accept')).toBeDefined();
      });
    });

    it('renders error state with machine-readable code on 410 Gone', async () => {
      const error: any = new Error('This invitation has expired.');
      error.status = 410;
      error.detail = { code: 'INVITE_EXPIRED', message: 'This invitation has expired.' };
      vi.spyOn(api, 'getInvitePreview').mockRejectedValue(error);

      const { getByText } = render(
        <InviteLandingPage token="tok_expired" onJoined={() => {}} onCancel={() => {}} />
      );

      await waitFor(() => {
        expect(getByText('Invitation Unavailable')).toBeDefined();
        expect(getByText('INVITE_EXPIRED')).toBeDefined();
        expect(getByText('This invitation has expired.')).toBeDefined();
      });
    });

    it('unauthenticated user clicking accept triggers signInWithGoogle and persists intent', async () => {
      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'Post Sound Stage',
        role: 'member',
        status: 'active',
      });
      const signInSpy = vi.spyOn(authManager, 'signInWithGoogle').mockResolvedValue({
        status: 'success',
        user: { uid: 'u1', email: 'u@example.com' } as any,
      });

      const { getByText } = render(
        <InviteLandingPage token="tok_intent_test" onJoined={() => {}} onCancel={() => {}} />
      );

      await waitFor(() => {
        expect(getByText('Sign in with Google to Accept')).toBeDefined();
      });

      const btn = getByText('Sign in with Google to Accept');
      fireEvent.click(btn);

      // Verify authManager.signInWithGoogle called
      expect(signInSpy).toHaveBeenCalledWith('popup');
    });

    it('unauthenticated user cancelling Google sign-in purges pending invite session', async () => {
      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'Post Sound Stage',
        role: 'member',
        status: 'active',
      });
      vi.spyOn(authManager, 'signInWithGoogle').mockResolvedValue({ status: 'cancelled' });

      const { getByText } = render(
        <InviteLandingPage token="tok_cancel_test" onJoined={() => {}} onCancel={() => {}} />
      );

      await waitFor(() => {
        expect(getByText('Sign in with Google to Accept')).toBeDefined();
      });

      const btn = getByText('Sign in with Google to Accept');
      fireEvent.click(btn);

      await waitFor(() => {
        expect(getPendingInviteSession()).toBeNull();
      });
    });

    it('unauthenticated user experiencing auth error purges pending invite session', async () => {
      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'Post Sound Stage',
        role: 'member',
        status: 'active',
      });
      vi.spyOn(authManager, 'signInWithGoogle').mockResolvedValue({ status: 'error', error: new Error('Network error') });

      const { getByText } = render(
        <InviteLandingPage token="tok_err_test" onJoined={() => {}} onCancel={() => {}} />
      );

      await waitFor(() => {
        expect(getByText('Sign in with Google to Accept')).toBeDefined();
      });

      const btn = getByText('Sign in with Google to Accept');
      fireEvent.click(btn);

      await waitFor(() => {
        expect(getPendingInviteSession()).toBeNull();
      });
    });

    it('authenticated user renders confirmation and accepts invite', async () => {
      currentUserSignal.value = {
        uid: 'user_director',
        email: 'director@production.org',
        display_name: 'Director Bob',
      };

      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'Art Department',
        role: 'coordinator',
        status: 'active',
      });

      const joinedSpace = {
        space_id: 'space_art_dept',
        name: 'Art Department',
        kind: 'shared_space' as const,
        created_by: 'owner_1',
        created_at: new Date().toISOString(),
        tags: [],
      };
      const acceptSpy = vi.spyOn(api, 'acceptInvite').mockResolvedValue(joinedSpace);
      let joinedCalled = false;

      const { getByText } = render(
        <InviteLandingPage
          token="tok_auth_accept"
          onJoined={(space) => {
            joinedCalled = true;
            expect(space.space_id).toBe('space_art_dept');
          }}
          onCancel={() => {}}
        />
      );

      await waitFor(() => {
        expect(getByText('director@production.org')).toBeDefined();
        expect(getByText('Accept & Join Art Department')).toBeDefined();
      });

      const acceptBtn = getByText('Accept & Join Art Department');
      fireEvent.click(acceptBtn);

      await waitFor(() => {
        expect(acceptSpy).toHaveBeenCalledWith('tok_auth_accept');
        expect(joinedCalled).toBe(true);
      });
    });

    it('declining clears pending invite and triggers onCancel', async () => {
      setPendingInviteSession({ token: 'tok_decline' });
      vi.spyOn(api, 'getInvitePreview').mockResolvedValue({
        space_name: 'VFX Unit',
        role: 'member',
        status: 'active',
      });

      let cancelled = false;
      const { getByText } = render(
        <InviteLandingPage
          token="tok_decline"
          onJoined={() => {}}
          onCancel={() => { cancelled = true; }}
        />
      );

      await waitFor(() => {
        expect(getByText('Cancel')).toBeDefined();
      });

      const cancelBtn = getByText('Cancel');
      fireEvent.click(cancelBtn);

      expect(cancelled).toBe(true);
      expect(getPendingInviteSession()).toBeNull();
    });
  });
});
