import { JSX } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import { FileRecord, DocumentChunkSummary } from '../types';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import {
  composerContextFileIdsSignal,
  composerIntentSignal,
  activeViewSignal,
} from '../services/store';

interface DocumentViewerModalProps {
  file: FileRecord;
  spaceId: string;
  onClose: () => void;
}

function renderHighlightedText(text: string, query: string): JSX.Element | string {
  if (!query.trim() || !text) return text;
  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const parts = text.split(new RegExp(`(${escapedQuery})`, 'gi'));
  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === query.toLowerCase() ? (
          <mark key={i}>{part}</mark>
        ) : (
          part
        )
      )}
    </>
  );
}

export function DocumentViewerModal({ file, spaceId, onClose }: DocumentViewerModalProps): JSX.Element {
  const [chunks, setChunks] = useState<DocumentChunkSummary[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [totalChunks, setTotalChunks] = useState<number>(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [isDownloading, setIsDownloading] = useState(false);
  const modalRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedElementRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocusedElementRef.current = document.activeElement as HTMLElement;
    modalRef.current?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      previouslyFocusedElementRef.current?.focus();
    };
  }, [onClose]);

  useEffect(() => {
    const activeGen = file.active_generation || 0;
    if (activeGen <= 0 || !['ready', 'ready_partial'].includes(file.ingestion_status || '')) {
      setChunks([]);
      setTotalChunks(0);
      setNextCursor(null);
      return;
    }

    let isMounted = true;
    setIsLoading(true);
    api
      .getChunks(spaceId, file.file_id, activeGen, 0, 50)
      .then((res) => {
        if (isMounted) {
          setChunks(res.items || []);
          setTotalChunks(res.total || 0);
          setNextCursor(res.has_more ? res.next_cursor || null : null);
        }
      })
      .catch((err) => {
        if (isMounted) {
          console.warn('Failed to load document chunks:', err);
        }
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [spaceId, file.file_id, file.active_generation, file.ingestion_status]);

  const handleLoadMore = async () => {
    const activeGen = file.active_generation || 0;
    if (nextCursor === null || isLoadingMore || activeGen <= 0) return;

    setIsLoadingMore(true);
    try {
      const res = await api.getChunks(spaceId, file.file_id, activeGen, nextCursor, 50);
      setChunks((prev) => [...prev, ...(res.items || [])]);
      setNextCursor(res.has_more ? res.next_cursor || null : null);
    } catch (err) {
      console.warn('Failed to load more chunks:', err);
    } finally {
      setIsLoadingMore(false);
    }
  };

  const handleDownload = async () => {
    setIsDownloading(true);
    try {
      showToast(`Downloading '${file.filename}'...`, 'info');
      await api.downloadFile(spaceId, file.file_id, file.filename);
      showToast(`'${file.filename}' downloaded successfully`, 'success');
    } catch (err: any) {
      showToast(err.message || 'Download failed', 'error');
    } finally {
      setIsDownloading(false);
    }
  };

  const handleAskAI = () => {
    composerContextFileIdsSignal.value = [file.file_id];
    composerIntentSignal.value = 'document_qa';
    activeViewSignal.value = 'chat';
    onClose();
  };

  const filteredChunks = chunks.filter((c) => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    return (
      (c.normalized_text || '').toLowerCase().includes(q) ||
      (c.source_locator || '').toLowerCase().includes(q)
    );
  });

  const formatFileSize = (bytes?: number) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  return (
    <div
      class="doc-viewer-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={modalRef}
        class="doc-viewer-card"
        role="dialog"
        aria-modal="true"
        aria-label={`Document Viewer - ${file.filename}`}
        tabIndex={-1}
      >
        {/* Header */}
        <div class="doc-viewer-header">
          <div class="doc-viewer-header-info">
            <div class="doc-viewer-title-row">
              <span class="doc-viewer-icon">📄</span>
              <h2 class="doc-viewer-title" title={file.filename}>{file.filename}</h2>
            </div>
            <div class="doc-viewer-meta-row">
              <span>Size: {formatFileSize(file.size_bytes)}</span>
              <span>Gen: #{file.active_generation || 0}</span>
              <span>Loaded chunks: {chunks.length} / {totalChunks || file.chunk_count || chunks.length}</span>
              <span>Status: <strong>{(file.ingestion_status || 'PENDING').toUpperCase()}</strong></span>
            </div>
          </div>
          <div class="doc-viewer-header-actions">
            <button
              type="button"
              class="btn-secondary btn-compact"
              disabled={isDownloading}
              onClick={handleDownload}
            >
              {isDownloading ? 'Downloading...' : '⬇ Download Original'}
            </button>
            <button
              type="button"
              class="btn-primary btn-compact"
              onClick={handleAskAI}
            >
              💬 Ask AI
            </button>
            <button
              type="button"
              class="doc-viewer-close-btn"
              onClick={onClose}
              aria-label="Close viewer"
            >
              ✕
            </button>
          </div>
        </div>

        {/* OCR Gap Warning Banner */}
        {file.ocr_gap_pages && file.ocr_gap_pages.length > 0 && (
          <div class="doc-viewer-alert-banner alert-warning">
            <span>⚠ Some pages lack a text layer (pages {file.ocr_gap_pages.join(', ')}). Content on these pages may not be fully indexable by AI.</span>
          </div>
        )}

        {/* Search within document input */}
        {chunks.length > 0 && (
          <div class="doc-viewer-search-bar">
            <input
              type="text"
              class="doc-search-input"
              placeholder="Search within document..."
              value={searchQuery}
              onInput={(e) => setSearchQuery((e.target as HTMLInputElement).value)}
            />
            {searchQuery && (
              <span class="doc-search-count">Found {filteredChunks.length} matches in {chunks.length} loaded chunks</span>
            )}
          </div>
        )}

        {/* Content Body */}
        <div class="doc-viewer-body">
          {isLoading ? (
            <div class="doc-viewer-loading">
              <div class="spinner-large" />
              <p>Loading document chunks...</p>
            </div>
          ) : file.ingestion_status === 'needs_ocr' ? (
            <div class="doc-viewer-empty-state">
              <span class="empty-icon">📷</span>
              <h3>OCR Recognition Required</h3>
              <p>This document is a scanned image PDF without a readable text layer. Text search and AI Q&A are unavailable until OCR is performed.</p>
            </div>
          ) : file.ingestion_status === 'failed' ? (
            <div class="doc-viewer-empty-state">
              <span class="empty-icon">❌</span>
              <h3>Document Ingestion Failed</h3>
              <p>{file.ingestion_error_message || 'File may be corrupted or password protected.'}</p>
            </div>
          ) : ['pending', 'extracting', 'indexing'].includes(file.ingestion_status || '') ? (
            <div class="doc-viewer-empty-state">
              <div class="spinner-large" />
              <h3>Indexing in progress...</h3>
              <p>The system is extracting text and building semantic indexes. Full content and AI Q&A will be available once indexing completes.</p>
            </div>
          ) : (
            <div class="doc-chunks-list">
              {filteredChunks.length === 0 ? (
                <div class="doc-viewer-empty-state">
                  <p>No matches found for "{searchQuery}" in {chunks.length} loaded chunks</p>
                  {nextCursor !== null && (
                    <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                      Additional chunks exist. Click below to load more chunks.
                    </p>
                  )}
                </div>
              ) : (
                filteredChunks.map((chunk, index) => (
                  <div key={chunk.chunk_id || index} class="doc-chunk-item">
                    <div class="doc-chunk-header">
                      <span class="doc-chunk-index">Chunk #{chunk.ordinal ?? index + 1}</span>
                      {chunk.source_locator && (
                        <span class="doc-chunk-locator">{chunk.source_locator}</span>
                      )}
                      {chunk.page_number && (
                        <span class="doc-chunk-page">Page {chunk.page_number}</span>
                      )}
                    </div>
                    <div class="doc-chunk-text">
                      {renderHighlightedText(chunk.normalized_text || '', searchQuery)}
                    </div>
                  </div>
                ))
              )}

              {nextCursor !== null && (
                <div style="display: flex; justify-content: center; padding-top: 12px;">
                  <button
                    type="button"
                    class="btn-secondary btn-compact"
                    disabled={isLoadingMore}
                    onClick={handleLoadMore}
                  >
                    {isLoadingMore ? 'Loading...' : `Load More Chunks (${totalChunks - chunks.length} remaining)`}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
