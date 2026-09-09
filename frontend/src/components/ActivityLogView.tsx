import { JSX } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import {
  activityEventsSignal,
  activityNextCursorSignal,
  activityStateSignal,
  spaceContextSignal,
  activeTagSignal,
  activeSpaceIdSignal,
  activeViewSignal,
  selectedFileIdsSignal,
  selectedRunIdSignal,
} from '../services/store';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { ActivityEvent } from '../types';

export function ActivityLogView(): JSX.Element {
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const reqGenRef = useRef<number>(0);

  const events = activityEventsSignal.value;
  const nextCursor = activityNextCursorSignal.value;
  const activityState = activityStateSignal.value;
  const context = spaceContextSignal.value;
  const activeTag = activeTagSignal.value;
  const spaceId = context?.space.space_id || '';

  const loadInitialActivities = async (targetTag?: string) => {
    if (!spaceId) return;
    const reqSpaceId = spaceId;
    const reqTag = targetTag !== undefined ? targetTag : activeTag;
    const currentGen = ++reqGenRef.current;
    setIsLoadingMore(false);
    activityStateSignal.value = 'loading';
    try {
      const resp = await api.listActivity(reqSpaceId, reqTag !== 'all' ? reqTag : undefined, undefined, 30);
      if (
        currentGen === reqGenRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag
      ) {
        const uniqueItems = Array.from(
          new Map((resp.items || []).map((e: ActivityEvent) => [e.event_id, e])).values()
        );
        activityEventsSignal.value = uniqueItems;
        activityNextCursorSignal.value = resp.next_cursor || null;
        activityStateSignal.value = 'ready';
      }
    } catch (err: any) {
      if (
        currentGen === reqGenRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag
      ) {
        activityStateSignal.value = 'error';
        showToast(err.message || 'Failed to load activity log', 'error');
      }
    }
  };

  const handleLoadMore = async () => {
    if (!spaceId || !nextCursor || isLoadingMore) return;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    const currentGen = ++reqGenRef.current;
    setIsLoadingMore(true);
    try {
      const resp = await api.listActivity(reqSpaceId, reqTag !== 'all' ? reqTag : undefined, nextCursor, 30);
      if (
        currentGen === reqGenRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag
      ) {
        const existing = activityEventsSignal.value;
        const incoming = resp.items || [];
        const map = new Map<string, ActivityEvent>();
        for (const item of [...existing, ...incoming]) {
          map.set(item.event_id, item);
        }
        activityEventsSignal.value = Array.from(map.values());
        activityNextCursorSignal.value = resp.next_cursor || null;
      }
    } catch (err: any) {
      if (
        currentGen === reqGenRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag
      ) {
        showToast(err.message || 'Failed to load more activity events', 'error');
      }
    } finally {
      setIsLoadingMore(false);
    }
  };

  useEffect(() => {
    setIsLoadingMore(false);
    loadInitialActivities(activeTag);
    return () => {
      reqGenRef.current++;
    };
  }, [spaceId, activeTag]);

  const getEventIcon = (eventType: string) => {
    switch (eventType) {
      case 'file.uploaded':
        return '📤';
      case 'file.ingestion_ready':
        return '✅';
      case 'file.ingestion_partial':
      case 'file.ingestion_partial_ocr':
        return '⚠️';
      case 'file.needs_ocr':
        return '🔍';
      case 'file.ingestion_failed':
        return '❌';
      case 'file.reindex_triggered':
        return '🔄';
      case 'message.created':
        return '💬';
      case 'run.started':
        return '🚀';
      case 'run.completed':
        return '🎉';
      case 'run.failed':
        return '🛑';
      case 'gate.approved':
        return '🛡️';
      case 'gate.rejected':
        return '🚫';
      case 'tag.created':
        return '🏷️';
      case 'tag.updated':
        return '✏️';
      case 'tag.archived':
        return '📦';
      case 'tag.unarchived':
        return '📂';
      default:
        return '📌';
    }
  };

  const handleEventClick = (ev: ActivityEvent) => {
    if (ev.resource_type === 'file') {
      selectedFileIdsSignal.value = [ev.resource_id];
      activeViewSignal.value = 'files';
    } else if (ev.resource_type === 'run' || ev.resource_type === 'gate') {
      selectedRunIdSignal.value = ev.resource_id;
      activeViewSignal.value = 'runs';
    } else if (ev.resource_type === 'message') {
      activeViewSignal.value = 'chat';
    }
  };

  return (
    <div class="activity-view-container">
      <div class="activity-header">
        <div class="activity-title-group">
          <h1 class="activity-title">Space Activity Timeline</h1>
          <span class="activity-count-pill">{events.length} {events.length === 1 ? 'Event' : 'Events'}</span>
        </div>

        {/* Tag Pills Selector */}
        <div class="activity-tag-pills" role="tablist" aria-label="Filter activity by tag">
          <button
            type="button"
            class={`tag-pill-btn ${activeTag === 'all' ? 'active' : ''}`}
            onClick={() => {
              activeTagSignal.value = 'all';
            }}
          >
            All Tags
          </button>
          {context?.space.tags.map((t) => (
            <button
              key={t.slug}
              type="button"
              class={`tag-pill-btn ${activeTag === t.slug ? 'active' : ''} ${t.archived ? 'tag-pill-archived' : ''}`}
              style={activeTag === t.slug ? { borderColor: t.color, backgroundColor: `${t.color}22` } : {}}
              onClick={() => {
                activeTagSignal.value = t.slug;
              }}
            >
              <span class="tag-color-dot" style={{ backgroundColor: t.color }} />
              {t.name}
              {t.archived && <span class="archived-tag-badge">(Archived)</span>}
            </button>
          ))}
        </div>
      </div>

      {/* Activity Timeline List */}
      <div class="activity-list-card">
        {activityState === 'loading' && events.length === 0 ? (
          <div class="activity-loading-state">
            <div class="spinner-large" />
            <p>Loading activity logs...</p>
          </div>
        ) : events.length === 0 ? (
          <div class="activity-empty-state">
            <p class="empty-title">No Activity Recorded Yet</p>
            <p class="empty-subtitle">Chat conversations, file uploads, scene runs, gate approvals, and tag changes are logged here in an immutable, unified timeline.</p>
          </div>
        ) : (
          <div class="activity-timeline-flow">
            {events.map((ev) => {
              const tags = (ev.project_tags && ev.project_tags.length > 0)
                ? ev.project_tags
                : [ev.project_tag || 'general'];

              const isClickable = ['file', 'run', 'gate', 'message'].includes(ev.resource_type);

              return (
                <div
                  key={ev.event_id}
                  class={`activity-event-row ${isClickable ? 'activity-event-clickable' : ''}`}
                  onClick={() => handleEventClick(ev)}
                >
                  <div class="activity-icon-col">
                    <span class="activity-event-icon" title={ev.event_type}>
                      {getEventIcon(ev.event_type)}
                    </span>
                    <div class="activity-timeline-line" />
                  </div>
                  <div class="activity-content-col">
                    <div class="activity-summary-row">
                      <span class="activity-summary-text">{ev.summary}</span>
                      <div class="activity-tags-group">
                        {tags.map((t) => (
                          <span key={t} class="activity-tag-badge">
                            #{t}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div class="activity-meta-row">
                      <span class="activity-time-text">
                        {new Date(ev.created_at).toLocaleString()}
                      </span>
                      <span class="activity-resource-id">
                        {ev.resource_type.toUpperCase()}: {ev.resource_id}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}

            {nextCursor && (
              <div class="activity-load-more-row">
                <button
                  class="btn-secondary load-more-btn"
                  type="button"
                  disabled={isLoadingMore}
                  onClick={handleLoadMore}
                >
                  {isLoadingMore ? 'Loading...' : 'Load More Events'}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
