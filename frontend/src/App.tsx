import { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { currentUserSignal, authLoadingSignal, authManager, getPendingInviteSession } from './services/auth';
import {
  activeViewSignal,
  spacesSignal,
  activeSpaceIdSignal,
  activeTagSignal,
  contextStateSignal,
  sidebarOpenSignal,
  rightPanelOpenSignal,
  themeSignal,
} from './services/store';
import { api } from './services/api';
import { LoginGate } from './components/LoginGate';
import { InviteLandingPage } from './components/InviteLandingPage';
import { Sidebar } from './components/Sidebar';
import { Header } from './components/Header';
import { ChatArea } from './components/ChatArea';
import { RightPanel } from './components/RightPanel';
import { ResponsiveDrawer } from './components/primitives/ResponsiveDrawer';
import { CreateSpaceModal } from './components/modals/CreateSpaceModal';
import { JoinSpaceModal } from './components/modals/JoinSpaceModal';
import { SpaceManageModal } from './components/modals/SpaceManageModal';
import { FileCenter } from './components/FileCenter';
import { RunsAndApprovalsView } from './components/RunsAndApprovalsView';
import { ActivityLogView } from './components/ActivityLogView';
import { ToastContainer } from './components/primitives/ToastContainer';
import { ConfirmDialog } from './components/primitives/ConfirmDialog';
import { SpanWaterfallDrawer } from './components/SpanWaterfallDrawer';
import { HACKATHON_GRAFANA_ONLY } from './hackathon';
import { lazy, Suspense } from 'preact/compat';

import { loadSpaceData } from './services/spaceLoader';

const StagingTestAuthPage =
  import.meta.env.MODE !== 'production'
    ? lazy(() => import('./staging/TestAuthPage').then((m) => ({ default: m.TestAuthPage })))
    : null;


export function App(): JSX.Element {
  const currentUser = currentUserSignal.value;
  const activeSpaceId = activeSpaceIdSignal.value;
  const activeTag = activeTagSignal.value;
  const isLoading = authLoadingSignal.value;

  const [isMobile, setIsMobile] = useState(
    typeof window !== 'undefined' ? window.matchMedia('(max-width: 768px)').matches : false
  );
  const [isTablet, setIsTablet] = useState(
    typeof window !== 'undefined' ? window.matchMedia('(max-width: 1024px)').matches : false
  );

  const [inviteToken, setInviteToken] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    const match = window.location.pathname.match(/\/invites\/([^/]+)/);
    return match ? match[1] : null;
  });

  // Restore pending invite session if returning authenticated
  useEffect(() => {
    if (currentUser && !inviteToken) {
      const pending = getPendingInviteSession();
      if (pending && pending.token) {
        setInviteToken(pending.token);
      }
    }
  }, [currentUser?.uid]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mqlMobile = window.matchMedia('(max-width: 768px)');
    const mqlTablet = window.matchMedia('(max-width: 1024px)');

    const handleMobileChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    const handleTabletChange = (e: MediaQueryListEvent) => setIsTablet(e.matches);

    mqlMobile.addEventListener('change', handleMobileChange);
    mqlTablet.addEventListener('change', handleTabletChange);

    return () => {
      mqlMobile.removeEventListener('change', handleMobileChange);
      mqlTablet.removeEventListener('change', handleTabletChange);
    };
  }, []);

  // Initialize Auth & Theme
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', themeSignal.value);
    authManager.initAuth();
  }, []);

  // Load Spaces when user logs in
  useEffect(() => {
    if (!currentUser) return;

    const loadUserSpaces = async () => {
      try {
        const userSpaces = await api.listSpaces();
        spacesSignal.value = userSpaces;

        // Restore last active space or default to first space (Home DM)
        const lastSpaceId = localStorage.getItem('last_active_space_id');
        if (lastSpaceId && userSpaces.some((s) => s.space_id === lastSpaceId)) {
          activeSpaceIdSignal.value = lastSpaceId;
        } else if (userSpaces.length > 0) {
          activeSpaceIdSignal.value = userSpaces[0].space_id;
        }
      } catch (err: any) {
        console.error('Failed to load spaces:', err);
      }
    };

    loadUserSpaces();
  }, [currentUser?.uid]);

  // Load Space Data when activeSpaceId or activeTag changes
  useEffect(() => {
    if (!currentUser || !activeSpaceId) {
      loadSpaceData('', 'all');
      return;
    }

    loadSpaceData(activeSpaceId, activeTag);
  }, [currentUser?.uid, activeSpaceId, activeTag]);

  if (isLoading) {
    return (
      <div class="app-loading-screen">
        <div class="logo-badge">
          <span class="logo-icon">🎬</span>
          <h2>Loading StudioTower...</h2>
        </div>
      </div>
    );
  }

  if (
    import.meta.env.MODE !== 'production' &&
    typeof window !== 'undefined' &&
    window.location.pathname === '/test-auth' &&
    StagingTestAuthPage
  ) {
    return (
      <Suspense fallback={<div style={{ padding: '2rem' }}>Loading test auth...</div>}>
        <StagingTestAuthPage />
      </Suspense>
    );
  }

  if (inviteToken) {
    return (
      <>
        <InviteLandingPage
          token={inviteToken}
          onJoined={(space) => {
            if (!spacesSignal.value.some((s) => s.space_id === space.space_id)) {
              spacesSignal.value = [...spacesSignal.value, space];
            }
            activeSpaceIdSignal.value = space.space_id;
            setInviteToken(null);
            if (typeof window !== 'undefined') {
              window.history.replaceState({}, '', '/');
            }
          }}
          onCancel={() => {
            setInviteToken(null);
            if (typeof window !== 'undefined') {
              window.history.replaceState({}, '', '/');
            }
          }}
        />
        <ToastContainer />
      </>
    );
  }

  if (!currentUser) {
    return (
      <>
        <LoginGate />
        <ToastContainer />
        <ConfirmDialog />
      </>
    );
  }

  const handleSelectSpace = (spaceId: string) => {
    activeSpaceIdSignal.value = spaceId;
    activeTagSignal.value = 'all';
    sidebarOpenSignal.value = false;
  };

  const handleNavigateHome = () => {
    const agentDm = spacesSignal.value.find((s) => s.kind === 'agent_dm');
    if (agentDm) {
      activeSpaceIdSignal.value = agentDm.space_id;
      activeTagSignal.value = 'all';
      sidebarOpenSignal.value = false;
    }
  };

  const isSidebarOpen = sidebarOpenSignal.value;
  const isRightPanelOpen = rightPanelOpenSignal.value;
  const isContextError = contextStateSignal.value === 'error';

  return (
    <div
      class={`app-layout ${isSidebarOpen ? 'sidebar-open' : ''} ${
        isRightPanelOpen ? 'right-panel-open' : ''
      }`}
    >
      <ResponsiveDrawer
        isOpen={isSidebarOpen}
        onClose={() => (sidebarOpenSignal.value = false)}
        title="Spaces Navigation"
        isOverlay={isMobile}
        drawerId="mobile-sidebar-drawer"
        triggerId="btn-sidebar-toggle"
        overlayClassName="sidebar-drawer-overlay"
      >
        <Sidebar onSelectSpace={handleSelectSpace} />
      </ResponsiveDrawer>

      <main class="app-workspace">
        <Header
          onSelectTag={(tag) => {
            activeTagSignal.value = tag;
            sidebarOpenSignal.value = false;
          }}
          onNavigateHome={handleNavigateHome}
        />

        <div class="workspace-body">
          {isContextError ? (
            <div class="space-error-container">
              <div class="error-card">
                <span class="error-icon">⚠️</span>
                <h3>Failed to load Space Information</h3>
                <p>Could not load context for space "{activeSpaceId}".</p>
                <button
                  class="btn-primary btn-retry-space"
                  onClick={() => {
                    const id = activeSpaceId;
                    if (id) {
                      activeSpaceIdSignal.value = null;
                      setTimeout(() => (activeSpaceIdSignal.value = id), 10);
                    }
                  }}
                >
                  ↻ Retry Loading Space
                </button>
              </div>
            </div>
          ) : (
            <>
              {activeViewSignal.value === 'chat' && <ChatArea />}
              {activeViewSignal.value === 'files' && <FileCenter />}
              {activeViewSignal.value === 'runs' && <RunsAndApprovalsView />}
              {activeViewSignal.value === 'activity' && <ActivityLogView />}
              <ResponsiveDrawer
                isOpen={isRightPanelOpen}
                onClose={() => (rightPanelOpenSignal.value = false)}
                title="Telemetry & Lineage Inspector"
                isOverlay={isTablet}
                drawerId="tablet-right-panel-drawer"
                triggerId="btn-right-panel-toggle"
                overlayClassName="right-panel-drawer-overlay"
              >
                <RightPanel />
              </ResponsiveDrawer>
            </>
          )}
        </div>
      </main>

      {/* Global Modals & Notifications */}
      <CreateSpaceModal />
      <JoinSpaceModal />
      <SpaceManageModal />
      {HACKATHON_GRAFANA_ONLY ? null : <SpanWaterfallDrawer />}
      <ToastContainer />
      <ConfirmDialog />
    </div>
  );
}
