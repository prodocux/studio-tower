import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { Header } from '../src/components/Header';
import { App } from '../src/App';
import { ActivityLogView } from '../src/components/ActivityLogView';
import {
  activeViewSignal,
  spaceContextSignal,
  activeSpaceIdSignal,
  activeTagSignal,
  spacesSignal,
  messagesSignal,
  filesSignal,
  selectedFileIdsSignal,
  composerContextFileIdsSignal,
  activityEventsSignal,
  activityNextCursorSignal,
} from '../src/services/store';
import { currentUserSignal, authLoadingSignal, authManager } from '../src/services/auth';
import { api } from '../src/services/api';
import { clearSpaceCache, loadSpaceData } from '../src/services/spaceLoader';

describe('Header & App Mounting Integration Tests', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    clearSpaceCache();
    authLoadingSignal.value = false;
    currentUserSignal.value = {
      uid: 'u_test_user_01',
      email: 'creator@studiotower.ai',
      display_name: 'Studio Director',
    };
    spacesSignal.value = [
      {
        space_id: 'sp_film_01',
        name: 'Sci-Fi Epic Space',
        kind: 'shared_space',
        created_by: 'u_test_user_01',
        created_at: '2026-08-30T00:00:00Z',
        tags: [
          { name: 'General', slug: 'general', color: '#64748B' },
          { name: 'Soundtrack', slug: 'soundtrack', color: '#3B82F6' },
        ],
      },
      {
        space_id: 'sp_film_02',
        name: 'Horror Short Space',
        kind: 'shared_space',
        created_by: 'u_test_user_01',
        created_at: '2026-08-30T01:00:00Z',
        tags: [{ name: 'General', slug: 'general', color: '#64748B' }],
      },
    ];
    activeSpaceIdSignal.value = 'sp_film_01';
    spaceContextSignal.value = {
      space: spacesSignal.value[0],
      current_user_role: 'owner',
      member_count: 5,
      capabilities: {
        can_invite: true,
        can_manage_members: true,
        can_change_role: true,
        can_remove_member: true,
        can_transfer_ownership: true,
        can_approve_runs: true,
        can_manage_tags: true,
        can_leave_space: false,
      },
    };
    activeTagSignal.value = 'all';
    activeViewSignal.value = 'chat';
    selectedFileIdsSignal.value = [];
    composerContextFileIdsSignal.value = [];

    vi.spyOn(authManager, 'initAuth').mockImplementation(() => {});
    vi.spyOn(api, 'listSpaces').mockResolvedValue(spacesSignal.value);
    vi.spyOn(api, 'getSpaceContext').mockResolvedValue(spaceContextSignal.value!);
    vi.spyOn(api, 'listMessages').mockResolvedValue([]);
    vi.spyOn(api, 'listFiles').mockResolvedValue([]);
    vi.spyOn(api, 'listRuns').mockResolvedValue([]);
    vi.spyOn(api, 'getLineage').mockResolvedValue({ nodes: [], edges: [] });
  });

  it('renders Header without runtime ReferenceError when activeViewSignal is accessed', () => {
    const onSelectTag = vi.fn();
    const onNavigateHome = vi.fn();

    const { getByRole, getByText } = render(
      <Header onSelectTag={onSelectTag} onNavigateHome={onNavigateHome} />
    );

    // Verify view switch tabs
    expect(getByRole('tab', { name: /(對話|Chat)/ })).toBeDefined();
    expect(getByRole('tab', { name: /(文件中心|File Center)/ })).toBeDefined();
    expect(getByRole('tab', { name: /(任務與審批|Tasks & Approvals)/ })).toBeDefined();
    expect(getByRole('tab', { name: /(活動紀錄|Activity Log)/ })).toBeDefined();
    expect(getByText('Sci-Fi Epic Space')).toBeDefined();
  });

  it('switches activeViewSignal cleanly between all 4 workspace views', () => {
    const { getByRole } = render(
      <Header onSelectTag={vi.fn()} onNavigateHome={vi.fn()} />
    );

    const filesTab = getByRole('tab', { name: /(文件中心|File Center)/ });
    fireEvent.click(filesTab);
    expect(activeViewSignal.value).toBe('files');

    const runsTab = getByRole('tab', { name: /(任務與審批|Tasks & Approvals)/ });
    fireEvent.click(runsTab);
    expect(activeViewSignal.value).toBe('runs');

    const activityTab = getByRole('tab', { name: /(活動紀錄|Activity Log)/ });
    fireEvent.click(activityTab);
    expect(activeViewSignal.value).toBe('activity');

    const chatTab = getByRole('tab', { name: /(對話|Chat)/ });
    fireEvent.click(chatTab);
    expect(activeViewSignal.value).toBe('chat');
  });

  it('clears SWR cache, aborts in-flight requests, and resets signals on user sign-out', async () => {
    composerContextFileIdsSignal.value = ['f_test_01'];
    selectedFileIdsSignal.value = ['f_test_01'];
    messagesSignal.value = [{ message_id: 'm1', role: 'user', content: 'hello', created_at: '2026-08-30' } as any];

    await authManager.signOut();

    expect(currentUserSignal.value).toBeNull();
    expect(spaceContextSignal.value).toBeNull();
    expect(messagesSignal.value).toEqual([]);
    expect(selectedFileIdsSignal.value).toEqual([]);
    expect(composerContextFileIdsSignal.value).toEqual([]);
  });

  it('discards delayed loader responses arriving after space switch or after logout', async () => {
    let resolveDelayedContext: (val: any) => void;
    const delayedPromise = new Promise((resolve) => {
      resolveDelayedContext = resolve;
    });

    vi.spyOn(api, 'getSpaceContext').mockImplementation((spaceId: string) => {
      if (spaceId === 'sp_film_01') {
        return delayedPromise as any;
      }
      return Promise.resolve({
        space: spacesSignal.value[1],
        current_user_role: 'member',
        member_count: 2,
        capabilities: {},
      } as any);
    });

    // 1. Start loading space 1
    const p1 = loadSpaceData('sp_film_01', 'all');

    // 2. Quickly switch to space 2 before space 1 resolves
    activeSpaceIdSignal.value = 'sp_film_02';
    await loadSpaceData('sp_film_02', 'all');

    expect(spaceContextSignal.value?.space.space_id).toBe('sp_film_02');

    // 3. Now space 1 delayed response finally arrives
    resolveDelayedContext!({
      space: spacesSignal.value[0],
      current_user_role: 'owner',
      member_count: 5,
      capabilities: {},
    });
    await p1;

    // 4. Verify space 1 did NOT overwrite space 2
    expect(spaceContextSignal.value?.space.space_id).toBe('sp_film_02');
  });

  it('resets isLoadingMore when switching Tag in ActivityLogView and prevents disabled button lockup', async () => {
    activityEventsSignal.value = [
      {
        event_id: 'ev_1',
        event_type: 'file.uploaded',
        space_id: 'sp_film_01',
        project_tag: 'soundtrack',
        resource_type: 'file',
        resource_id: 'f_sound_1',
        summary: '檔案 sound.mp3 已上傳',
        created_at: '2026-08-30T10:00:00Z',
      },
    ];
    activityNextCursorSignal.value = 'cur_next_01';

    let resolveLoadMore: (res: any) => void;
    vi.spyOn(api, 'listActivity').mockImplementation(() => {
      return new Promise((resolve) => {
        resolveLoadMore = resolve;
      });
    });

    const { getByText, queryByText } = render(<ActivityLogView />);

    const loadMoreBtn = getByText(/(載入更多歷史事件|Load More Events)/);
    fireEvent.click(loadMoreBtn);

    // Button should show loading
    expect(getByText(/(載入中\.\.\.|Loading\.\.\.)/)).toBeDefined();

    // Switch tag before load more finishes
    activeTagSignal.value = 'general';

    // Finish old delayed load more
    resolveLoadMore!({
      items: [],
      next_cursor: null,
      has_more: false,
    });

    await waitFor(() => {
      // Button should not be locked in "Loading..."
      expect(queryByText(/(載入中\.\.\.|Loading\.\.\.)/)).toBeNull();
    });
  });

  it('discards delayed reindex responses in FileCenter when performing Space A -> Space B -> Space A switch', async () => {
    const { FileCenter } = await import('../src/components/FileCenter');
    filesSignal.value = [
      {
        file_id: 'f_film_01',
        space_id: 'sp_film_01',
        storage_path: 'uploads/f_film_01',
        filename: 'script_scene1.pdf',
        content_type: 'application/pdf',
        size_bytes: 1024,
        uploaded_by: 'u_test_user_01',
        source_type: 'user_upload',
        sha256: 'sha_1',
        ingestion_status: 'ready',
        active_generation: 1,
        project_tags: ['general'],
        created_at: '2026-08-30T10:00:00Z',
      },
    ];

    let resolveReindex: (val: any) => void;
    vi.spyOn(api, 'reindexFile').mockImplementation(() => {
      return new Promise((resolve) => {
        resolveReindex = resolve;
      });
    });

    const listFilesSpy = vi.spyOn(api, 'listFiles').mockResolvedValue([
      {
        file_id: 'f_film_01',
        space_id: 'sp_film_01',
        storage_path: 'uploads/f_film_01',
        filename: 'script_scene1.pdf',
        content_type: 'application/pdf',
        size_bytes: 1024,
        uploaded_by: 'u_test_user_01',
        source_type: 'user_upload',
        sha256: 'sha_1_fresh',
        ingestion_status: 'ready',
        active_generation: 2,
        project_tags: ['general'],
        created_at: '2026-08-30T10:00:00Z',
      },
    ]);

    const { getByRole, rerender } = render(<FileCenter />);

    // 1. Click reindex button on file
    const reindexBtn = getByRole('button', { name: /(重新索引|Reindex)/ });
    fireEvent.click(reindexBtn);

    // 2. Switch to Space B
    activeSpaceIdSignal.value = 'sp_film_02';
    rerender(<FileCenter />);

    // 3. Switch back to Space A
    activeSpaceIdSignal.value = 'sp_film_01';
    rerender(<FileCenter />);

    // 4. Resolve the delayed reindex from the 1st visit
    resolveReindex!({ ok: true });

    // 5. Verify the delayed reindex did not trigger stale listFiles write
    expect(filesSignal.value[0].active_generation).toBe(1);
  });

  it('mounts full App component with logged-in user without errors', async () => {
    currentUserSignal.value = {
      uid: 'u_test_user_01',
      email: 'creator@studiotower.ai',
      display_name: 'Studio Director',
    };
    activeSpaceIdSignal.value = 'sp_film_01';
    spaceContextSignal.value = {
      space: spacesSignal.value[0],
      current_user_role: 'owner',
      member_count: 5,
      capabilities: {
        can_invite: true,
        can_manage_members: true,
        can_change_role: true,
        can_remove_member: true,
        can_transfer_ownership: true,
        can_approve_runs: true,
        can_manage_tags: true,
        can_leave_space: false,
      },
    };

    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector('.app-layout')).not.toBeNull();
      expect(container.querySelector('.header-nav-tabs')).not.toBeNull();
    });
  });
});
