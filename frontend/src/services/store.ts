import { signal } from '@preact/signals';
import { Space, SpaceContext, Message, FileRecord, Run, LineageGraph, WorkspaceView, ActivityEvent, WaterfallTarget } from '../types';

export type ResourceState = 'idle' | 'loading' | 'ready' | 'error';

export const activeViewSignal = signal<WorkspaceView>('chat');

export const spacesSignal = signal<Space[]>([]);
export const activeSpaceIdSignal = signal<string | null>(null);
export const spaceContextSignal = signal<SpaceContext | null>(null);
export const activeTagSignal = signal<string>('all');
export const activeRightTabSignal = signal<'files' | 'lineage' | 'grafana' | 'team'>('files');

// File Center Filters & State
export const fileSearchQuerySignal = signal<string>('');
export const fileStatusFilterSignal = signal<string>('all');
export const fileSortSignal = signal<'uploaded_at_desc' | 'uploaded_at_asc' | 'name_asc' | 'size_desc' | 'updated_at_desc' | 'updated_at_asc'>('uploaded_at_desc');
export const selectedFileIdsSignal = signal<string[]>([]);

// Activity Events State
export const activityEventsSignal = signal<ActivityEvent[]>([]);
export const activityNextCursorSignal = signal<string | null>(null);
export const activityStateSignal = signal<ResourceState>('idle');

// Per-resource loading lifecycle states
export const contextStateSignal = signal<ResourceState>('idle');
export const messagesStateSignal = signal<ResourceState>('idle');
export const filesStateSignal = signal<ResourceState>('idle');
export const runsStateSignal = signal<ResourceState>('idle');
export const lineageStateSignal = signal<ResourceState>('idle');

// Responsive Drawer Open/Close signals
export const sidebarOpenSignal = signal<boolean>(false);
export const rightPanelOpenSignal = signal<boolean>(false);

export const messagesSignal = signal<Message[]>([]);
export const messagesNextCursorSignal = signal<string | null>(null);
export const messagesHasMoreSignal = signal<boolean>(false);
export const filesSignal = signal<FileRecord[]>([]);
export const runsSignal = signal<Run[]>([]);
export const lineageSignal = signal<LineageGraph | null>(null);

// Composer cross-panel control signals (used by RightPanel Discuss buttons to set ChatArea state)
export const composerDraftSignal = signal<string>('');
export const composerContextFileIdsSignal = signal<string[]>([]);
export const composerContextRunIdSignal = signal<string | null>(null);
export const composerIntentSignal = signal<'conversation' | 'document_qa' | 'create_breakdown'>('conversation');

// Inspector & Telemetry targeted selection signal (does NOT pollute Chat Composer)
export const selectedRunIdSignal = signal<string | null>(null);

// Span Waterfall Drawer targeted run selection signal
let waterfallGenCounter = 0;
export const getNextWaterfallGeneration = () => ++waterfallGenCounter;
export const selectedWaterfallTargetSignal = signal<WaterfallTarget | null>(null);

export const activeModalSignal = signal<'create_space' | 'join_space' | 'manage_space' | null>(null);
export const selectedLineageNodeSignal = signal<any | null>(null);

const initialTheme = typeof window !== 'undefined' && localStorage.getItem('studiotower_theme') === 'light' ? 'light' : 'dark';
export const themeSignal = signal<'dark' | 'light'>(initialTheme);

export function toggleTheme() {
  const next = themeSignal.value === 'dark' ? 'light' : 'dark';
  themeSignal.value = next;
  if (typeof window !== 'undefined') {
    localStorage.setItem('studiotower_theme', next);
    document.documentElement.setAttribute('data-theme', next);
  }
}

// Request sequence controller for Space/Tag switches to prevent race conditions
class RequestSequencer {
  private currentController: AbortController | null = null;

  getNextSignal(): AbortSignal {
    if (this.currentController) {
      this.currentController.abort();
    }
    this.currentController = new AbortController();
    return this.currentController.signal;
  }

  reset(): void {
    if (this.currentController) {
      this.currentController.abort();
      this.currentController = null;
    }
  }
}

export const requestSequencer = new RequestSequencer();
