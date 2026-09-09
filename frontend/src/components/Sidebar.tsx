import { JSX } from 'preact';
import { useState } from 'preact/hooks';
import {
  spacesSignal,
  activeSpaceIdSignal,
  activeModalSignal,
} from '../services/store';
import { currentUserSignal, authManager } from '../services/auth';
import { Space } from '../types';

interface SidebarProps {
  onSelectSpace: (spaceId: string) => void;
}

export function Sidebar({ onSelectSpace }: SidebarProps): JSX.Element {
  const [searchQuery, setSearchQuery] = useState('');
  const spaces = spacesSignal.value;
  const activeSpaceId = activeSpaceIdSignal.value;
  const currentUser = currentUserSignal.value;

  const agentDm = spaces.find((s) => s.kind === 'agent_dm');
  const sharedSpaces = spaces.filter((s) => s.kind !== 'agent_dm');
  const filteredShared = sharedSpaces.filter((s) =>
    s.name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <aside class="app-sidebar" aria-label="Spaces Navigation">
      <div class="sidebar-header">
        <div class="sidebar-logo">
          <span class="logo-icon">🎬</span>
          <span class="logo-text">StudioTower</span>
        </div>
        <div class="sidebar-actions">
          <button
            id="btn-create-space"
            class="btn-secondary btn-compact"
            onClick={() => (activeModalSignal.value = 'create_space')}
            title="Create new Space"
          >
            + Space
          </button>
          <button
            id="btn-join-space"
            class="btn-secondary btn-compact"
            onClick={() => (activeModalSignal.value = 'join_space')}
            title="Join Space with Invite Token"
          >
            🔗 Join
          </button>
        </div>
      </div>

      <div class="sidebar-search-box">
        <input
          class="sidebar-search-input"
          placeholder="🔍 Search spaces..."
          value={searchQuery}
          onInput={(e) => setSearchQuery((e.target as HTMLInputElement).value)}
          aria-label="Filter spaces"
        />
      </div>

      <div class="sidebar-section-title">Home (Private Workspace)</div>
      <div class="space-list space-list-pinned">
        {agentDm ? (
          <button
            key={agentDm.space_id}
            class={`space-item ${activeSpaceId === agentDm.space_id ? 'active' : ''}`}
            onClick={() => onSelectSpace(agentDm.space_id)}
          >
            <span class="space-item-icon">🤖</span>
            <span class="space-item-name">{agentDm.name}</span>
          </button>
        ) : (
          <div class="space-empty-note">Connecting Assistant...</div>
        )}
      </div>

      <div class="sidebar-section-title">
        <span>Shared Spaces</span>
        <span class="badge-count">{sharedSpaces.length}</span>
      </div>

      <div class="space-list space-list-scrollable">
        {filteredShared.length > 0 ? (
          filteredShared.map((s: Space) => (
            <button
              key={s.space_id}
              class={`space-item ${activeSpaceId === s.space_id ? 'active' : ''}`}
              onClick={() => onSelectSpace(s.space_id)}
            >
              <span class="space-item-icon">🏢</span>
              <span class="space-item-name">{s.name}</span>
            </button>
          ))
        ) : (
          <div class="space-empty-note">
            {searchQuery ? 'No matching spaces' : 'No shared spaces yet'}
          </div>
        )}
      </div>

      {currentUser && (
        <div class="sidebar-footer">
          <div class="user-profile-badge">
            <div class="user-avatar">
              {(currentUser.display_name || currentUser.email || 'U')[0].toUpperCase()}
            </div>
            <div class="user-info">
              <span class="user-name">{currentUser.display_name || currentUser.email}</span>
              <span class="user-email">{currentUser.email}</span>
            </div>
          </div>
          <button
            class="btn-secondary btn-compact"
            onClick={() => authManager.signOut()}
            title="Sign out"
          >
            Exit
          </button>
        </div>
      )}
    </aside>
  );
}
