import { JSX } from 'preact';
import { useState, useMemo, useRef, useEffect } from 'preact/hooks';
import {
  filesSignal,
  spaceContextSignal,
  activeTagSignal,
  activeSpaceIdSignal,
  fileSearchQuerySignal,
  fileStatusFilterSignal,
  fileSortSignal,
  selectedFileIdsSignal,
  activeViewSignal,
  composerContextFileIdsSignal,
  composerIntentSignal,
  filesStateSignal,
} from '../services/store';
import { currentUserSignal } from '../services/auth';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { FileRecord } from '../types';
import { DocumentViewerModal } from './DocumentViewerModal';
import { promptConfirm } from './primitives/ConfirmDialog';

export function FileCenter(): JSX.Element {
  const [previewFile, setPreviewFile] = useState<FileRecord | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const navEpochRef = useRef<number>(0);
  const uploadOpIdRef = useRef<number>(0);
  const reindexOpIdRef = useRef<number>(0);
  const pollGenRef = useRef<number>(0);

  const files = filesSignal.value;
  const context = spaceContextSignal.value;
  const activeTag = activeTagSignal.value;
  const searchQuery = fileSearchQuerySignal.value;
  const statusFilter = fileStatusFilterSignal.value;
  const sortOrder = fileSortSignal.value;
  const selectedIds = selectedFileIdsSignal.value;
  const filesState = filesStateSignal.value;

  const spaceId = context?.space.space_id || '';
  const canManage = Boolean(
    context?.capabilities?.can_manage_tags ||
    context?.current_user_role === 'owner' ||
    context?.current_user_role === 'admin' ||
    context?.current_user_role === 'coordinator'
  );

  // Invalidate in-flight operations and reset uploading state on navigation (space / tag / user switch)
  useEffect(() => {
    navEpochRef.current++;
    setIsUploading(false);

    return () => {
      navEpochRef.current++;
    };
  }, [spaceId, activeTag]);

  // Background Ingestion Polling for processing files
  useEffect(() => {
    if (!spaceId) return;
    const epoch = navEpochRef.current;
    const currentTag = activeTag;
    const currentUid = currentUserSignal.value?.uid;
    const hasProcessing = files.some((f) =>
      ['pending', 'extracting', 'indexing'].includes(f.ingestion_status || '')
    );
    if (!hasProcessing) return;

    const interval = setInterval(async () => {
      const pollGen = ++pollGenRef.current;
      if (
        epoch !== navEpochRef.current ||
        activeSpaceIdSignal.value !== spaceId ||
        activeTagSignal.value !== currentTag ||
        currentUserSignal.value?.uid !== currentUid
      ) {
        return;
      }
      try {
        const refreshed = await api.listFiles(spaceId, currentTag !== 'all' ? currentTag : undefined);
        if (
          pollGen === pollGenRef.current &&
          epoch === navEpochRef.current &&
          activeSpaceIdSignal.value === spaceId &&
          activeTagSignal.value === currentTag &&
          currentUserSignal.value?.uid === currentUid
        ) {
          filesSignal.value = refreshed;
        }
      } catch (err) {
        console.warn('Background ingestion polling error:', err);
      }
    }, 2500);

    return () => {
      pollGenRef.current++;
      clearInterval(interval);
    };
  }, [spaceId, activeTag, files]);

  // Filter and Sort logic
  const filteredFiles = useMemo(() => {
    return files.filter((f) => {
      // 1. Tag filter
      if (activeTag && activeTag !== 'all') {
        const fileTags = f.project_tags || [];
        if (!fileTags.includes(activeTag)) return false;
      }

      // 2. Status filter
      if (statusFilter !== 'all') {
        if (statusFilter === 'ready' && f.ingestion_status !== 'ready') return false;
        if (statusFilter === 'ready_partial' && f.ingestion_status !== 'ready_partial') return false;
        if (statusFilter === 'needs_ocr' && f.ingestion_status !== 'needs_ocr') return false;
        if (statusFilter === 'processing' && !['pending', 'extracting', 'indexing'].includes(f.ingestion_status || '')) return false;
        if (statusFilter === 'failed' && f.ingestion_status !== 'failed') return false;
      }

      // 3. Search query
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchName = (f.filename || '').toLowerCase().includes(q);
        const matchUploader = (f.uploaded_by || '').toLowerCase().includes(q);
        if (!matchName && !matchUploader) return false;
      }

      return true;
    });
  }, [files, activeTag, statusFilter, searchQuery]);

  const sortedFiles = useMemo(() => {
    return [...filteredFiles].sort((a, b) => {
      switch (sortOrder) {
        case 'uploaded_at_desc':
          return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
        case 'uploaded_at_asc':
          return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
        case 'name_asc':
          return (a.filename || '').localeCompare(b.filename || '');
        case 'size_desc':
          return (b.size_bytes || 0) - (a.size_bytes || 0);
        case 'updated_at_desc':
        case 'updated_at_asc':
        default:
          return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
      }
    });
  }, [filteredFiles, sortOrder]);

  const handleSelectFile = (fileId: string) => {
    if (selectedIds.includes(fileId)) {
      selectedFileIdsSignal.value = selectedIds.filter((id) => id !== fileId);
    } else {
      selectedFileIdsSignal.value = [...selectedIds, fileId];
    }
  };

  const handleSelectAll = () => {
    if (selectedIds.length === sortedFiles.length) {
      selectedFileIdsSignal.value = [];
    } else {
      selectedFileIdsSignal.value = sortedFiles.map((f) => f.file_id);
    }
  };

  const handleAskAIWithSelected = () => {
    if (selectedIds.length === 0) return;
    composerContextFileIdsSignal.value = [...selectedIds];
    composerIntentSignal.value = 'document_qa';
    activeViewSignal.value = 'chat';
    showToast(`Selected ${selectedIds.length} document(s) for chat Q&A`, 'info');
  };

  const handleAskAIForSingleFile = (file: FileRecord) => {
    composerContextFileIdsSignal.value = [file.file_id];
    composerIntentSignal.value = 'document_qa';
    activeViewSignal.value = 'chat';
  };

  const handlePreviewFile = (file: FileRecord) => {
    setPreviewFile(file);
  };

  const handleDownloadFile = async (file: FileRecord) => {
    if (!spaceId) return;
    try {
      showToast(`Downloading '${file.filename}'...`, 'info');
      await api.downloadFile(spaceId, file.file_id, file.filename);
      showToast(`'${file.filename}' downloaded successfully`, 'success');
    } catch (err: any) {
      showToast(err.message || 'Download failed', 'error');
    }
  };

  const handleDeleteFile = async (file: FileRecord) => {
    if (!spaceId) return;
    const { confirmed } = await promptConfirm({
      title: 'Delete File',
      message: `Are you sure you want to permanently delete "${file.filename}"? This action cannot be undone.`,
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      isDestructive: true,
    });
    if (!confirmed) return;

    try {
      showToast(`Deleting '${file.filename}'...`, 'info');
      await api.deleteFile(spaceId, file.file_id);
      filesSignal.value = filesSignal.value.filter((f) => f.file_id !== file.file_id);
      selectedFileIdsSignal.value = selectedFileIdsSignal.value.filter((id) => id !== file.file_id);
      showToast(`'${file.filename}' deleted successfully`, 'success');
    } catch (err: any) {
      showToast(err.message || 'Failed to delete file', 'error');
    }
  };

  const handleReindex = async (file: FileRecord) => {
    if (!spaceId) return;
    const opId = ++reindexOpIdRef.current;
    const epoch = navEpochRef.current;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    const reqUid = currentUserSignal.value?.uid;
    try {
      showToast(`Triggering reindex for '${file.filename}'...`, 'info');
      await api.reindexFile(reqSpaceId, file.file_id);
      showToast(`Dispatched reindex task for '${file.filename}'`, 'success');
      // Refresh files list if still in same space/tag/user/epoch/op
      if (
        opId === reindexOpIdRef.current &&
        epoch === navEpochRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag &&
        currentUserSignal.value?.uid === reqUid
      ) {
        const freshFiles = await api.listFiles(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (
          opId === reindexOpIdRef.current &&
          epoch === navEpochRef.current &&
          activeSpaceIdSignal.value === reqSpaceId &&
          activeTagSignal.value === reqTag &&
          currentUserSignal.value?.uid === reqUid
        ) {
          filesSignal.value = freshFiles;
        }
      }
    } catch (err: any) {
      if (
        opId === reindexOpIdRef.current &&
        epoch === navEpochRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag &&
        currentUserSignal.value?.uid === reqUid
      ) {
        showToast(err.message || 'Failed to trigger reindex', 'error');
      }
    }
  };

  const handleFileUpload = async (e: Event) => {
    const target = e.target as HTMLInputElement;
    const file = target.files?.[0];
    if (!file || !spaceId) return;

    const opId = ++uploadOpIdRef.current;
    const epoch = navEpochRef.current;
    const uploadSpaceId = spaceId;
    const uploadTag = activeTag;
    const uploadUid = currentUserSignal.value?.uid;
    setIsUploading(true);
    try {
      showToast(`Uploading ${file.name}...`, 'info');
      const tagToSend = uploadTag && uploadTag !== 'all' ? uploadTag : 'general';
      const uploaded = await api.uploadFileWithProgress(file, uploadSpaceId, tagToSend);
      if (
        opId === uploadOpIdRef.current &&
        epoch === navEpochRef.current &&
        activeSpaceIdSignal.value === uploadSpaceId &&
        activeTagSignal.value === uploadTag &&
        currentUserSignal.value?.uid === uploadUid
      ) {
        filesSignal.value = [uploaded, ...filesSignal.value];
        showToast(`Uploaded successfully: ${file.name}`, 'success');
      }
    } catch (err: any) {
      if (
        opId === uploadOpIdRef.current &&
        epoch === navEpochRef.current &&
        activeSpaceIdSignal.value === uploadSpaceId &&
        activeTagSignal.value === uploadTag &&
        currentUserSignal.value?.uid === uploadUid
      ) {
        showToast(err.message || 'Upload failed', 'error');
      }
    } finally {
      if (opId === uploadOpIdRef.current) {
        setIsUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const renderStatusBadge = (file: FileRecord) => {
    const status = file.ingestion_status;
    const activeGen = file.active_generation || 0;
    const chunkCount = file.chunk_count || 0;

    if (status === 'ready') {
      return (
        <span class="file-status-badge badge-ready" title={`Gen #${activeGen}, contains ${chunkCount} indexed chunks (Fully searchable)`}>
          ✓ Ready (Gen #{activeGen})
        </span>
      );
    }

    if (status === 'ready_partial') {
      const gapStr = file.ocr_gap_pages?.length ? ` (pages ${file.ocr_gap_pages.join(', ')} lack text layer)` : '';
      return (
        <span class="file-status-badge badge-warning" title={`Scanned image pages detected${gapStr}, other text pages are searchable`}>
          ⚠ Partial{gapStr}
        </span>
      );
    }

    if (status === 'needs_ocr') {
      return (
        <span class="file-status-badge badge-warning" title="Scanned PDF without readable text layer. Requires OCR before search and Q&A.">
          📷 Needs OCR
        </span>
      );
    }

    if (['pending', 'extracting', 'indexing'].includes(status || '')) {
      const reindexingNotice = activeGen > 0 ? ` (Gen #${activeGen} still available)` : '';
      return (
        <span class="file-status-badge badge-processing" title="Indexing and semantic chunking in background">
          <span class="spinner-inline" /> Indexing{reindexingNotice}
        </span>
      );
    }

    if (status === 'failed') {
      const errMsg = file.ingestion_error_message || 'Ingestion failed';
      return (
        <span class="file-status-badge badge-failed" title={errMsg}>
          ✕ Failed: {errMsg}
        </span>
      );
    }

    return <span class="file-status-badge badge-pending">Uploaded</span>;
  };

  return (
    <div class="file-center-container">
      {/* Top Header & Search Bar */}
      <div class="file-center-header">
        <div class="file-center-title-row">
          <div class="file-center-heading-group">
            <h1 class="file-center-title">File Center</h1>
            <span class="file-center-count-pill">{filteredFiles.length} {filteredFiles.length === 1 ? 'File' : 'Files'}</span>
          </div>
          <div class="file-center-actions">
            <input
              id="file-upload-input"
              ref={fileInputRef}
              type="file"
              style="display: none"
              accept=".pdf,.fdx,.fountain,.doc,.docx,.xlsx,.csv,.txt,.pptx"
              onChange={handleFileUpload}
              aria-label="Upload document file"
            />
            <button
              id="btn-upload-file"
              class="btn-primary file-upload-btn"
              type="button"
              disabled={isUploading}
              onClick={() => fileInputRef.current?.click()}
            >
              {isUploading ? 'Uploading...' : '+ Upload File'}
            </button>
          </div>
        </div>

        {/* Filter Controls Bar */}
        <div class="file-center-controls-bar">
          {/* Search Input */}
          <div class="file-search-box">
            <input
              type="text"
              class="file-search-input"
              placeholder="Search filename or uploader..."
              value={searchQuery}
              onInput={(e) => {
                fileSearchQuerySignal.value = (e.target as HTMLInputElement).value;
              }}
              aria-label="Search files"
            />
            {searchQuery && (
              <button
                class="search-clear-btn"
                type="button"
                onClick={() => {
                  fileSearchQuerySignal.value = '';
                }}
              >
                ✕
              </button>
            )}
          </div>

          {/* Status Filter Dropdown */}
          <div class="file-filter-dropdown-group">
            <label class="control-label" htmlFor="status-filter-select">Status:</label>
            <select
              id="status-filter-select"
              class="file-select-control"
              value={statusFilter}
              onChange={(e) => {
                fileStatusFilterSignal.value = (e.target as HTMLSelectElement).value;
              }}
            >
              <option value="all">All Statuses</option>
              <option value="ready">Ready</option>
              <option value="ready_partial">Partial (Scanned)</option>
              <option value="needs_ocr">Needs OCR</option>
              <option value="processing">Processing</option>
              <option value="failed">Failed</option>
            </select>
          </div>

          {/* Sort Selector Dropdown */}
          <div class="file-filter-dropdown-group">
            <label class="control-label" htmlFor="sort-select">Sort by:</label>
            <select
              id="sort-select"
              class="file-select-control"
              value={sortOrder}
              onChange={(e) => {
                fileSortSignal.value = (e.target as HTMLSelectElement).value as any;
              }}
            >
              <option value="uploaded_at_desc">Newest Upload</option>
              <option value="uploaded_at_asc">Oldest Upload</option>
              <option value="name_asc">Name A-Z</option>
              <option value="size_desc">File Size</option>
            </select>
          </div>
        </div>

        {/* Tag Pills Filter Selector */}
        <div class="file-tag-pills-row" role="tablist" aria-label="Filter files by tag">
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
              class={`tag-pill-btn ${activeTag === t.slug ? 'active' : ''} ${t.archived ? 'archived' : ''}`}
              style={activeTag === t.slug ? { borderColor: t.color, backgroundColor: `${t.color}22` } : {}}
              onClick={() => {
                activeTagSignal.value = t.slug;
              }}
            >
              <span class="tag-color-dot" style={{ backgroundColor: t.color }} />
              {t.name}
              {t.archived && <span class="archived-label">(Archived)</span>}
            </button>
          ))}
        </div>
      </div>

      {/* Batch Selection Action Floating Bar */}
      {selectedIds.length > 0 && (
        <div class="file-batch-bar" role="toolbar" aria-label="Batch operations toolbar">
          <span class="batch-count-text">Selected <strong>{selectedIds.length}</strong> document(s)</span>
          <div class="batch-buttons">
            <button class="btn-primary btn-batch-qa" data-testid="batch-ask-ai" type="button" onClick={handleAskAIWithSelected}>
              💬 Ask AI ({selectedIds.length})
            </button>
            <button
              class="btn-secondary"
              type="button"
              onClick={() => {
                selectedFileIdsSignal.value = [];
              }}
            >
              Deselect All
            </button>
          </div>
        </div>
      )}

      {/* Main File Table / List */}
      <div class="file-list-card">
        {filesState === 'loading' && files.length === 0 ? (
          <div class="file-loading-state">
            <div class="spinner-large" />
            <p>Loading files list...</p>
          </div>
        ) : filteredFiles.length === 0 ? (
          <div class="file-empty-state">
            <p class="empty-title">No Matching Files Found</p>
            <p class="empty-subtitle">
              {searchQuery || statusFilter !== 'all' || activeTag !== 'all'
                ? 'Try adjusting your search query or clear filter criteria'
                : 'Click "+ Upload File" at top right to import scripts or production documents'}
            </p>
          </div>
        ) : (
          <table class="file-table" aria-label="Files list">
            <thead>
              <tr>
                <th style="width: 40px;">
                  <input
                    type="checkbox"
                    aria-label="Select all files"
                    checked={selectedIds.length > 0 && selectedIds.length === filteredFiles.length}
                    onChange={handleSelectAll}
                  />
                </th>
                <th>File Name</th>
                <th>Tags</th>
                <th>Size</th>
                <th>Status</th>
                <th>Uploaded</th>
                <th style="text-align: right;">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredFiles.map((file) => {
                const isSelected = selectedIds.includes(file.file_id);
                const isCallableForQA = ['ready', 'ready_partial'].includes(file.ingestion_status || '');
                const canDelete = canManage || file.uploaded_by === currentUserSignal.value?.uid;
                return (
                  <tr key={file.file_id} data-file-id={file.file_id} class={isSelected ? 'selected-row' : ''}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${file.filename}`}
                        checked={isSelected}
                        onChange={() => handleSelectFile(file.file_id)}
                      />
                    </td>
                    <td>
                      <div class="filename-cell">
                        <span class="file-mimetype-icon">📄</span>
                        <span class="filename-text" title={file.filename}>{file.filename}</span>
                      </div>
                    </td>
                    <td>
                      <div class="file-tags-group">
                        {(file.project_tags || ['general']).map((tagSlug) => {
                          const tagDef = context?.space.tags.find((t) => t.slug === tagSlug);
                          return (
                            <span
                              key={tagSlug}
                              class="table-tag-chip"
                              style={tagDef?.color ? { borderColor: tagDef.color, color: tagDef.color } : {}}
                            >
                              {tagDef?.name || tagSlug}
                            </span>
                          );
                        })}
                      </div>
                    </td>
                    <td>{formatFileSize(file.size_bytes)}</td>
                    <td>{renderStatusBadge(file)}</td>
                    <td>{file.created_at ? new Date(file.created_at).toLocaleDateString() : '-'}</td>
                    <td>
                      <div class="file-row-actions">
                        <button
                          class="action-btn"
                          type="button"
                          title="Preview document text and chunk snippets"
                          onClick={() => handlePreviewFile(file)}
                        >
                          Preview
                        </button>
                        <button
                          class="action-btn"
                          type="button"
                          title="Download file"
                          onClick={() => handleDownloadFile(file)}
                        >
                          Download
                        </button>
                        {isCallableForQA && (
                          <button
                            class="action-btn"
                            data-testid="row-ask-ai"
                            type="button"
                            title="Ask AI with this document"
                            onClick={() => handleAskAIForSingleFile(file)}
                          >
                            Ask AI
                          </button>
                        )}
                        {file.ingestion_status === 'failed' && canManage && (
                          <button
                            class="action-btn action-retry-btn"
                            type="button"
                            title="Retry document ingestion"
                            onClick={() => handleReindex(file)}
                          >
                            Retry
                          </button>
                        )}
                        {canManage && file.ingestion_status !== 'failed' && (
                          <button
                            class="action-btn"
                            type="button"
                            title="Manually trigger reindexing"
                            onClick={() => handleReindex(file)}
                          >
                            Reindex
                          </button>
                        )}
                        {canDelete && (
                          <button
                            class="action-btn action-delete-btn"
                            type="button"
                            title="Delete file permanently"
                            style={{ color: 'var(--color-danger, #EF4444)' }}
                            onClick={() => handleDeleteFile(file)}
                          >
                            Delete
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Real Document Viewer Modal */}
      {previewFile && (
        <DocumentViewerModal
          file={previewFile}
          spaceId={spaceId}
          onClose={() => setPreviewFile(null)}
        />
      )}
    </div>
  );
}
