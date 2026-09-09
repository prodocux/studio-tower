import { describe, test, expect, beforeEach, vi } from 'vitest';
import {
  activeSpaceIdSignal,
  activeTagSignal,
  spaceContextSignal,
  messagesSignal,
  contextStateSignal,
  messagesStateSignal,
  requestSequencer,
} from '../src/services/store';
import { api } from '../src/services/api';
import { currentUserSignal } from '../src/services/auth';
import { loadSpaceData, clearSpaceCache } from '../src/services/spaceLoader';

describe('Stale Generation Guard & Out-of-Order Async Response Isolation', () => {
  beforeEach(() => {
    clearSpaceCache();
    currentUserSignal.value = {
      uid: 'u_user_1',
      email: 'user1@studiotower.ai',
      display_name: 'User One',
    };
    activeSpaceIdSignal.value = null;
    activeTagSignal.value = 'all';
    spaceContextSignal.value = null;
    messagesSignal.value = [];
    contextStateSignal.value = 'idle';
    messagesStateSignal.value = 'idle';
    requestSequencer.reset();
    vi.restoreAllMocks();
  });

  test('Production loadSpaceData discards un-aborted out-of-order response from Space A after switching to Space B', async () => {
    let resolveSpaceAContext: (val: any) => void;
    let resolveSpaceAMessages: (val: any) => void;

    // 1. Deferred promises for Space Alpha that intentionally IGNORE AbortSignal
    const spaceAPromiseContext = new Promise((resolve) => {
      resolveSpaceAContext = resolve;
    });
    const spaceAPromiseMessages = new Promise((resolve) => {
      resolveSpaceAMessages = resolve;
    });

    vi.spyOn(api, 'getSpaceContext').mockImplementation((spaceId) => {
      if (spaceId === 'space_alpha') return spaceAPromiseContext as any;
      if (spaceId === 'space_beta') {
        return Promise.resolve({
          space: { space_id: 'space_beta', name: 'Space Beta', kind: 'production' },
          current_user_role: 'owner',
          member_count: 2,
          capabilities: { can_invite: true, can_manage_members: true },
        }) as any;
      }
      return Promise.reject(new Error('Unknown space'));
    });

    vi.spyOn(api, 'listMessagesPage').mockImplementation((spaceId) => {
      if (spaceId === 'space_alpha') return spaceAPromiseMessages as any;
      if (spaceId === 'space_beta') {
        return Promise.resolve({
          items: [
            { message_id: 'msg_beta_1', content: 'Message from Space Beta', space_id: 'space_beta' },
          ],
          next_cursor: null,
          has_more: false,
        }) as any;
      }
      return Promise.resolve({ items: [], next_cursor: null, has_more: false }) as any;
    });

    vi.spyOn(api, 'listFiles').mockResolvedValue([] as any);
    vi.spyOn(api, 'listRuns').mockResolvedValue([] as any);
    vi.spyOn(api, 'getLineage').mockResolvedValue(null as any);

    // 2. Start Space Alpha Fetch using PRODUCTION loadSpaceData
    activeSpaceIdSignal.value = 'space_alpha';
    activeTagSignal.value = 'all';

    const promiseAlpha = loadSpaceData('space_alpha', 'all');

    // 3. Switch active space signals to Space Beta while Space Alpha promises are pending
    activeSpaceIdSignal.value = 'space_beta';
    activeTagSignal.value = 'all';

    const promiseBeta = loadSpaceData('space_beta', 'all');
    await promiseBeta;

    // Verify Space Beta state is active
    expect(spaceContextSignal.value?.space.space_id).toBe('space_beta');
    expect(spaceContextSignal.value?.space.name).toBe('Space Beta');
    expect(messagesSignal.value[0]?.content).toBe('Message from Space Beta');

    // 4. Fulfill Space Alpha's delayed promises (simulating un-aborted late response arrival)
    resolveSpaceAContext!({
      space: { space_id: 'space_alpha', name: 'Space Alpha Stale', kind: 'production' },
      current_user_role: 'member',
      member_count: 5,
      capabilities: { can_invite: false, can_manage_members: false },
    });

    resolveSpaceAMessages!({
      items: [
        { message_id: 'msg_alpha_stale', content: 'STALE MESSAGE FROM ALPHA', space_id: 'space_alpha' },
      ],
      next_cursor: null,
      has_more: false,
    });

    await promiseAlpha;

    // 5. Strict Assertion: Production loadSpaceData MUST PRESERVE Space Beta data in signals!
    expect(spaceContextSignal.value?.space.space_id).toBe('space_beta');
    expect(spaceContextSignal.value?.space.name).toBe('Space Beta');
    expect(messagesSignal.value.length).toBe(1);
    expect(messagesSignal.value[0].content).toBe('Message from Space Beta');
  });
});
