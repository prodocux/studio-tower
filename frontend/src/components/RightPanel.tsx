import { JSX } from 'preact';
import { useEffect, useRef } from 'preact/hooks';
import {
  activeRightTabSignal,
  activeSpaceIdSignal,
  activeTagSignal,
  spaceContextSignal,
  lineageSignal,
  runsSignal,
  filesSignal,
  lineageStateSignal,
  runsStateSignal,
  filesStateSignal,
  activeModalSignal,
  selectedLineageNodeSignal,
  composerDraftSignal,
  composerContextFileIdsSignal,
  composerContextRunIdSignal,
  composerIntentSignal,
  selectedRunIdSignal,
  selectedWaterfallTargetSignal,
  getNextWaterfallGeneration,
} from '../services/store';
import { currentUserSignal } from '../services/auth';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { promptConfirm } from './primitives/ConfirmDialog';
import { Drawer } from './primitives/Drawer';
import { LineageNode, FileRecord } from '../types';
import { getSafeGrafanaUrl } from '../utils';
import { GrafanaCloudBoard } from './GrafanaCloudBoard';
import { HACKATHON_GRAFANA_ONLY } from '../hackathon';

export function RightPanel(): JSX.Element {
  const activeTab = activeRightTabSignal.value;
  const context = spaceContextSignal.value;
  const lineage = lineageSignal.value;
  const runs = runsSignal.value;
  const files = filesSignal.value;
  const activeTag = activeTagSignal.value;
  const selectedNode = selectedLineageNodeSignal.value as LineageNode | null;
  const lineageState = lineageStateSignal.value;
  const runsState = runsStateSignal.value;
  const filesState = filesStateSignal.value;
  const selectedRunId = selectedRunIdSignal.value;

  const navEpochRef = useRef<number>(0);
  const pollGenRef = useRef<number>(0);
  const reindexOpIdRef = useRef<number>(0);

  const capabilities = context?.capabilities;
  const spaceId = context?.space.space_id || activeSpaceIdSignal.value;
  const canReindex = Boolean(context?.current_user_role && context.current_user_role !== 'member');

  // Invalidate operations on Space / Tag navigation
  useEffect(() => {
    navEpochRef.current++;
    return () => {
      navEpochRef.current++;
    };
  }, [spaceId, activeTag]);

  // Scroll into view when a Run is selected in telemetry
  useEffect(() => {
    if (selectedRunId && activeTab === 'grafana') {
      const el = document.getElementById(`run-card-${selectedRunId}`);
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  }, [selectedRunId, activeTab]);

  useEffect(() => {
    if (activeTab !== 'files' || !spaceId) return;

    const hasPending = files.some(
      (f) => f.ingestion_status && ['pending', 'extracting', 'indexing'].includes(f.ingestion_status)
    );

    if (!hasPending) return;

    const epoch = navEpochRef.current;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    const reqUid = currentUserSignal.value?.uid;

    const interval = setInterval(async () => {
      const pollGen = ++pollGenRef.current;
      if (
        epoch !== navEpochRef.current ||
        activeSpaceIdSignal.value !== reqSpaceId ||
        activeTagSignal.value !== reqTag ||
        currentUserSignal.value?.uid !== reqUid
      ) {
        return;
      }
      try {
        const updated = await api.listFiles(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (
          pollGen === pollGenRef.current &&
          epoch === navEpochRef.current &&
          activeSpaceIdSignal.value === reqSpaceId &&
          activeTagSignal.value === reqTag &&
          currentUserSignal.value?.uid === reqUid
        ) {
          filesSignal.value = updated;
        }
      } catch {
        // ignore polling errors
      }
    }, 5000);

    return () => {
      pollGenRef.current++;
      clearInterval(interval);
    };
  }, [activeTab, spaceId, activeTag, files]);

  // Active polling for telemetry runs when any run is in 'running' state
  useEffect(() => {
    if (!spaceId) return;

    const hasRunning = runs.some((r) => r.status === 'running');
    if (!hasRunning) return;

    const epoch = navEpochRef.current;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    const reqUid = currentUserSignal.value?.uid;

    const interval = setInterval(async () => {
      const pollGen = ++pollGenRef.current;
      if (
        epoch !== navEpochRef.current ||
        activeSpaceIdSignal.value !== reqSpaceId ||
        activeTagSignal.value !== reqTag ||
        currentUserSignal.value?.uid !== reqUid
      ) {
        return;
      }
      try {
        const updated = await api.listRuns(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (
          pollGen === pollGenRef.current &&
          epoch === navEpochRef.current &&
          activeSpaceIdSignal.value === reqSpaceId &&
          activeTagSignal.value === reqTag &&
          currentUserSignal.value?.uid === reqUid
        ) {
          runsSignal.value = updated;
        }
      } catch {
        // ignore polling errors
      }
    }, 3000);

    return () => {
      pollGenRef.current++;
      clearInterval(interval);
    };
  }, [spaceId, activeTag, runs]);

  const handleReindexFile = async (fileId: string) => {
    if (!spaceId) return;
    const opId = ++reindexOpIdRef.current;
    const epoch = navEpochRef.current;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    const reqUid = currentUserSignal.value?.uid;
    try {
      showToast('Dispatching re-indexing job...', 'info');
      await api.reindexFile(reqSpaceId, fileId);
      if (
        opId === reindexOpIdRef.current &&
        epoch === navEpochRef.current &&
        activeSpaceIdSignal.value === reqSpaceId &&
        activeTagSignal.value === reqTag &&
        currentUserSignal.value?.uid === reqUid
      ) {
        const updated = await api.listFiles(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (
          opId === reindexOpIdRef.current &&
          epoch === navEpochRef.current &&
          activeSpaceIdSignal.value === reqSpaceId &&
          activeTagSignal.value === reqTag &&
          currentUserSignal.value?.uid === reqUid
        ) {
          filesSignal.value = updated;
        }
      }
      showToast('Re-indexing job started.', 'success');
    } catch (err: any) {
      showToast(`Re-indexing failed: ${err.message}`, 'error');
    }
  };

  const handleApproveGate = async (runId: string) => {
    if (!spaceId) return;
    try {
      const updatedRun = await api.approveRun(spaceId, runId, true);
      runsSignal.value = runsSignal.value.map((r) => (r.run_id === runId ? updatedRun : r));
      showToast('Gate approved! Generating PDX artifacts...', 'success');
      // Refresh lineage
      const newLin = await api.getLineage(spaceId);
      lineageSignal.value = newLin;
    } catch (err: any) {
      showToast(`Approval failed: ${err.message}`, 'error');
    }
  };

  const handleRejectGate = async (runId: string) => {
    if (!spaceId) return;
    const { confirmed, value: reason } = await promptConfirm({
      title: 'Reject Approval Gate',
      message: 'Provide a reason for rejecting this production action (will be recorded in cryptographic manifest):',
      withInput: true,
      inputPlaceholder: 'e.g. Safety concern regarding weather conditions',
      confirmLabel: 'Reject Action',
      isDestructive: true,
    });
    if (!confirmed) return;

    try {
      const updatedRun = await api.approveRun(spaceId, runId, false, reason);
      runsSignal.value = runsSignal.value.map((r) => (r.run_id === runId ? updatedRun : r));
      showToast('Gate rejected and recorded', 'info');
      // Refresh lineage
      const newLin = await api.getLineage(spaceId);
      lineageSignal.value = newLin;
    } catch (err: any) {
      showToast(`Rejection failed: ${err.message}`, 'error');
    }
  };

  const handleDiagnose = (runId: string) => {
    if (!spaceId) return;
    selectedWaterfallTargetSignal.value = {
      spaceId,
      runId,
      triggerId: `btn-right-diag-${runId}`,
      requestGeneration: getNextWaterfallGeneration(),
    };
  };

  const handleCopyDigest = (sha?: string) => {
    if (!sha) return;
    navigator.clipboard.writeText(sha);
    showToast('Copied SHA-256 digest to clipboard!', 'success');
  };

  const handleDownloadFile = async (f: FileRecord) => {
    if (!spaceId) return;
    try {
      showToast(`Downloading ${f.filename}...`, 'info');
      await api.downloadFile(spaceId, f.file_id, f.filename);
      showToast(`Downloaded ${f.filename}`, 'success');
    } catch (err: any) {
      showToast(`Download failed: ${err.message}`, 'error');
    }
  };

  const formatBytes = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <aside class="right-panel" aria-label="Lineage, Files, and Telemetry Inspector">
      <div class="right-panel-header">
        <div class="panel-tabs">
          <button
            class={`panel-tab-btn ${activeTab === 'files' ? 'active' : ''}`}
            onClick={() => (activeRightTabSignal.value = 'files')}
          >
            <span>📁</span> Files ({files.length})
          </button>
          <button
            class={`panel-tab-btn ${activeTab === 'lineage' ? 'active' : ''}`}
            onClick={() => (activeRightTabSignal.value = 'lineage')}
          >
            <span>🌿</span> Lineage
          </button>
          <button
            class={`panel-tab-btn ${activeTab === 'grafana' ? 'active' : ''}`}
            onClick={() => (activeRightTabSignal.value = 'grafana')}
          >
            <span>📊</span> Telemetry
          </button>
          <button
            class={`panel-tab-btn ${activeTab === 'team' ? 'active' : ''}`}
            onClick={() => (activeRightTabSignal.value = 'team')}
          >
            <span>👥</span> Space
          </button>
        </div>
      </div>

      <div class="panel-content">
        {/* Tab 0: Space File Library */}
        {activeTab === 'files' && (
          <div class="files-tab-view">
            <div class="files-tab-header">
              <span class="files-track-info">Track: <strong>#{activeTag}</strong></span>
              <button
                class="btn-text-action"
                onClick={async () => {
                  if (spaceId) {
                    filesStateSignal.value = 'loading';
                    try {
                      const newFiles = await api.listFiles(spaceId, activeTag);
                      filesSignal.value = newFiles;
                      filesStateSignal.value = 'ready';
                    } catch {
                      filesStateSignal.value = 'error';
                    }
                  }
                }}
              >
                ↻ Refresh
              </button>
            </div>

            {filesState === 'error' ? (
              <div class="panel-error-state">
                <div class="empty-icon">⚠️</div>
                <h4>Failed to Load Files</h4>
                <p>Could not retrieve space file library.</p>
              </div>
            ) : files.length === 0 ? (
              <div class="panel-empty-state">
                <div class="empty-icon">📁</div>
                <h4>No Files in File Library</h4>
                <p>Attach screenplays (PDF, FDX), treatments (DOCX, TXT), or budgets (XLSX, CSV) via 📎 or drag & drop.</p>
              </div>
            ) : (
              <div class="files-card-list">
                {files.map((f) => (
                  <div key={f.file_id} class="file-card">
                    <div class="file-card-top">
                      <span class="file-icon">
                        {f.filename.endsWith('.pdf') ? '📕' : f.filename.endsWith('.fdx') ? '🎬' : '📄'}
                      </span>
                      <div class="file-info-col">
                        <div class="file-name" title={f.filename}>{f.filename}</div>
                        <div class="file-meta-row">
                          <span class="file-size-badge">{formatBytes(f.size_bytes)}</span>
                          {f.ingestion_status === 'ready' && (
                            <span class="file-tag-badge" style={{ backgroundColor: 'rgba(34, 197, 94, 0.2)', color: '#4ade80' }} title="Fully indexed document">
                              ✓ Ready ({f.chunk_count || 0} chunks)
                            </span>
                          )}
                          {f.ingestion_status === 'ready_partial' && (
                            <span
                              class="file-tag-badge"
                              style={{ backgroundColor: 'rgba(234, 179, 8, 0.2)', color: '#facc15' }}
                              title={`Partial OCR: Pages ${(f.ocr_gap_pages || []).join(', ')} require OCR`}
                            >
                              ⚠ Partial OCR ({f.chunk_count || 0} chunks)
                            </span>
                          )}
                          {(f.ingestion_status === 'extracting' || f.ingestion_status === 'indexing' || f.ingestion_status === 'pending') && (
                            <span class="file-tag-badge" style={{ backgroundColor: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa' }} title="Processing document...">
                              ⏳ Extracting...
                            </span>
                          )}
                          {f.ingestion_status === 'needs_ocr' && (
                            <span class="file-tag-badge" style={{ backgroundColor: 'rgba(249, 115, 22, 0.2)', color: '#fb923c' }} title="Scanned document requires OCR">
                              🔍 Needs OCR
                            </span>
                          )}
                          {f.ingestion_status === 'failed' && (
                            <span class="file-tag-badge" style={{ backgroundColor: 'rgba(239, 68, 68, 0.2)', color: '#f87171' }} title={f.ingestion_error_code || 'Ingestion failed'}>
                              ✕ Failed
                            </span>
                          )}
                          {f.project_tags && f.project_tags.map((t) => (
                            <span key={t} class="file-tag-badge">#{t}</span>
                          ))}
                          <span class="file-time">{new Date(f.created_at).toLocaleDateString([], { month: 'short', day: 'numeric' })}</span>
                        </div>
                      </div>
                    </div>

                    <div class="file-card-actions">
                      <button
                        class="btn-compact btn-outline"
                        onClick={() => {
                          composerDraftSignal.value = `Discuss asset "${f.filename}": What is this story about and what are the key production considerations?`;
                          composerContextFileIdsSignal.value = [f.file_id];
                          composerIntentSignal.value = 'conversation';
                          showToast(`Selected "${f.filename}" for AI discussion`, 'info');
                        }}
                        title="Discuss this file with AI"
                      >
                        💬 Discuss
                      </button>
                      {canReindex && (
                        <button
                          class="btn-compact btn-secondary"
                          onClick={() => handleReindexFile(f.file_id)}
                          title="Re-extract and index chunks (Owner/Admin/Coordinator only)"
                        >
                          ↻ Reindex
                        </button>
                      )}
                      <button
                        class="btn-compact btn-primary"
                        onClick={() => handleDownloadFile(f)}
                        title="Download binary file"
                      >
                        📥 Download
                      </button>
                      <button
                        class="btn-compact btn-secondary"
                        onClick={() => handleCopyDigest(f.sha256)}
                        title="Copy SHA-256 checksum"
                      >
                        📋 SHA
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {/* Tab 1: Grafana Tempo lineage. Local DAG is parked for the hackathon. */}
        {activeTab === 'lineage' && (
          <div class="lineage-tab-view">
            <GrafanaCloudBoard spaceId={spaceId} mode="lineage" />
            {!HACKATHON_GRAFANA_ONLY && (
            lineageState === 'error' ? (
              <div class="panel-error-state">
                <div class="empty-icon">⚠️</div>
                <h4>Failed to Load Lineage</h4>
                <p>Could not retrieve artifact dependency graph.</p>
                <button
                  class="btn-secondary btn-compact"
                  onClick={async () => {
                    if (spaceId) {
                      lineageStateSignal.value = 'loading';
                      try {
                        const newLin = await api.getLineage(spaceId);
                        lineageSignal.value = newLin;
                        lineageStateSignal.value = 'ready';
                      } catch {
                        lineageStateSignal.value = 'error';
                      }
                    }
                  }}
                >
                  ↻ Retry Lineage
                </button>
              </div>
            ) : !lineage || lineage.nodes.length === 0 ? (
              <div class="panel-empty-state">
                <div class="empty-icon">🌿</div>
                <h4>No Artifact Lineage Yet</h4>
                <p>
                  Upload a screenplay (PDF, FDX, Fountain, text) or run scene breakdowns to dynamically generate the verifiable artifact graph.
                </p>
                {lineage?.grafana ? (
                  <p class="grafana-visual-kicker">
                    Grafana Tempo · {lineage.grafana.connected
                      ? `${lineage.grafana.trace_count || 0} traces`
                      : lineage.grafana.error || 'not connected'}
                    {lineage.grafana.ingest ? ` · ingest ${lineage.grafana.ingest}` : ''}
                    {lineage.grafana.connected && !lineage.grafana.trace_count && lineage.grafana.error
                      ? ` · ${lineage.grafana.error}`
                      : ''}
                  </p>
                ) : null}
              </div>
            ) : (
              <div class="dag-container">
                {lineage.grafana ? (
                  <div class="grafana-visual-kicker">
                    Grafana Tempo · {lineage.grafana.connected
                      ? `${lineage.grafana.trace_count || 0} traces`
                      : lineage.grafana.error || 'not connected'}
                    {lineage.grafana.ingest ? ` · ingest ${lineage.grafana.ingest}` : ''}
                    {lineage.grafana.connected && !lineage.grafana.trace_count && lineage.grafana.error
                      ? ` · ${lineage.grafana.error}`
                      : ''}
                  </div>
                ) : null}
                {(() => {
                  const edges = lineage.edges || [];
                  const nodes = lineage.nodes || [];
                  const nodeMap = new Map<string, LineageNode>();
                  nodes.forEach((n) => nodeMap.set(n.id, n));

                  return nodes.map((node, idx) => {
                    const inEdges = edges.filter((e) => e.to === node.id);
                    const outEdges = edges.filter((e) => e.from === node.id);

                    const isFork = outEdges.length > 1;
                    const isReuse =
                      (node.type === 'source' && outEdges.length > 1) ||
                      (inEdges.length > 0 && outEdges.length > 1);
                    const isMerge = inEdges.length > 1;
                    const isTerminal =
                      outEdges.length === 0 &&
                      (node.type === 'artifact' || node.type === 'manifest');

                    const parents = inEdges
                      .map((e) => ({
                        node: nodeMap.get(e.from),
                        relation: e.relation,
                      }))
                      .filter((p): p is { node: LineageNode; relation: string } => p.node !== undefined);

                    const children = outEdges
                      .map((e) => ({
                        node: nodeMap.get(e.to),
                        relation: e.relation,
                      }))
                      .filter((c): c is { node: LineageNode; relation: string } => c.node !== undefined);

                    const isSelected = selectedNode?.id === node.id;

                    return (
                      <div key={node.id} class="dag-node-wrapper">
                        <button
                          class={`dag-node dag-node-${node.type} dag-status-${node.status} ${
                            isSelected ? 'dag-node-selected' : ''
                          }`}
                          onClick={() => (selectedLineageNodeSignal.value = node)}
                          title={`Inspect ${node.label}`}
                        >
                          <div class="dag-node-header">
                            <div class="dag-badge-cluster">
                              <span class={`dag-node-type-badge badge-${node.type}`}>
                                {node.type.toUpperCase()}
                              </span>
                              {isFork && (
                                <span
                                  class="dag-topology-badge badge-fork"
                                  title={`Branching: Splits into ${outEdges.length} downstream branches`}
                                >
                                  ⑂ Fork ({outEdges.length})
                                </span>
                              )}
                              {isReuse && (
                                <span
                                  class="dag-topology-badge badge-reuse"
                                  title={`Reuse: Referenced across ${outEdges.length} downstream workflows`}
                                >
                                  ♻ Reused ({outEdges.length}x)
                                </span>
                              )}
                              {isMerge && (
                                <span
                                  class="dag-topology-badge badge-merge"
                                  title={`Convergence: Synthesizes data from ${inEdges.length} sources`}
                                >
                                  ⤹ Merged ({inEdges.length})
                                </span>
                              )}
                              {isTerminal && (
                                <span
                                  class="dag-topology-badge badge-terminal"
                                  title="Final verified artifact output"
                                >
                                  ✓ Output
                                </span>
                              )}
                            </div>
                            <span class={`dag-status-badge status-${node.status}`}>{node.status}</span>
                          </div>

                          <div class="dag-node-title">{node.label}</div>

                          {(parents.length > 0 || children.length > 0) && (
                            <div class="dag-node-relations">
                              {parents.length > 0 && (
                                <div class="dag-rel-item">
                                  <span class="dag-rel-direction">↳ From:</span>
                                  <span
                                    class="dag-rel-names"
                                    title={parents.map((p) => p.node.label).join(', ')}
                                  >
                                    {parents.map((p) => p.node.label).join(', ')}
                                  </span>
                                </div>
                              )}
                              {children.length > 0 && (
                                <div class="dag-rel-item">
                                  <span class="dag-rel-direction">↳ Feeds into:</span>
                                  <span
                                    class="dag-rel-names"
                                    title={children
                                      .map((c) => `${c.node.label} (${c.relation})`)
                                      .join(', ')}
                                  >
                                    {children.length} target{children.length > 1 ? 's' : ''} (
                                    {children.map((c) => c.relation).join(', ')})
                                  </span>
                                </div>
                              )}
                            </div>
                          )}

                          {node.sha256 && (
                            <div
                              class="dag-node-sha"
                              style={{
                                fontSize: '11px',
                                color: 'var(--text-muted)',
                                fontFamily: 'monospace',
                              }}
                            >
                              SHA: {node.sha256.substring(0, 8)}...
                              {node.sha256.substring(node.sha256.length - 8)}
                            </div>
                          )}
                        </button>

                        {idx < nodes.length - 1 && (
                          <div class="dag-connector-wrapper">
                            {outEdges.length > 1 && (
                              <div class="dag-fork-rail">
                                ⑂ Branching Point ({outEdges.length} paths)
                              </div>
                            )}
                            {outEdges.length === 1 && (
                              <span class="dag-connector-pill">
                                {outEdges[0].relation === 'analyzed_by' && '🔍 '}
                                {outEdges[0].relation === 'gated_by' && '🛡️ '}
                                {outEdges[0].relation === 'generated_by' && '⚡ '}
                                {outEdges[0].relation}
                              </span>
                            )}
                            {outEdges.length === 0 && (
                              <div class="dag-connector-line" />
                            )}
                          </div>
                        )}
                      </div>
                    );
                  });
                })()}
              </div>
            )
            )}
          </div>
        )}

        {/* Tab 2: Grafana Cloud telemetry. Local run waterfalls are parked for the hackathon. */}
        {activeTab === 'grafana' && (
          <div class="telemetry-tab-view">
            <GrafanaCloudBoard spaceId={spaceId} mode="telemetry" />
            {!HACKATHON_GRAFANA_ONLY && (
            <>
            <h4 class="grafana-runs-heading">StudioTower runs</h4>
            {runsState === 'error' ? (
              <div class="panel-error-state">
                <div class="empty-icon">⚠️</div>
                <h4>Failed to Load Runs</h4>
                <p>Could not retrieve AI execution runs. This list is StudioTower Firestore, not Grafana.</p>
                <button
                  class="btn-secondary btn-compact"
                  onClick={async () => {
                    if (spaceId) {
                      runsStateSignal.value = 'loading';
                      try {
                        const newRuns = await api.listRuns(spaceId);
                        runsSignal.value = newRuns;
                        runsStateSignal.value = 'ready';
                      } catch {
                        runsStateSignal.value = 'error';
                      }
                    }
                  }}
                >
                  ↻ Retry Runs
                </button>
              </div>
            ) : runs.length === 0 ? (
              <div class="panel-empty-state">
                <div class="empty-icon">📋</div>
                <h4>No StudioTower runs yet</h4>
                <p>Run cards and local span waterfalls appear here after an AI task executes. Grafana dashboards are above.</p>
              </div>
            ) : (
              <div class="runs-list">
                {runs.map((r) => (
                  <div
                    key={r.run_id}
                    class={`run-card run-card-${r.status} ${selectedRunId === r.run_id ? 'selected-run-target' : ''}`}
                    data-selected={selectedRunId === r.run_id ? 'true' : undefined}
                    id={`run-card-${r.run_id}`}
                    data-ai-engine={r.telemetry?.ai_engine || 'unknown'}
                  >
                    <div class="run-card-header">
                      <span class="run-id-pill">{r.run_id.substring(0, 12)}</span>
                      {r.telemetry?.ai_engine && (
                        <span class={`ai-engine-badge engine-${r.telemetry.ai_engine}`}>
                          {r.telemetry.ai_engine === 'gemini-generate-content' ? '✨ GEMINI GENAI' : '⚙️ FALLBACK'}
                        </span>
                      )}
                      <span class={`status-pill status-${r.status}`}>{r.status.toUpperCase()}</span>
                    </div>

                    <div class="run-prompt-text">{r.prompt}</div>

                    {/* Approval Gate Card */}
                    {r.approval_gate && (
                      <div class={`gate-subcard gate-${r.approval_gate.status}`}>
                        <div class="gate-header">
                          <span class="gate-icon">🛡️</span>
                          <span class="gate-title">{r.approval_gate.title}</span>
                          <span class={`gate-status-pill status-${r.approval_gate.status}`}>
                            {r.approval_gate.status.toUpperCase()}
                          </span>
                        </div>
                        <p class="gate-desc">{r.approval_gate.description}</p>

                        {r.approval_gate.status === 'pending' && (
                          <div class="gate-actions">
                            {capabilities?.can_approve_runs ? (
                              <>
                                <button
                                  class="btn-primary btn-compact"
                                  onClick={() => handleApproveGate(r.run_id)}
                                >
                                  ✅ Approve Action
                                </button>
                                <button
                                  class="btn-danger btn-compact"
                                  onClick={() => handleRejectGate(r.run_id)}
                                >
                                  ❌ Reject Action
                                </button>
                              </>
                            ) : (
                              <div class="gate-locked-note">
                                🔒 Requires Production Coordinator or Owner permission to approve
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Telemetry Trace & Diagnosis */}
                    <div class="trace-box">
                      {r.trace_id && (
                        <div>
                          <span>Trace ID:</span> <code>{r.trace_id}</code>
                        </div>
                      )}
                      {r.telemetry?.has_real_telemetry ? (
                        <div style={{ display: 'flex', gap: '6px', marginTop: '6px', flexWrap: 'wrap' }}>
                          <button
                            id={`btn-right-diag-${r.run_id}`}
                            class="btn-secondary btn-compact"
                            onClick={() => handleDiagnose(r.run_id)}
                          >
                            🔍 Diagnose & Waterfall
                          </button>
                          {getSafeGrafanaUrl(r.telemetry?.grafana_dashboard_url) && (
                            <a
                              href={getSafeGrafanaUrl(r.telemetry?.grafana_dashboard_url)!}
                              target="_blank"
                              rel="noopener noreferrer"
                              class="btn-outline btn-compact"
                            >
                              📊 Open Grafana
                            </a>
                          )}
                          <button
                            class="btn-outline btn-compact"
                            onClick={() => {
                              composerDraftSignal.value = `Regarding Run #${r.run_id.slice(-6)}, let's review the scene breakdown and risk gates:`;
                              composerContextRunIdSignal.value = r.run_id;
                              composerContextFileIdsSignal.value = [];
                              composerIntentSignal.value = 'conversation';
                              showToast(`Loaded Run #${r.run_id.slice(-6)} context in composer`, 'info');
                            }}
                            title="Discuss this run breakdown with AI"
                          >
                            💬 Discuss Run
                          </button>
                        </div>
                      ) : (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '6px', marginTop: '6px' }}>
                          <span style={{ fontSize: '0.8rem', color: 'var(--text-muted, #888)' }}>
                            ℹ️ No Telemetry Data Recorded
                          </span>
                          <button
                            class="btn-outline btn-compact"
                            onClick={() => {
                              composerDraftSignal.value = `Regarding Run #${r.run_id.slice(-6)}, let's review the scene breakdown and risk gates:`;
                              composerContextRunIdSignal.value = r.run_id;
                              composerContextFileIdsSignal.value = [];
                              composerIntentSignal.value = 'conversation';
                              showToast(`Loaded Run #${r.run_id.slice(-6)} context in composer`, 'info');
                            }}
                            title="Discuss this run breakdown with AI"
                          >
                            💬 Discuss Run
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            </>
            )}
          </div>
        )}

        {/* Tab 3: Space & Team */}
        {activeTab === 'team' && (
          <div class="team-tab-view">
            <div class="team-space-meta">
              <h4 class="team-space-name">{context?.space.name}</h4>
              <div class="meta-item">
                <span>Created By:</span>
                <strong>{context?.space.created_by}</strong>
              </div>
              <div class="meta-item">
                <span>Active Track:</span>
                <strong>#{context?.space.tags?.[0]?.slug || 'general'}</strong>
              </div>
              <div class="meta-item">
                <span>Your Role:</span>
                <span class={`role-badge role-${context?.current_user_role}`}>
                  {context?.current_user_role.toUpperCase()}
                </span>
              </div>
            </div>

            <div class="team-actions-box">
              {capabilities?.can_invite && (
                <button
                  class="btn-primary"
                  onClick={() => (activeModalSignal.value = 'manage_space')}
                  style={{ width: '100%' }}
                >
                  ✉️ Manage Members & Invite
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Node Inspector Drawer */}
      <Drawer
        isOpen={selectedNode !== null}
        onClose={() => (selectedLineageNodeSignal.value = null)}
        title={selectedNode ? `${selectedNode.type.toUpperCase()}: ${selectedNode.label}` : 'Artifact Inspector'}
      >
        {selectedNode && (
          <div class="node-inspector-content">
            <div class="inspector-field">
              <label>Node ID</label>
              <code>{selectedNode.id}</code>
            </div>

            <div class="inspector-field">
              <label>Status</label>
              <span class={`status-pill status-${selectedNode.status}`}>{selectedNode.status}</span>
            </div>

            {selectedNode.size_bytes !== undefined && (
              <div class="inspector-field">
                <label>File Size</label>
                <span>{(selectedNode.size_bytes / 1024).toFixed(1)} KB</span>
              </div>
            )}

            {selectedNode.sha256 && (
              <div class="inspector-field">
                <label>Cryptographic SHA-256 Digest</label>
                <div class="sha-box">
                  <code>{selectedNode.sha256}</code>
                  <button
                    class="btn-secondary btn-compact"
                    onClick={() => handleCopyDigest(selectedNode.sha256)}
                  >
                    📋 Copy SHA
                  </button>
                </div>
              </div>
            )}

            {/* Topology & Dependency Analysis */}
            {(() => {
              const edges = lineage?.edges || [];
              const nodes = lineage?.nodes || [];
              const nodeMap = new Map<string, LineageNode>();
              nodes.forEach((n) => nodeMap.set(n.id, n));

              const inEdges = edges.filter((e) => e.to === selectedNode.id);
              const outEdges = edges.filter((e) => e.from === selectedNode.id);
              const isFork = outEdges.length > 1;
              const isReuse =
                (selectedNode.type === 'source' && outEdges.length > 1) ||
                (inEdges.length > 0 && outEdges.length > 1);
              const isMerge = inEdges.length > 1;

              const parents = inEdges
                .map((e) => ({
                  node: nodeMap.get(e.from),
                  relation: e.relation,
                }))
                .filter((p): p is { node: LineageNode; relation: string } => p.node !== undefined);

              const children = outEdges
                .map((e) => ({
                  node: nodeMap.get(e.to),
                  relation: e.relation,
                }))
                .filter((c): c is { node: LineageNode; relation: string } => c.node !== undefined);

              return (
                <div class="inspector-field" style={{ marginTop: '12px' }}>
                  <label>Topology & Lineage Pattern</label>
                  <div class="inspector-topology-tags">
                    {isFork && (
                      <span class="dag-topology-badge badge-fork">
                        ⑂ Fork: Splits into {outEdges.length} downstream branches
                      </span>
                    )}
                    {isReuse && (
                      <span class="dag-topology-badge badge-reuse">
                        ♻ Reused across {outEdges.length} execution pipelines
                      </span>
                    )}
                    {isMerge && (
                      <span class="dag-topology-badge badge-merge">
                        ⤹ Converged: Merges {inEdges.length} upstream sources
                      </span>
                    )}
                    {outEdges.length === 0 && (selectedNode.type === 'artifact' || selectedNode.type === 'manifest') && (
                      <span class="dag-topology-badge badge-terminal">
                        ✓ Terminal Verified Output
                      </span>
                    )}
                    {!isFork && !isReuse && !isMerge && (
                      <span class="dag-topology-badge badge-linear">
                        → Linear Pipeline Step
                      </span>
                    )}
                  </div>

                  {parents.length > 0 && (
                    <div style={{ marginTop: '10px' }}>
                      <label style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                        Upstream Sources ({parents.length})
                      </label>
                      <div class="inspector-rel-list">
                        {parents.map((p) => (
                          <div key={p.node.id} class="inspector-rel-row">
                            <span class={`dag-node-type-badge badge-${p.node.type}`}>
                              {p.node.type.toUpperCase()}
                            </span>
                            <span class="inspector-rel-name" title={p.node.label}>{p.node.label}</span>
                            <span class="dag-connector-pill">{p.relation}</span>
                            <button
                              class="btn-secondary btn-compact"
                              onClick={() => (selectedLineageNodeSignal.value = p.node)}
                              title="Navigate to parent"
                            >
                              View
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {children.length > 0 && (
                    <div style={{ marginTop: '10px' }}>
                      <label style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                        Downstream Consumers & Artifacts ({children.length})
                      </label>
                      <div class="inspector-rel-list">
                        {children.map((c) => (
                          <div key={c.node.id} class="inspector-rel-row">
                            <span class={`dag-node-type-badge badge-${c.node.type}`}>
                              {c.node.type.toUpperCase()}
                            </span>
                            <span class="inspector-rel-name" title={c.node.label}>{c.node.label}</span>
                            <span class="dag-connector-pill">{c.relation}</span>
                            <button
                              class="btn-secondary btn-compact"
                              onClick={() => (selectedLineageNodeSignal.value = c.node)}
                              title="Navigate to child"
                            >
                              View
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              );
            })()}

            {selectedNode.type === 'source' || selectedNode.type === 'artifact' ? (
              <div class="inspector-actions" style={{ marginTop: '20px' }}>
                <button
                  class="btn-primary"
                  onClick={async () => {
                    try {
                      showToast(`Downloading ${selectedNode.label}...`, 'info');
                      await api.downloadFile(spaceId!, selectedNode.id, selectedNode.label);
                      showToast(`Downloaded ${selectedNode.label}`, 'success');
                    } catch (err: any) {
                      showToast(`Download failed: ${err.message}`, 'error');
                    }
                  }}
                  style={{ width: '100%' }}
                >
                  ⬇️ Download Raw File (Authenticated)
                </button>
              </div>
            ) : null}
          </div>
        )}
      </Drawer>
    </aside>
  );
}
