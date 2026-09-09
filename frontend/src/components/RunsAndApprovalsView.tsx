import { JSX } from 'preact';
import { useState, useMemo } from 'preact/hooks';
import {
  runsSignal,
  spaceContextSignal,
  activeTagSignal,
  activeSpaceIdSignal,
  rightPanelOpenSignal,
  activeRightTabSignal,
  selectedRunIdSignal,
  selectedWaterfallTargetSignal,
  getNextWaterfallGeneration,
} from '../services/store';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { MetricsSummaryBar } from './MetricsSummaryBar';
import { HACKATHON_GRAFANA_ONLY } from '../hackathon';

export function RunsAndApprovalsView(): JSX.Element {
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [processingRunId, setProcessingRunId] = useState<string | null>(null);

  const runs = runsSignal.value;
  const context = spaceContextSignal.value;
  const activeTag = activeTagSignal.value;
  const spaceId = context?.space.space_id || '';
  const canApprove = Boolean(context?.capabilities?.can_approve_runs);

  // Pending approval runs
  const pendingApprovals = useMemo(() => {
    return runs.filter(
      (r) => r.status === 'awaiting_approval' || (r.approval_gate && r.approval_gate.status === 'pending')
    );
  }, [runs]);

  // Filtered runs history
  const filteredRuns = useMemo(() => {
    return runs.filter((r) => {
      // 1. Tag filter
      if (activeTag && activeTag !== 'all') {
        if (r.project_tag !== activeTag) return false;
      }

      // 2. Status filter
      if (statusFilter !== 'all') {
        if (statusFilter === 'awaiting_approval' && r.status !== 'awaiting_approval') return false;
        if (statusFilter === 'running' && r.status !== 'running') return false;
        if (statusFilter === 'completed' && r.status !== 'completed') return false;
        if (statusFilter === 'failed' && r.status !== 'failed') return false;
      }

      // 3. Search query
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchPrompt = (r.prompt || '').toLowerCase().includes(q);
        const matchId = r.run_id.toLowerCase().includes(q);
        const matchGate = r.approval_gate?.title?.toLowerCase().includes(q) || false;
        if (!matchPrompt && !matchId && !matchGate) return false;
      }

      return true;
    });
  }, [runs, activeTag, statusFilter, searchQuery]);

  const handleApprove = async (runId: string, approved: boolean) => {
    if (!spaceId) return;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    setProcessingRunId(runId);
    try {
      showToast(approved ? 'Approving task execution...' : 'Rejecting task...', 'info');
      await api.approveRun(reqSpaceId, runId, approved);
      showToast(approved ? 'Task approved and executing' : 'Task rejected', 'success');
      if (activeSpaceIdSignal.value === reqSpaceId && activeTagSignal.value === reqTag) {
        const freshRuns = await api.listRuns(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (activeSpaceIdSignal.value === reqSpaceId && activeTagSignal.value === reqTag) {
          runsSignal.value = freshRuns;
        }
      }
    } catch (err: any) {
      showToast(err.message || 'Approval action failed', 'error');
    } finally {
      setProcessingRunId(null);
    }
  };

  const handleRetry = async (runId: string) => {
    if (!spaceId) return;
    const reqSpaceId = spaceId;
    const reqTag = activeTag;
    setProcessingRunId(runId);
    try {
      showToast('Retrying task...', 'info');
      await api.retryRun(reqSpaceId, runId);
      showToast('Task retry dispatched', 'success');
      if (activeSpaceIdSignal.value === reqSpaceId && activeTagSignal.value === reqTag) {
        const freshRuns = await api.listRuns(reqSpaceId, reqTag !== 'all' ? reqTag : undefined);
        if (activeSpaceIdSignal.value === reqSpaceId && activeTagSignal.value === reqTag) {
          runsSignal.value = freshRuns;
        }
      }
    } catch (err: any) {
      showToast(err.message || 'Retry failed', 'error');
    } finally {
      setProcessingRunId(null);
    }
  };

  const handleInspectTelemetry = (runId: string) => {
    selectedRunIdSignal.value = runId;
    activeRightTabSignal.value = 'grafana';
    rightPanelOpenSignal.value = true;
    if (HACKATHON_GRAFANA_ONLY) return;
    selectedWaterfallTargetSignal.value = {
      spaceId,
      runId,
      triggerId: `btn-telemetry-${runId}`,
      requestGeneration: getNextWaterfallGeneration(),
    };
  };

  const renderStatusBadge = (status: string) => {
    switch (status) {
      case 'completed':
        return <span class="run-status-badge badge-success">✓ Completed</span>;
      case 'running':
        return <span class="run-status-badge badge-running"><span class="spinner-inline" /> Running</span>;
      case 'awaiting_approval':
        return <span class="run-status-badge badge-pending-approval">⏳ Awaiting Approval</span>;
      case 'queued':
        return <span class="run-status-badge badge-pending">📋 Queued</span>;
      case 'failed':
        return <span class="run-status-badge badge-failed">✕ Failed</span>;
      case 'aborted':
        return <span class="run-status-badge badge-failed">⏹ Aborted</span>;
      default:
        return <span class="run-status-badge">{status}</span>;
    }
  };

  return (
    <div class="runs-view-container">
      {/* Header */}
      <div class="runs-header">
        <div class="runs-title-group">
          <h1 class="runs-title">Tasks & Approvals Center</h1>
          <span class="runs-count-pill">{runs.length} {runs.length === 1 ? 'Task' : 'Tasks'}</span>
        </div>
      </div>

      {/* Space Telemetry & SLO Metrics Summary Bar */}
      {HACKATHON_GRAFANA_ONLY ? null : <MetricsSummaryBar spaceId={spaceId} activeTag={activeTag} />}

      {/* Pending Approvals Section */}
      {pendingApprovals.length > 0 && (
        <div class="pending-approvals-card">
          <div class="pending-approvals-header">
            <h2 class="pending-section-title">
              ⚠️ Pending Approvals Queue ({pendingApprovals.length})
            </h2>
            <span class="pending-hint-text">
              {canApprove ? 'You have space approval authority. Review task parameters and approve or reject.' : 'Requires space authority (Owner/Admin/Coordinator).'}
            </span>
          </div>
          <div class="pending-list">
            {pendingApprovals.map((run) => (
              <div key={run.run_id} class="pending-run-item">
                <div class="pending-run-info">
                  <div class="pending-run-top">
                    <span class="pending-agent-tag">
                      {run.approval_gate ? `🛡️ ${run.approval_gate.title}` : '🛡️ Pending Workflow'}
                    </span>
                    <span class="pending-run-id">ID: {run.run_id}</span>
                  </div>
                  <p class="pending-prompt">{run.prompt || 'Workflow task execution'}</p>
                  {run.approval_gate?.description && (
                    <p style="margin: 4px 0 0; font-size: 12px; color: var(--text-secondary);">
                      {run.approval_gate.description}
                    </p>
                  )}
                </div>
                {canApprove && (
                  <div class="pending-run-actions">
                    <button
                      type="button"
                      class="btn-primary btn-approve"
                      disabled={processingRunId === run.run_id}
                      onClick={() => handleApprove(run.run_id, true)}
                    >
                      ✓ Approve
                    </button>
                    <button
                      type="button"
                      class="btn-secondary btn-reject"
                      disabled={processingRunId === run.run_id}
                      onClick={() => handleApprove(run.run_id, false)}
                    >
                      ✕ Reject
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Controls Bar: Search & Status Filter */}
      <div class="runs-controls-bar">
        <div class="runs-search-box">
          <input
            type="text"
            class="runs-search-input"
            placeholder="Search prompt or Run ID..."
            value={searchQuery}
            onInput={(e) => setSearchQuery((e.target as HTMLInputElement).value)}
          />
        </div>
        <div class="runs-filter-group">
          <label htmlFor="run-status-select">Status:</label>
          <select
            id="run-status-select"
            class="runs-select-control"
            value={statusFilter}
            onChange={(e) => setStatusFilter((e.target as HTMLSelectElement).value)}
          >
            <option value="all">All Statuses</option>
            <option value="awaiting_approval">Awaiting Approval</option>
            <option value="running">Running</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
          </select>
        </div>
      </div>

      {/* Runs History Table */}
      <div class="runs-table-card">
        {filteredRuns.length === 0 ? (
          <div class="runs-empty-state">
            <p class="empty-title">No Task Execution Records Yet</p>
            <p class="empty-subtitle">When invoking agents or running script breakdown workflows in chat, progress and telemetry will appear here.</p>
          </div>
        ) : (
          <table class="runs-table">
            <thead>
              <tr>
                <th>Task / Prompt</th>
                <th>Tag</th>
                <th>Status</th>
                <th>Gate</th>
                <th>Created By</th>
                <th style="text-align: right;">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredRuns.map((run) => {
                const isRetryable = canApprove && run.status === 'failed' && run.is_retryable === true;
                return (
                  <tr key={run.run_id}>
                    <td>
                      <div class="run-prompt-cell">
                        <span class="run-prompt-text" title={run.prompt}>
                          {run.prompt || `Task ${run.run_id}`}
                        </span>
                        <span class="run-id-subtext">{run.run_id}</span>
                      </div>
                    </td>
                    <td>
                      <span class="table-tag-chip">{run.project_tag || 'general'}</span>
                    </td>
                    <td>{renderStatusBadge(run.status)}</td>
                    <td>
                      {run.approval_gate ? (
                        <span style="font-size: 11px; color: var(--text-secondary);">
                          {run.approval_gate.title} ({run.approval_gate.status})
                        </span>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td>{run.created_by ? `${run.created_by}` : '-'}</td>
                    <td>
                      <div class="run-row-actions">
                        <button
                          type="button"
                          class="action-btn"
                          id={`btn-telemetry-${run.run_id}`}
                          onClick={() => handleInspectTelemetry(run.run_id)}
                          title="Inspect Span Waterfall trace & AI diagnosis"
                        >
                          📊 Telemetry
                        </button>
                        {isRetryable && (
                          <button
                            type="button"
                            class="action-btn action-retry-btn"
                            disabled={processingRunId === run.run_id}
                            onClick={() => handleRetry(run.run_id)}
                          >
                            Retry
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
    </div>
  );
}
