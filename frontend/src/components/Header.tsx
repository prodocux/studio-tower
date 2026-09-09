import { JSX } from 'preact';
import {
  activeViewSignal,
  spaceContextSignal,
  activeTagSignal,
  activeModalSignal,
  sidebarOpenSignal,
  rightPanelOpenSignal,
  themeSignal,
  toggleTheme,
} from '../services/store';

interface HeaderProps {
  onSelectTag: (tagSlug: string) => void;
  onNavigateHome: () => void;
}

export function Header({ onSelectTag, onNavigateHome }: HeaderProps): JSX.Element {
  const context = spaceContextSignal.value;
  const activeTag = activeTagSignal.value;
  const currentTheme = themeSignal.value;

  if (!context) {
    return (
      <header class="workspace-header" aria-label="Workspace Navigation">
        <div class="header-top">
          <div class="breadcrumb-trail">
            <button
              id="btn-sidebar-toggle"
              class="btn-icon mobile-menu-btn"
              onClick={() => (sidebarOpenSignal.value = !sidebarOpenSignal.value)}
              title="Toggle Navigation Menu"
              aria-label="Toggle Navigation Menu"
              aria-expanded={sidebarOpenSignal.value}
              aria-controls="mobile-sidebar-drawer"
            >
              ☰
            </button>
            <span class="breadcrumb-item">🎬 StudioTower</span>
          </div>
          <div class="header-meta-group">
            <button
              id="btn-theme-toggle"
              class="btn-icon theme-toggle-btn"
              onClick={toggleTheme}
              title={currentTheme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
              aria-label="Toggle Light/Dark Theme"
            >
              {currentTheme === 'dark' ? '☀️' : '🌙'}
            </button>
          </div>
        </div>
      </header>
    );
  }

  const { space, current_user_role, member_count, capabilities } = context;
  const isAgentDm = space.kind === 'agent_dm';

  return (
    <header class="workspace-header" aria-label="Workspace Navigation">
      <div class="header-top">
        <nav class="breadcrumb-trail" aria-label="Breadcrumb">
          <button
            id="btn-sidebar-toggle"
            class="btn-icon mobile-menu-btn"
            onClick={() => (sidebarOpenSignal.value = !sidebarOpenSignal.value)}
            title="Toggle Navigation Menu"
            aria-label="Toggle Navigation Menu"
            aria-expanded={sidebarOpenSignal.value}
            aria-controls="mobile-sidebar-drawer"
          >
            ☰
          </button>

          <button class="breadcrumb-btn" onClick={onNavigateHome}>
            <span>🏠</span> Home
          </button>
          <span class="breadcrumb-separator">/</span>
          <span class="breadcrumb-current-space">
            <span>{isAgentDm ? '🤖' : '🏢'}</span> {space.name}
          </span>
          {activeTag && activeTag !== 'all' && (
            <>
              <span class="breadcrumb-separator">/</span>
              <span class="breadcrumb-current-tag">#{activeTag}</span>
            </>
          )}
        </nav>

        <div class="header-meta-group">
          {!isAgentDm && (
            <>
              <div class="header-badge header-members-badge" title="Active space members">
                <span>👥</span> {member_count} {member_count === 1 ? 'Member' : 'Members'}
              </div>

              <div class={`header-badge role-badge role-${current_user_role}`}>
                <span>🛡️</span> {current_user_role.toUpperCase()}
              </div>

              {(capabilities.can_invite || capabilities.can_manage_members) && (
                <button
                  id="btn-manage-space"
                  class="btn-primary btn-compact"
                  onClick={() => (activeModalSignal.value = 'manage_space')}
                  title="Space Settings & Team Governance"
                >
                  ⚙️ Manage & Invite
                </button>
              )}
            </>
          )}

          <button
            id="btn-right-panel-toggle"
            class="btn-secondary btn-compact tablet-drawer-btn"
            onClick={() => (rightPanelOpenSignal.value = !rightPanelOpenSignal.value)}
            title="Toggle Telemetry & Lineage Drawer"
            aria-label="Toggle Telemetry & Lineage Drawer"
            aria-expanded={rightPanelOpenSignal.value}
            aria-controls="tablet-right-panel-drawer"
          >
            📊 Drawer
          </button>

          <button
            id="btn-theme-toggle"
            class="btn-icon theme-toggle-btn"
            onClick={toggleTheme}
            title={currentTheme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
            aria-label="Toggle Light/Dark Theme"
          >
            {currentTheme === 'dark' ? '☀️' : '🌙'}
          </button>
        </div>
      </div>

      {/* Top Primary View Navigation Tabs */}
      <div class="header-nav-tabs" role="tablist" aria-label="Workspace Views">
        <button
          type="button"
          id="tab-view-chat"
          role="tab"
          aria-selected={activeViewSignal.value === 'chat'}
          class={`header-nav-tab ${activeViewSignal.value === 'chat' ? 'active' : ''}`}
          onClick={() => (activeViewSignal.value = 'chat')}
        >
          💬 Chat
        </button>
        <button
          type="button"
          id="tab-view-files"
          role="tab"
          aria-selected={activeViewSignal.value === 'files'}
          class={`header-nav-tab ${activeViewSignal.value === 'files' ? 'active' : ''}`}
          onClick={() => (activeViewSignal.value = 'files')}
        >
          📁 File Center
        </button>
        <button
          type="button"
          id="tab-view-runs"
          role="tab"
          aria-selected={activeViewSignal.value === 'runs'}
          class={`header-nav-tab ${activeViewSignal.value === 'runs' ? 'active' : ''}`}
          onClick={() => (activeViewSignal.value = 'runs')}
        >
          ⚙️ Tasks & Approvals
        </button>
        <button
          type="button"
          id="tab-view-activity"
          role="tab"
          aria-selected={activeViewSignal.value === 'activity'}
          class={`header-nav-tab ${activeViewSignal.value === 'activity' ? 'active' : ''}`}
          onClick={() => (activeViewSignal.value = 'activity')}
        >
          ⏱️ Activity Log
        </button>
      </div>

      {/* Project Tags Bar */}
      <div class="header-tag-bar">
        <button
          class={`tag-pill ${activeTag === 'all' ? 'active' : ''}`}
          onClick={() => onSelectTag('all')}
        >
          #all-tracks
        </button>
        {space.tags?.map((tag) => (
          <button
            key={tag.slug}
            class={`tag-pill ${activeTag === tag.slug ? 'active' : ''}`}
            style={{ borderColor: tag.color || '#3B82F6' }}
            onClick={() => onSelectTag(tag.slug)}
          >
            <span class="tag-color-dot" style={{ backgroundColor: tag.color || '#3B82F6' }} />
            #{tag.slug}
          </button>
        ))}
      </div>
    </header>
  );
}
