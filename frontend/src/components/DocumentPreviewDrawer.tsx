import { JSX } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import { Citation, DocumentChunkSummary } from '../types';
import { api } from '../services/api';

interface DocumentPreviewDrawerProps {
  citation: Citation | null;
  spaceId: string;
  onClose: () => void;
}

export function DocumentPreviewDrawer({
  citation,
  spaceId,
  onClose,
}: DocumentPreviewDrawerProps): JSX.Element | null {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isUnavailable, setIsUnavailable] = useState(false);
  const [chunkData, setChunkData] = useState<DocumentChunkSummary | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedElementRef = useRef<HTMLElement | null>(null);

  // Request ID guard and abort controller to prevent race conditions on rapid citation switching or retries
  const activeRequestIdRef = useRef(0);
  const abortControllerRef = useRef<AbortController | null>(null);

  const loadCitation = (targetCitation: Citation, targetSpaceId: string) => {
    const reqId = ++activeRequestIdRef.current;

    // Abort previous in-flight request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const ac = new AbortController();
    abortControllerRef.current = ac;

    setLoading(true);
    setError(null);
    setIsUnavailable(false);
    setChunkData(null);

    api
      .getCitationChunk(
        targetSpaceId,
        targetCitation.file_id,
        targetCitation.chunk_id,
        targetCitation.generation,
        ac.signal
      )
      .then((data) => {
        // Discard result if a newer citation was selected in the meantime
        if (activeRequestIdRef.current !== reqId) return;
        setChunkData(data);
        setLoading(false);
      })
      .catch((err: any) => {
        if (activeRequestIdRef.current !== reqId) return;
        if (err.name === 'AbortError') return;

        if (
          err.message &&
          (err.message.includes('404') ||
            err.message.includes('SOURCE_GENERATION_UNAVAILABLE'))
        ) {
          setIsUnavailable(true);
        } else {
          setError(err.message || 'Failed to load original citation snippet');
        }
        setLoading(false);
      });
  };

  useEffect(() => {
    if (!citation || !spaceId) {
      activeRequestIdRef.current++;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      setChunkData(null);
      setError(null);
      setIsUnavailable(false);
      return;
    }

    if (!previouslyFocusedElementRef.current && document.activeElement instanceof HTMLElement) {
      previouslyFocusedElementRef.current = document.activeElement;
    }

    loadCitation(citation, spaceId);

    // Focus management: move focus into the close button for accessibility
    const timer = setTimeout(() => {
      closeButtonRef.current?.focus();
    }, 50);

    return () => {
      clearTimeout(timer);
      activeRequestIdRef.current++;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      if (previouslyFocusedElementRef.current && typeof previouslyFocusedElementRef.current.focus === 'function') {
        previouslyFocusedElementRef.current.focus();
        previouslyFocusedElementRef.current = null;
      }
    };
  }, [citation?.chunk_id, citation?.generation, citation?.file_id, spaceId]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && citation) {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [citation, onClose]);

  if (!citation) return null;

  const locatorDisplay = citation.page_number
    ? `Page ${citation.page_number}`
    : citation.source_locator || 'Page 1';

  const handleRetry = () => {
    if (citation && spaceId) {
      loadCitation(citation, spaceId);
    }
  };

  const renderHighlightedContent = () => {
    if (!chunkData) {
      return (
        <div class="preview-drawer-content-box font-mono">
          <mark class="preview-drawer-highlight">{citation.snippet}</mark>
        </div>
      );
    }

    const raw = chunkData.normalized_text || (chunkData as any).raw_text || '';
    if (!raw) {
      return <div class="preview-drawer-content-box font-mono">No content</div>;
    }

    const snippet = (citation.snippet || '').trim();

    // 1. Precise character-range verification & slicing:
    // Sliced substring in raw text MUST EXACTLY equal snippet (trimmed)
    let isOffsetVerified = false;
    let relStart = 0;
    let relEnd = 0;

    if (
      typeof citation.char_start === 'number' &&
      typeof citation.char_end === 'number' &&
      typeof chunkData.char_start === 'number' &&
      citation.char_end > citation.char_start &&
      citation.char_start >= chunkData.char_start
    ) {
      relStart = citation.char_start - chunkData.char_start;
      relEnd = citation.char_end - chunkData.char_start;

      if (relStart >= 0 && relEnd <= raw.length && relStart < relEnd) {
        const sliced = raw.slice(relStart, relEnd).trim();
        if (snippet && sliced === snippet) {
          isOffsetVerified = true;
        }
      }
    }

    if (isOffsetVerified) {
      const before = raw.slice(0, relStart);
      const match = raw.slice(relStart, relEnd);
      const after = raw.slice(relEnd);

      return (
        <div class="preview-drawer-content-box">
          {before}
          <mark class="preview-drawer-highlight">{match}</mark>
          {after}
        </div>
      );
    }

    // 2. Substring matching fallback:
    // Only highlight if snippet occurs uniquely in raw text and is non-trivial (>= 4 chars)
    if (snippet && snippet.length >= 4) {
      const firstIdx = raw.indexOf(snippet);
      const lastIdx = raw.lastIndexOf(snippet);
      if (firstIdx !== -1 && firstIdx === lastIdx) {
        const before = raw.slice(0, firstIdx);
        const match = raw.slice(firstIdx, firstIdx + snippet.length);
        const after = raw.slice(firstIdx + snippet.length);

        return (
          <div class="preview-drawer-content-box">
            {before}
            <mark class="preview-drawer-highlight">{match}</mark>
            {after}
          </div>
        );
      }
    }

    // 3. Snippet mismatch, ambiguous occurrences, or un-locatable:
    // Do NOT highlight arbitrary or entire text! Display warning notice and render plain text cleanly.
    return (
      <div class="preview-drawer-content-box">
        <div class="preview-drawer-alert alert-warning" style="margin-bottom: 12px;">
          <div class="preview-drawer-alert-title">⚠️ Exact citation location mismatch</div>
          <p class="preview-drawer-alert-desc">Exact text offset could not be automatically locked in this chunk. Full passage is displayed below for manual verification.</p>
        </div>
        <div class="preview-drawer-plain-text">{raw}</div>
      </div>
    );
  };

  return (
    <div
      class="preview-drawer-overlay"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="preview-drawer-title"
    >
      <div
        class="preview-drawer-card"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div class="preview-drawer-header">
          <div class="preview-drawer-title-group">
            <span class="preview-drawer-badge">{citation.index}</span>
            <div class="preview-drawer-title-col">
              <h2
                id="preview-drawer-title"
                class="preview-drawer-title"
                title={citation.filename}
              >
                {citation.filename}
              </h2>
              <div class="preview-drawer-meta">
                <span>{locatorDisplay}</span>
                <span>•</span>
                <span class="gen-tag">Gen #{citation.generation}</span>
              </div>
            </div>
          </div>

          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close preview drawer / 關閉預覽抽屜"
            class="preview-drawer-close-btn"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div class="preview-drawer-body">
          {loading && (
            <div class="preview-drawer-loading">
              <div class="preview-skeleton-bar" style={{ width: '70%' }} />
              <div class="preview-skeleton-bar" style={{ width: '90%' }} />
              <div class="preview-skeleton-box" />
            </div>
          )}

          {isUnavailable && (
            <div class="preview-drawer-alert alert-unavailable">
              <div class="preview-drawer-alert-title">
                ⚠️ Source Version Unavailable
              </div>
              <p class="preview-drawer-alert-desc">
                The file version corresponding to this citation (Gen #{citation.generation}) has been reindexed or purged. To ensure provenance authenticity, it will not be automatically substituted with newer versions.
              </p>
            </div>
          )}

          {error && !isUnavailable && (
            <div class="preview-drawer-alert alert-error">
              <div class="preview-drawer-alert-title">
                ⚠️ Loading Failed
              </div>
              <p class="preview-drawer-alert-desc">{error}</p>
              <button
                type="button"
                onClick={handleRetry}
                class="preview-drawer-btn-retry"
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !isUnavailable && !error && (
            <>
              <div class="preview-drawer-section-header">
                <span>Original Citation Snippet</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '10px' }}>
                  Hash: {citation.content_hash.slice(0, 10)}...
                </span>
              </div>

              {renderHighlightedContent()}

              <div class="preview-drawer-meta-box">
                <div class="meta-row">
                  <span class="meta-label">Source File:</span>
                  <span>{citation.filename}</span>
                </div>
                <div class="meta-row">
                  <span class="meta-label">Location:</span>
                  <span>{locatorDisplay} (offset {citation.char_start} - {citation.char_end})</span>
                </div>
                <div class="meta-row">
                  <span class="meta-label">Relevance Score:</span>
                  <span class="meta-value">{citation.score}</span>
                </div>
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div class="preview-drawer-footer">
          <button
            type="button"
            onClick={onClose}
            class="preview-drawer-btn-close"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
