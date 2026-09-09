import { JSX } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import { Modal } from '../primitives/Modal';
import { promptConfirm } from '../primitives/ConfirmDialog';
import {
  activeModalSignal,
  spaceContextSignal,
  spacesSignal,
  activeSpaceIdSignal,
} from '../../services/store';
import { currentUserSignal } from '../../services/auth';
import { api } from '../../services/api';
import { showToast } from '../../services/toast';
import { MemberInfo, Invite, MembershipRole, User } from '../../types';

export function SpaceManageModal(): JSX.Element | null {
  const isOpen = activeModalSignal.value === 'manage_space';
  const context = spaceContextSignal.value;
  const currentUser = currentUserSignal.value;

  const [activeTab, setActiveTab] = useState<'members' | 'invites' | 'tags' | 'danger'>('members');
  const [members, setMembers] = useState<MemberInfo[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  // Tag Management Form State
  const [newTagName, setNewTagName] = useState('');
  const [newTagSlug, setNewTagSlug] = useState('');
  const [newTagColor, setNewTagColor] = useState('#3B82F6');
  const [newTagDesc, setNewTagDesc] = useState('');
  const [isAddingTag, setIsAddingTag] = useState(false);

  // Invite Creator Form State
  const [inviteRole, setInviteRole] = useState<MembershipRole>('member');
  const [targetEmail, setTargetEmail] = useState('');
  const [isSingleUse, setIsSingleUse] = useState(true);
  const [isCreatingInvite, setIsCreatingInvite] = useState(false);
  const [generatedInvite, setGeneratedInvite] = useState<Invite | null>(null);

  // Direct User Addition Search State
  const [directSearchQuery, setDirectSearchQuery] = useState('');
  const [directSearchResults, setDirectSearchResults] = useState<User[]>([]);
  const [directAddRole, setDirectAddRole] = useState<MembershipRole>('member');
  const [isSearchingUsers, setIsSearchingUsers] = useState(false);
  const [isAddingDirectUser, setIsAddingDirectUser] = useState(false);

  const spaceId = context?.space.space_id;
  const capabilities = context?.capabilities;

  const loadData = async () => {
    if (!spaceId) return;
    setIsLoading(true);
    try {
      const [mems, invs] = await Promise.all([
        api.listMembers(spaceId),
        capabilities?.can_invite ? api.listInvites(spaceId).catch(() => []) : Promise.resolve([]),
      ]);
      setMembers(mems);
      setInvites(invs);
    } catch (err: any) {
      showToast(`Failed to load members: ${err.message}`, 'error');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen && spaceId) {
      loadData();
      // Bounded 20s polling while modal is open
      const timer = setInterval(loadData, 20000);
      return () => clearInterval(timer);
    }
  }, [isOpen, spaceId]);

  if (!isOpen || !context) return null;

  const handleRoleChange = async (targetUid: string, newRole: MembershipRole) => {
    try {
      await api.updateMemberRole(spaceId!, targetUid, newRole);
      showToast('Updated member role', 'success');
      loadData();
    } catch (err: any) {
      showToast(`Role update failed: ${err.message}`, 'error');
    }
  };

  const handleRemoveMember = async (targetUid: string, name: string) => {
    const { confirmed } = await promptConfirm({
      title: 'Remove Member',
      message: `Are you sure you want to remove ${name} from this Space?`,
      confirmLabel: 'Remove',
      isDestructive: true,
    });
    if (!confirmed) return;

    try {
      await api.removeMember(spaceId!, targetUid);
      showToast(`Removed member ${name}`, 'success');
      loadData();
    } catch (err: any) {
      showToast(`Failed to remove member: ${err.message}`, 'error');
    }
  };

  const handleCreateInvite = async (e: JSX.TargetedEvent) => {
    e.preventDefault();
    setIsCreatingInvite(true);
    try {
      const newInv = await api.createInvite(spaceId!, {
        role: inviteRole,
        target_email: targetEmail.trim() || null,
        max_uses: isSingleUse ? 1 : 100,
      });
      setGeneratedInvite(newInv);
      showToast('Generated invitation link', 'success');
      setTargetEmail('');
      loadData();
    } catch (err: any) {
      showToast(`Failed to create invite: ${err.message}`, 'error');
    } finally {
      setIsCreatingInvite(false);
    }
  };

  const handleSearchUsers = async (query: string) => {
    setDirectSearchQuery(query);
    if (!query.trim()) {
      setDirectSearchResults([]);
      return;
    }
    setIsSearchingUsers(true);
    try {
      const results = await api.searchUsers(query);
      setDirectSearchResults(results);
    } catch (err) {
      console.warn('Search users error:', err);
    } finally {
      setIsSearchingUsers(false);
    }
  };

  const handleDirectAddMember = async (emailOrUid: string) => {
    setIsAddingDirectUser(true);
    try {
      await api.directAddMember(spaceId!, emailOrUid, directAddRole);
      showToast(`Added ${emailOrUid} to Space as ${directAddRole}!`, 'success');
      setDirectSearchQuery('');
      setDirectSearchResults([]);
      loadData();
    } catch (err: any) {
      showToast(`Direct add failed: ${err.message}`, 'error');
    } finally {
      setIsAddingDirectUser(false);
    }
  };

  const handleCopyLink = (token: string) => {
    const fullLink = `${window.location.origin}/invites/${token}`;
    navigator.clipboard.writeText(fullLink);
    showToast('Copied invite link to clipboard!', 'success');
  };

  const handleRevokeInvite = async (token: string) => {
    try {
      await api.revokeInvite(token);
      showToast('Revoked invitation link', 'info');
      loadData();
    } catch (err: any) {
      showToast(`Revocation failed: ${err.message}`, 'error');
    }
  };

  const handleTransferOwnership = async () => {
    const otherMembers = members.filter((m) => m.uid !== currentUser?.uid);
    if (otherMembers.length === 0) {
      showToast('No eligible members to transfer ownership to', 'warning');
      return;
    }

    const { confirmed, value: selectedUid } = await promptConfirm({
      title: 'Transfer Space Ownership',
      message: 'Enter the exact UID of the member to transfer full Space Ownership to (You will become an Admin):',
      withInput: true,
      inputPlaceholder: otherMembers[0]?.uid,
      confirmLabel: 'Transfer Ownership',
      isDestructive: true,
    });

    if (!confirmed || !selectedUid?.trim()) return;

    try {
      await api.transferOwnership(spaceId!, selectedUid.trim());
      showToast('Transferred Space Ownership', 'success');
      activeModalSignal.value = null;
      // Refresh space context
      const newCtx = await api.getSpaceContext(spaceId!);
      spaceContextSignal.value = newCtx;
    } catch (err: any) {
      showToast(`Transfer failed: ${err.message}`, 'error');
    }
  };

  const handleLeaveSpace = async () => {
    const { confirmed } = await promptConfirm({
      title: 'Leave Space',
      message: `Are you sure you want to leave "${context.space.name}"? You will lose access to its tracks and files.`,
      confirmLabel: 'Leave Space',
      isDestructive: true,
    });
    if (!confirmed) return;

    try {
      await api.leaveSpace(spaceId!);
      showToast(`Left space "${context.space.name}"`, 'info');
      activeModalSignal.value = null;
      // Remove from spaces list and navigate home
      spacesSignal.value = spacesSignal.value.filter((s) => s.space_id !== spaceId);
      const remaining = spacesSignal.value;
      activeSpaceIdSignal.value = remaining[0]?.space_id || null;
    } catch (err: any) {
      showToast(`Failed to leave space: ${err.message}`, 'error');
    }
  };

  const handleAddTag = async (e: Event) => {
    e.preventDefault();
    if (!spaceId || !newTagName.trim() || !newTagSlug.trim()) return;
    setIsAddingTag(true);
    try {
      const updatedSpace = await api.addTag(spaceId, {
        name: newTagName.trim(),
        slug: newTagSlug.trim().toLowerCase(),
        color: newTagColor,
        description: newTagDesc.trim() || undefined,
      });
      showToast(`Created project tag #${newTagSlug}`, 'success');
      spaceContextSignal.value = { ...context, space: updatedSpace };
      setNewTagName('');
      setNewTagSlug('');
      setNewTagDesc('');
    } catch (err: any) {
      showToast(err.message || 'Failed to create tag', 'error');
    } finally {
      setIsAddingTag(false);
    }
  };

  const handleArchiveTag = async (tagSlug: string, currentArchived = false) => {
    if (!spaceId) return;
    const actionName = currentArchived ? 'Unarchive' : 'Archive';
    const { confirmed } = await promptConfirm({
      title: `${actionName} Tag #${tagSlug}`,
      message: currentArchived
        ? `Are you sure you want to unarchive tag #${tagSlug}? It will reappear in active tag filters.`
        : `Are you sure you want to archive tag #${tagSlug}? Associated files and message histories remain preserved.`,
      confirmLabel: actionName,
      isDestructive: !currentArchived,
    });
    if (!confirmed) return;

    try {
      const updatedSpace = await api.archiveTag(spaceId, tagSlug, !currentArchived);
      showToast(`Tag #${tagSlug} ${currentArchived ? 'unarchived' : 'archived'} successfully`, 'success');
      spaceContextSignal.value = { ...context, space: updatedSpace };
    } catch (err: any) {
      showToast(err.message || `Failed to ${actionName.toLowerCase()} tag`, 'error');
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => (activeModalSignal.value = null)}
      title={`Manage Space: ${context.space.name}`}
      maxWidth="620px"
    >
      <div class="manage-tabs">
        <button
          class={`manage-tab-btn ${activeTab === 'members' ? 'active' : ''}`}
          onClick={() => setActiveTab('members')}
        >
          👥 Members ({members.length})
        </button>
        {capabilities?.can_invite && (
          <button
            class={`manage-tab-btn ${activeTab === 'invites' ? 'active' : ''}`}
            onClick={() => setActiveTab('invites')}
          >
            ✉️ Invitations ({invites.length})
          </button>
        )}
        <button
          class={`manage-tab-btn ${activeTab === 'tags' ? 'active' : ''}`}
          onClick={() => setActiveTab('tags')}
        >
          🏷️ Tags ({context.space.tags?.length || 0})
        </button>
        <button
          class={`manage-tab-btn ${activeTab === 'danger' ? 'active' : ''}`}
          onClick={() => setActiveTab('danger')}
        >
          ⚙️ Settings & Danger Zone
        </button>
      </div>

      <div class="manage-tab-content">
        {/* Members Roster Tab */}
        {activeTab === 'members' && (
          <div class="members-tab">
            <div class="tab-header">
              <span class="tab-subtitle">Active crew members and allocated roles</span>
              {isLoading && <span class="loading-spinner">🔄 Refreshing...</span>}
            </div>

            <div class="members-list">
              {members.map((m) => {
                const isMe = m.uid === currentUser?.uid;
                const canChangeThisUser = capabilities?.can_change_role && !isMe && m.role !== 'owner';
                const canRemoveThisUser = capabilities?.can_remove_member && !isMe && m.role !== 'owner';

                return (
                  <div key={m.uid} class="member-item">
                    <div class="member-info">
                      <div class="member-avatar">{(m.display_name || m.email || 'U')[0].toUpperCase()}</div>
                      <div class="member-details">
                        <span class="member-name">
                          {m.display_name || m.email} {isMe ? '(You)' : ''}
                        </span>
                        <span class="member-email">{m.email || m.uid}</span>
                      </div>
                    </div>

                    <div class="member-actions">
                      {canChangeThisUser ? (
                        <select
                          class="role-select"
                          value={m.role}
                          onChange={(e) => handleRoleChange(m.uid, (e.target as HTMLSelectElement).value as MembershipRole)}
                        >
                          <option value="member">Member</option>
                          <option value="coordinator">Coordinator</option>
                          <option value="admin">Admin</option>
                        </select>
                      ) : (
                        <span class={`role-badge role-${m.role}`}>{m.role.toUpperCase()}</span>
                      )}

                      {canRemoveThisUser && (
                        <button
                          class="btn-text-danger"
                          onClick={() => handleRemoveMember(m.uid, m.display_name || m.email || m.uid)}
                          title="Remove from Space"
                        >
                          ✕
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Invitations & Direct User Addition Tab */}
        {activeTab === 'invites' && capabilities?.can_invite && (
          <div class="invites-tab">
            {/* 1. Direct Site User Search & Add */}
            <div class="direct-add-section">
              <h4>Add Existing Site User Directly</h4>
              <p class="form-hint">Search registered site users by email or name to add them to this Space immediately.</p>
              
              <div class="direct-add-row">
                <input
                  type="text"
                  class="form-input search-user-input"
                  placeholder={isSearchingUsers ? 'Searching users...' : 'Enter email or name to search...'}
                  value={directSearchQuery}
                  onInput={(e) => handleSearchUsers((e.target as HTMLInputElement).value)}
                />
                <select
                  class="form-select role-select"
                  value={directAddRole}
                  onChange={(e) => setDirectAddRole((e.target as HTMLSelectElement).value as MembershipRole)}
                >
                  <option value="member">Member</option>
                  <option value="coordinator">Coordinator</option>
                  <option value="admin">Admin</option>
                </select>
                <button
                  type="button"
                  class="btn-primary"
                  onClick={() => directSearchQuery.trim() && handleDirectAddMember(directSearchQuery.trim())}
                  disabled={!directSearchQuery.trim() || isAddingDirectUser}
                >
                  {isAddingDirectUser ? 'Adding...' : '+ Add User'}
                </button>
              </div>

              {/* Auto-complete Search Results Dropdown */}
              {directSearchResults.length > 0 && (
                <div class="user-search-results-list">
                  {directSearchResults.map((u) => (
                    <div key={u.uid} class="user-search-item" onClick={() => handleDirectAddMember(u.email || u.uid)}>
                      <span class="user-search-name">{u.display_name || u.email}</span>
                      <span class="user-search-email">({u.email})</span>
                      <span class="btn-add-pill">+ Select</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <hr class="section-divider" />

            {/* 2. Invitation Link Generator */}
            <form class="create-invite-form" onSubmit={handleCreateInvite}>
              <h4>Generate Invitation Link</h4>

              <div class="form-row">
                <div class="form-group">
                  <label>Assign Role</label>
                  <select
                    class="form-select"
                    value={inviteRole}
                    onChange={(e) => setInviteRole((e.target as HTMLSelectElement).value as MembershipRole)}
                  >
                    <option value="member">Member</option>
                    <option value="coordinator">Coordinator</option>
                    <option value="admin">Admin</option>
                  </select>
                </div>

                <div class="form-group">
                  <label>Link Usage</label>
                  <select
                    class="form-select"
                    value={isSingleUse ? 'single' : 'multi'}
                    onChange={(e) => setIsSingleUse((e.target as HTMLSelectElement).value === 'single')}
                  >
                    <option value="single">Single Use (1 Join)</option>
                    <option value="multi">Multi Use (Unlimited)</option>
                  </select>
                </div>
              </div>

              <div class="form-group">
                <label>Restricted Email (Optional)</label>
                <input
                  type="email"
                  class="form-input"
                  placeholder="alice@filmcrew.org (Leave blank for anyone with link)"
                  value={targetEmail}
                  onInput={(e) => setTargetEmail((e.target as HTMLInputElement).value)}
                />
              </div>

              <button type="submit" class="btn-primary" disabled={isCreatingInvite}>
                {isCreatingInvite ? 'Generating...' : '🔑 Generate Invite Link'}
              </button>
            </form>

            {generatedInvite && (
              <div class="generated-invite-box generated-invite-banner">
                <span class="box-title">✅ Link Created:</span>
                <code class="invite-token-code">{generatedInvite.token}</code>
                <button
                  type="button"
                  class="btn-secondary btn-copy"
                  onClick={() => handleCopyLink(generatedInvite.token)}
                >
                  📋 Copy Invite Link
                </button>
              </div>
            )}

            <div class="active-invites-section">
              <h4>Active Invitation Links</h4>
              {invites.length === 0 ? (
                <p class="empty-hint">No active invitation links.</p>
              ) : (
                <div class="invites-list">
                  {invites.map((inv) => (
                    <div key={inv.token} class="invite-item invite-row">
                      <div class="invite-info">
                        <span class="invite-role-badge">Role: {inv.role.toUpperCase()}</span>
                        <span class="invite-meta">
                          Uses: {inv.used_count}/{inv.max_uses} | Token: {inv.token.substring(0, 8)}...
                        </span>
                      </div>
                      <div class="invite-item-actions">
                        <button class="btn-text-action" onClick={() => handleCopyLink(inv.token)}>
                          Copy Link
                        </button>
                        <button class="btn-text-danger" onClick={() => handleRevokeInvite(inv.token)}>
                          Revoke
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tags Management Tab */}
        {activeTab === 'tags' && (
          <div class="tags-tab">
            {capabilities?.can_manage_tags && (
              <form class="create-tag-form" onSubmit={handleAddTag}>
                <h4>+ Add Project Tag</h4>
                <div class="form-row-grid">
                  <div class="form-group">
                    <label>Tag Name *</label>
                    <input
                      type="text"
                      placeholder="e.g. Sound Track, VFX"
                      value={newTagName}
                      onInput={(e) => {
                        const val = (e.target as HTMLInputElement).value;
                        setNewTagName(val);
                        if (!newTagSlug) {
                          setNewTagSlug(val.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''));
                        }
                      }}
                      required
                    />
                  </div>
                  <div class="form-group">
                    <label>Slug (Immutable) *</label>
                    <input
                      type="text"
                      placeholder="e.g. sound-track"
                      value={newTagSlug}
                      onInput={(e) => setNewTagSlug((e.target as HTMLInputElement).value.toLowerCase())}
                      pattern="^[a-z0-9]+(?:-[a-z0-9]+)*$"
                      title="Lowercase letters, numbers, and single hyphens"
                      required
                    />
                  </div>
                  <div class="form-group">
                    <label>Color</label>
                    <input
                      type="color"
                      value={newTagColor}
                      onInput={(e) => setNewTagColor((e.target as HTMLInputElement).value)}
                      style="height: 38px; padding: 2px; width: 100%; cursor: pointer;"
                    />
                  </div>
                </div>
                <div class="form-group" style="margin-top: 8px;">
                  <label>Description (Optional)</label>
                  <input
                    type="text"
                    placeholder="Brief scope description..."
                    value={newTagDesc}
                    onInput={(e) => setNewTagDesc((e.target as HTMLInputElement).value)}
                  />
                </div>
                <button type="submit" class="btn-primary" disabled={isAddingTag} style="margin-top: 10px;">
                  {isAddingTag ? 'Adding...' : '🏷️ Create Tag'}
                </button>
              </form>
            )}

            <div class="active-tags-section" style="margin-top: 20px;">
              <h4>Existing Project Tags ({context.space.tags?.length || 0})</h4>
              <div class="tags-list">
                {context.space.tags?.map((t) => (
                  <div key={t.slug} class="tag-item tag-row" style="display: flex; align-items: center; justify-content: space-between; padding: 10px; border-bottom: 1px solid var(--border-subtle);">
                    <div class="tag-info" style="display: flex; align-items: center; gap: 8px;">
                      <span class="tag-color-dot" style={{ backgroundColor: t.color, width: '12px', height: '12px', borderRadius: '50%' }} />
                      <strong>#{t.slug}</strong>
                      <span style="color: var(--text-secondary); font-size: 13px;">({t.name})</span>
                      {t.archived && <span class="archived-label" style="color: var(--text-muted); font-size: 11px;">[Archived]</span>}
                    </div>
                    {capabilities?.can_manage_tags && t.slug !== 'general' && (
                      <div class="tag-actions" style="display: flex; gap: 6px;">
                        <button
                          type="button"
                          class="btn-text-action"
                          onClick={() => handleArchiveTag(t.slug, t.archived)}
                        >
                          {t.archived ? 'Unarchive' : 'Archive'}
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Danger Zone & Settings Tab */}
        {activeTab === 'danger' && (
          <div class="danger-tab">
            {capabilities?.can_transfer_ownership && (
              <div class="danger-box">
                <h4>Transfer Space Ownership</h4>
                <p>Transfer full Space ownership to another crew member. You will become an Admin.</p>
                <button class="btn-warning" onClick={handleTransferOwnership}>
                  👑 Transfer Ownership
                </button>
              </div>
            )}

            {capabilities?.can_leave_space && (
              <div class="danger-box">
                <h4>Leave Space</h4>
                <p>Remove yourself from this Space. You will lose access to its tracks and files.</p>
                <button class="btn-danger" onClick={handleLeaveSpace}>
                  🚪 Leave Space
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
