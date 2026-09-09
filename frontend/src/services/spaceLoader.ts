import { currentUserSignal, registerSignOutHook } from './auth';
import {
  activeSpaceIdSignal,
  activeTagSignal,
  spaceContextSignal,
  messagesSignal,
  messagesNextCursorSignal,
  messagesHasMoreSignal,
  filesSignal,
  runsSignal,
  lineageSignal,
  contextStateSignal,
  messagesStateSignal,
  filesStateSignal,
  runsStateSignal,
  lineageStateSignal,
  selectedFileIdsSignal,
  composerContextFileIdsSignal,
  composerContextRunIdSignal,
  selectedRunIdSignal,
  composerDraftSignal,
  composerIntentSignal,
  requestSequencer,
} from './store';
import { api } from './api';
import { createMergedAbortSignal } from './abortHelper';
import { SpaceContext, Message, FileRecord, Run, LineageGraph } from '../types';

interface SpaceCacheEntry {
  context: SpaceContext;
  messages: Message[];
  files: FileRecord[];
  runs: Run[];
  lineage: LineageGraph | null;
  cachedAt: number;
}

const spaceMemoryCache = new Map<string, SpaceCacheEntry>();
let activeLoaderSeq = 0;

export function clearSpaceCache(spaceId?: string) {
  if (spaceId) {
    for (const key of spaceMemoryCache.keys()) {
      if (key.includes(`:${spaceId}:`)) spaceMemoryCache.delete(key);
    }
  } else {
    spaceMemoryCache.clear();
  }
}

registerSignOutHook(() => {
  requestSequencer.reset();
  clearSpaceCache();
  spaceContextSignal.value = null;
  messagesSignal.value = [];
  filesSignal.value = [];
  runsSignal.value = [];
  lineageSignal.value = null;
  selectedFileIdsSignal.value = [];
  composerContextFileIdsSignal.value = [];
  composerContextRunIdSignal.value = null;
  selectedRunIdSignal.value = null;
  composerDraftSignal.value = '';
  composerIntentSignal.value = 'conversation';
  contextStateSignal.value = 'idle';
  messagesStateSignal.value = 'idle';
  filesStateSignal.value = 'idle';
  runsStateSignal.value = 'idle';
  lineageStateSignal.value = 'idle';
});

/**
 * Encapsulated production space loader logic with SWR client-side caching & request abort sequencing.
 * Returns an awaitable Promise<void> covering primary space context and secondary parallel resource fetches.
 */
export async function loadSpaceData(spaceId: string, tag: string): Promise<void> {
  const reqUid = currentUserSignal.value?.uid;
  const reqSpaceId = spaceId;
  const reqTag = tag;
  const reqSeq = ++activeLoaderSeq;

  // Strict cross-space isolation: reset all composer context when initiating space/tag switch
  selectedFileIdsSignal.value = [];
  composerContextFileIdsSignal.value = [];
  composerContextRunIdSignal.value = null;
  selectedRunIdSignal.value = null;
  composerDraftSignal.value = '';
  composerIntentSignal.value = 'conversation';

  if (!spaceId || !reqUid) {
    requestSequencer.reset();
    spaceContextSignal.value = null;
    messagesSignal.value = [];
    filesSignal.value = [];
    runsSignal.value = [];
    lineageSignal.value = null;
    selectedFileIdsSignal.value = [];
    contextStateSignal.value = 'idle';
    messagesStateSignal.value = 'idle';
    filesStateSignal.value = 'idle';
    runsStateSignal.value = 'idle';
    lineageStateSignal.value = 'idle';
    return;
  }

  localStorage.setItem('last_active_space_id', spaceId);

  const cacheKey = `${reqUid}:${spaceId}:${tag}`;
  const cached = spaceMemoryCache.get(cacheKey);

  if (cached) {
    // SWR Instant populate from cache
    spaceContextSignal.value = cached.context;
    messagesSignal.value = cached.messages;
    filesSignal.value = cached.files;
    runsSignal.value = cached.runs;
    lineageSignal.value = cached.lineage;

    contextStateSignal.value = 'ready';
    messagesStateSignal.value = 'ready';
    filesStateSignal.value = 'ready';
    runsStateSignal.value = 'ready';
    lineageStateSignal.value = 'ready';
  } else {
    // Clean stale data when no cache
    spaceContextSignal.value = null;
    messagesSignal.value = [];
    filesSignal.value = [];
    runsSignal.value = [];
    lineageSignal.value = null;

    contextStateSignal.value = 'loading';
    messagesStateSignal.value = 'loading';
    filesStateSignal.value = 'loading';
    runsStateSignal.value = 'loading';
    lineageStateSignal.value = 'loading';
  }

  const signal = requestSequencer.getNextSignal();

  const isValidRequest = () =>
    reqSeq === activeLoaderSeq &&
    currentUserSignal.value?.uid === reqUid &&
    activeSpaceIdSignal.value === reqSpaceId &&
    activeTagSignal.value === reqTag;

  // 1. Primary Phase: Space Context (Critical Path, 25s timeout for cold starts)
  const primaryMerged = createMergedAbortSignal(signal, 25000);
  try {
    const ctx = await api.getSpaceContext(spaceId, primaryMerged.signal);
    if (isValidRequest()) {
      spaceContextSignal.value = ctx;
      contextStateSignal.value = 'ready';
    }
  } catch (err) {
    if (isValidRequest()) {
      console.error('Failed to load space context:', err);
      contextStateSignal.value = 'error';
    }
    primaryMerged.cleanup();
    return; // Halt secondary fetches if Context fails
  }
  primaryMerged.cleanup();

  // If active space/tag/user changed during primary phase, halt secondary fetches
  if (!isValidRequest()) {
    return;
  }

  // 2. Secondary Phase: Parallel, Non-blocking for Messages, Files, Runs, Lineage (25s timeout)
  const secMerged = createMergedAbortSignal(signal, 25000);

  const fetchMessagesWithRetry = async (retryCount = 0): Promise<void> => {
    try {
      const res = await api.listMessagesPage(spaceId, tag, 30, undefined, secMerged.signal);
      if (isValidRequest()) {
        messagesSignal.value = res.items;
        messagesNextCursorSignal.value = res.next_cursor || null;
        messagesHasMoreSignal.value = res.has_more;
        messagesStateSignal.value = 'ready';
      }
    } catch (err) {
      if (isValidRequest()) {
        if (retryCount < 1) {
          console.warn('Initial messages fetch failed (likely cold start), retrying in 1.2s...', err);
          await new Promise((r) => setTimeout(r, 1200));
          if (isValidRequest()) {
            return fetchMessagesWithRetry(retryCount + 1);
          }
        }
        console.error('Failed to load messages page:', err);
        messagesStateSignal.value = 'error';
      }
    }
  };

  const pMessages = fetchMessagesWithRetry();

  const pFiles = api
    .listFiles(spaceId, tag, secMerged.signal)
    .then((files) => {
      if (isValidRequest()) {
        filesSignal.value = files;
        filesStateSignal.value = 'ready';
      }
    })
    .catch((err) => {
      if (isValidRequest()) {
        console.warn('Failed to load files:', err);
        filesStateSignal.value = 'error';
      }
    });

  const pRuns = api
    .listRuns(spaceId, tag, secMerged.signal)
    .then((runs) => {
      if (isValidRequest()) {
        runsSignal.value = runs;
        runsStateSignal.value = 'ready';
      }
    })
    .catch((err) => {
      if (isValidRequest()) {
        console.warn('Failed to load runs:', err);
        runsStateSignal.value = 'error';
      }
    });

  const pLineage = api
    .getLineage(spaceId, tag, secMerged.signal)
    .then((lineage) => {
      if (isValidRequest()) {
        lineageSignal.value = lineage;
        lineageStateSignal.value = 'ready';
      }
    })
    .catch((err) => {
      if (isValidRequest()) {
        console.warn('Failed to load lineage:', err);
        lineageStateSignal.value = 'error';
      }
    });

  await Promise.allSettled([pMessages, pFiles, pRuns, pLineage]).finally(() => {
    secMerged.cleanup();
  });

  if (isValidRequest() && spaceContextSignal.value) {
    spaceMemoryCache.set(cacheKey, {
      context: spaceContextSignal.value,
      messages: messagesSignal.value,
      files: filesSignal.value,
      runs: runsSignal.value,
      lineage: lineageSignal.value,
      cachedAt: Date.now(),
    });
  }
}
