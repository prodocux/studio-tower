import { JSX } from 'preact';
import { useState, useEffect, useRef, useMemo } from 'preact/hooks';
import {
  selectedWaterfallTargetSignal,
  runsSignal,
  spaceContextSignal,
  activeSpaceIdSignal,
  activeTagSignal,
} from '../services/store';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { promptConfirm } from './primitives/ConfirmDialog';
import { ResponsiveDrawer } from './primitives/ResponsiveDrawer';
import {
  RunTraceResponse,
  SpanWaterfallNode,
  DiagnosisRecord,
  TelemetryVerificationResult,
} from '../types';
import { getSafeGrafanaUrl } from '../utils';

interface FlattenedSpan {
  node: SpanWaterfallNode;
  depth: number;
}

export function SpanWaterfallDrawer(): JSX.Element | null {
  const target = selectedWaterfallTargetSignal.value;
  const context = spaceContextSignal.value;
  const runs = runsSignal.value;

  const [activeTab, setActiveTab] = useState<'waterfall' | 'diagnosis'>('waterfall');
  const [traceData, setTraceData] = useState<RunTraceResponse | null>(null);
  const [traceLoading, setTraceLoading] = useState<boolean>(false);
  const [traceError, setTraceError] = useState<string | null>(null);
  const [traceStatusCode, setTraceStatusCode] = useState<number | null>(null);

  const [diagnosisData, setDiagnosisData] = useState<DiagnosisRecord | null>(null);
  const [diagLoading, setDiagLoading] = useState<boolean>(false);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [countdownSec, setCountdownSec] = useState<number>(0);

  const [verifyLoading, setVerifyLoading] = useState<boolean>(false);
  const [verifyResult, setVerifyResult] = useState<TelemetryVerificationResult | null>(null);

  const [expandedSpanKeys, setExpandedSpanKeys] = useState<Set<string>>(new Set());
  const [retrying, setRetrying] = useState<boolean>(false);

  // Request & concurrency fencing
  const activeGenRef = useRef<number>(0);
  const traceAcRef = useRef<AbortController | null>(null);
  const diagAcRef = useRef<AbortController | null>(null);
  const verifyAcRef = useRef<AbortController | null>(null);
  const retryAcRef = useRef<AbortController | null>(null);
  const countdownTimerRef = useRef<any>(null);

  // Find targeted run from store
  const targetRun = useMemo(() => {
    if (!target) return null;
    return runs.find((r) => r.run_id === target.runId && r.space_id === target.spaceId) || null;
  }, [runs, target?.runId, target?.spaceId]);

  const canRetry = Boolean(
    context?.capabilities?.can_approve_runs &&
      targetRun &&
      targetRun.status === 'failed' &&
      targetRun.is_retryable === true
  );

  const handleClose = () => {
    selectedWaterfallTargetSignal.value = null;
  };

  // Load Trace
  const fetchTrace = (spaceId: string, runId: string, gen: number) => {
    traceAcRef.current?.abort();
    const ac = new AbortController();
    traceAcRef.current = ac;

    setTraceLoading(true);
    setTraceError(null);
    setTraceStatusCode(null);

    api
      .getRunTrace(spaceId, runId, ac.signal)
      .then((res) => {
        if (activeGenRef.current === gen && !ac.signal.aborted) {
          setTraceData(res);
          setTraceLoading(false);
        }
      })
      .catch((err: any) => {
        if (activeGenRef.current !== gen || ac.signal.aborted) return;
        setTraceLoading(false);
        setTraceStatusCode(err.status || 500);
        setTraceError(err.message || 'Failed to load telemetry trace data');
      });
  };

  // Target Change Effect
  useEffect(() => {
    if (!target) {
      activeGenRef.current++;
      traceAcRef.current?.abort();
      diagAcRef.current?.abort();
      verifyAcRef.current?.abort();
      retryAcRef.current?.abort();
      if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
      setTraceData(null);
      setDiagnosisData(null);
      setTraceLoading(false);
      setTraceError(null);
      setTraceStatusCode(null);
      setDiagLoading(false);
      setDiagError(null);
      setCountdownSec(0);
      setVerifyLoading(false);
      setVerifyResult(null);
      setExpandedSpanKeys(new Set());
      setActiveTab('waterfall');
      setRetrying(false);
      return;
    }

    const gen = ++activeGenRef.current;
    setRetrying(false);
    fetchTrace(target.spaceId, target.runId, gen);

    return () => {
      traceAcRef.current?.abort();
      diagAcRef.current?.abort();
      verifyAcRef.current?.abort();
      retryAcRef.current?.abort();
      if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
    };
  }, [target?.spaceId, target?.runId, target?.requestGeneration]);

  // Handle Diagnosis Trigger
  const handleRunDiagnosis = async () => {
    if (!target || diagLoading || countdownSec > 0) return;
    const gen = activeGenRef.current;
    diagAcRef.current?.abort();
    const ac = new AbortController();
    diagAcRef.current = ac;

    setDiagLoading(true);
    setDiagError(null);

    try {
      const diag = await api.diagnoseRun(target.spaceId, target.runId, ac.signal);
      if (activeGenRef.current === gen && !ac.signal.aborted) {
        setDiagnosisData(diag);
        setDiagLoading(false);
        showToast('Telemetry root-cause diagnosis generated', 'success');
      }
    } catch (err: any) {
      if (activeGenRef.current !== gen || ac.signal.aborted) return;
      setDiagLoading(false);
      const retryAfter = err.retryAfter;
      if (retryAfter && typeof retryAfter === 'number' && retryAfter > 0) {
        setCountdownSec(retryAfter);
        if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
        countdownTimerRef.current = setInterval(() => {
          setCountdownSec((prev) => {
            if (prev <= 1) {
              clearInterval(countdownTimerRef.current);
              countdownTimerRef.current = null;
              return 0;
            }
            return prev - 1;
          });
        }, 1000);
        setDiagError(`Diagnosis rate limit reached. Please wait ${retryAfter}s before retrying.`);
      } else {
        setDiagError(err.message || 'Diagnosis analysis failed');
      }
    }
  };

  // Handle Telemetry Verification
  const handleVerifyTelemetry = async (force: boolean = false) => {
    if (!target || verifyLoading) return;
    const gen = activeGenRef.current;
    verifyAcRef.current?.abort();
    const ac = new AbortController();
    verifyAcRef.current = ac;

    setVerifyLoading(true);
    try {
      const res = await api.verifyRunTelemetry(target.spaceId, target.runId, force, ac.signal);
      if (activeGenRef.current === gen && !ac.signal.aborted) {
        setVerifyResult(res);
        setVerifyLoading(false);
        if (res.throttled) {
          showToast('Telemetry status check throttled. Please retry shortly.', 'warning');
        } else if (res.current_status === 'available') {
          showToast('Telemetry trace ready! Loading Waterfall...', 'success');
          fetchTrace(target.spaceId, target.runId, gen);
        } else {
          showToast(`Telemetry status: ${res.current_status}`, 'info');
        }
      }
    } catch (err: any) {
      if (activeGenRef.current !== gen || ac.signal.aborted) return;
      setVerifyLoading(false);
      showToast(err.message || 'Telemetry verification failed', 'error');
    }
  };

  // Handle Independent Run Retry
  const handleRetryRun = async () => {
    if (!target || !canRetry || retrying) return;
    const requestedTarget = target;
    const requestedGen = activeGenRef.current;

    const { confirmed } = await promptConfirm({
      title: 'Confirm Task Retry',
      message: `Are you sure you want to retry run #${requestedTarget.runId}? This will re-dispatch the workflow in the background.`,
      confirmLabel: 'Retry Task',
      cancelLabel: 'Cancel',
    });

    if (
      !confirmed ||
      activeGenRef.current !== requestedGen ||
      selectedWaterfallTargetSignal.value?.spaceId !== requestedTarget.spaceId ||
      selectedWaterfallTargetSignal.value?.runId !== requestedTarget.runId
    ) {
      return;
    }

    retryAcRef.current?.abort();
    const ac = new AbortController();
    retryAcRef.current = ac;

    setRetrying(true);
    try {
      showToast('Dispatching task retry request...', 'info');
      await api.retryRun(requestedTarget.spaceId, requestedTarget.runId, ac.signal);
      if (
        activeGenRef.current === requestedGen &&
        !ac.signal.aborted &&
        selectedWaterfallTargetSignal.value?.runId === requestedTarget.runId
      ) {
        showToast('Task retry successfully dispatched', 'success');
        // Refresh list runs
        const currentSpace = activeSpaceIdSignal.value;
        const currentTag = activeTagSignal.value;
        if (currentSpace) {
          const freshRuns = await api.listRuns(currentSpace, currentTag !== 'all' ? currentTag : undefined);
          if (activeGenRef.current === requestedGen && !ac.signal.aborted) {
            runsSignal.value = freshRuns;
          }
        }
      }
    } catch (err: any) {
      if (activeGenRef.current === requestedGen && !ac.signal.aborted) {
        showToast(err.message || 'Retry failed', 'error');
      }
    } finally {
      if (activeGenRef.current === requestedGen && !ac.signal.aborted) {
        setRetrying(false);
      }
    }
  };

  const toggleSpanExpand = (spanId: string) => {
    const traceId = traceData?.trace_id || target?.runId || 'trace';
    const key = `${traceId}:${spanId}`;
    setExpandedSpanKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  // Hierarchical tree flattener with cycle / orphan / duplicate / depth protection
  const flattenedSpans: FlattenedSpan[] = useMemo(() => {
    if (!traceData || !traceData.spans || traceData.spans.length === 0) return [];
    const spanMap = new Map<string, SpanWaterfallNode>();
    const childrenMap = new Map<string, string[]>();
    const deduplicatedSpans: SpanWaterfallNode[] = [];

    traceData.spans.forEach((s) => {
      if (!spanMap.has(s.span_id)) {
        spanMap.set(s.span_id, s);
        deduplicatedSpans.push(s);
      }
    });

    deduplicatedSpans.forEach((s) => {
      const parentId = s.parent_span_id;
      if (parentId && spanMap.has(parentId) && parentId !== s.span_id) {
        const list = childrenMap.get(parentId) || [];
        list.push(s.span_id);
        childrenMap.set(parentId, list);
      }
    });

    const roots: SpanWaterfallNode[] = [];
    deduplicatedSpans.forEach((s) => {
      if (!s.parent_span_id || !spanMap.has(s.parent_span_id) || s.parent_span_id === s.span_id) {
        roots.push(s);
      }
    });

    // Sort roots by offset_ms
    roots.sort((a, b) => (a.offset_ms || 0) - (b.offset_ms || 0));

    const result: FlattenedSpan[] = [];
    const visited = new Set<string>();

    const traverse = (node: SpanWaterfallNode, depth: number) => {
      if (visited.has(node.span_id) || depth > 10) return;
      visited.add(node.span_id);
      result.push({ node, depth });

      const childrenIds = childrenMap.get(node.span_id) || [];
      const children = childrenIds
        .map((id) => spanMap.get(id))
        .filter((c): c is SpanWaterfallNode => Boolean(c));
      children.sort((a, b) => (a.offset_ms || 0) - (b.offset_ms || 0));

      children.forEach((c) => traverse(c, depth + 1));
    };

    roots.forEach((r) => traverse(r, 0));

    // Catch any disconnected orphans not visited
    deduplicatedSpans.forEach((s) => {
      if (!visited.has(s.span_id)) {
        traverse(s, 0);
      }
    });

    return result;
  }, [traceData]);

  // Compute Total Duration with safe clamped bounds
  const totalDuration = useMemo(() => {
    if (!traceData?.spans || traceData.spans.length === 0) return 1;
    const maxEnd = Math.max(
      1,
      ...traceData.spans.map((s) => {
        const off = Number.isFinite(s.offset_ms) ? Math.max(0, s.offset_ms) : 0;
        const dur = Number.isFinite(s.duration_ms) ? Math.max(0, s.duration_ms) : 0;
        return off + dur;
      })
    );
    return maxEnd;
  }, [traceData]);

  if (!target) return null;

  const telemetryStatus = targetRun?.telemetry_status || targetRun?.telemetry?.telemetry_status || 'unknown';
  const isSyncing = telemetryStatus === 'exporting' || telemetryStatus === 'delayed';
  const isUnavailableStatus = telemetryStatus === 'unavailable' || telemetryStatus === 'not_instrumented';

  const grafanaUrl =
    getSafeGrafanaUrl(traceData?.grafana_dashboard_url) ||
    getSafeGrafanaUrl(targetRun?.telemetry?.grafana_dashboard_url);

  const renderStatusBadge = (status: string) => {
    switch (status) {
      case 'ok':
        return <span class="span-status-pill status-ok">OK</span>;
      case 'error':
        return <span class="span-status-pill status-error">ERROR</span>;
      case 'warning':
        return <span class="span-status-pill status-warn">WARN</span>;
      default:
        return <span class="span-status-pill">{status}</span>;
    }
  };

  const renderEngineBadge = (diag: DiagnosisRecord) => {
    if (diag.model_id) {
      return <span class="ai-engine-badge engine-gemini">✨ Gemini · {diag.model_id}</span>;
    }
    if (diag.engine && diag.engine.startsWith('gemini')) {
      return <span class="ai-engine-badge engine-gemini">✨ Gemini</span>;
    }
    return <span class="ai-engine-badge engine-rule-based">⚙️ Rule-Based Engine</span>;
  };

  return (
    <ResponsiveDrawer
      isOpen={target !== null}
      onClose={handleClose}
      title="Span Waterfall & Observability"
      isOverlay={true}
      drawerId="span-waterfall-drawer"
      triggerId={target.triggerId}
      width="680px"
      overlayClassName="waterfall-drawer-overlay"
    >
      <div class="waterfall-drawer-container">
        {/* Header Metadata Bar */}
        <div class="waterfall-meta-header">
          <div class="meta-header-top">
            <span class="run-id-tag">Run #{target.runId.substring(0, 12)}</span>
            {traceData?.trace_id && (
              <span class="trace-id-tag" title={traceData.trace_id}>
                Trace: <code>{traceData.trace_id.substring(0, 16)}...</code>
              </span>
            )}
            <span class={`telemetry-status-pill status-${telemetryStatus}`}>
              {telemetryStatus.toUpperCase()}
            </span>
            {targetRun?.telemetry?.has_real_telemetry && (
              <span class="real-telemetry-pill">✨ Live OTel</span>
            )}
          </div>
          {targetRun?.prompt && (
            <p class="waterfall-prompt-subtext" title={targetRun.prompt}>
              {targetRun.prompt}
            </p>
          )}
        </div>

        {/* Global Action Bar: Grafana Link & Retry */}
        <div class="waterfall-actions-toolbar">
          <div class="left-actions">
            {grafanaUrl ? (
              <a
                href={grafanaUrl}
                target="_blank"
                rel="noopener noreferrer"
                class="btn-action btn-grafana-link"
                title="View end-to-end distributed trace in Grafana (HTTPS)"
              >
                📊 Open in Grafana
              </a>
            ) : (
              <span class="grafana-disabled-hint">
                {targetRun?.telemetry?.has_real_telemetry
                  ? '🔒 Grafana link generating or unconfigured'
                  : 'ℹ️ No live OTel telemetry; Grafana link unavailable'}
              </span>
            )}
          </div>
          <div class="right-actions">
            {canRetry && (
              <button
                type="button"
                class="btn-action btn-retry-run"
                disabled={retrying}
                onClick={handleRetryRun}
              >
                {retrying ? 'Dispatching retry...' : '🔄 Retry Run'}
              </button>
            )}
          </div>
        </div>

        {/* Tab Navigation */}
        <div class="waterfall-tabs-nav">
          <button
            type="button"
            class={`waterfall-tab-btn ${activeTab === 'waterfall' ? 'active' : ''}`}
            onClick={() => setActiveTab('waterfall')}
          >
            ⚡ Span Waterfall {traceData ? `(${traceData.total_spans})` : ''}
          </button>
          <button
            type="button"
            class={`waterfall-tab-btn ${activeTab === 'diagnosis' ? 'active' : ''}`}
            onClick={() => setActiveTab('diagnosis')}
          >
            🔍 AI Diagnosis {diagnosisData ? '✓' : ''}
          </button>
        </div>

        {/* TAB 1: Waterfall View */}
        {activeTab === 'waterfall' && (
          <div class="waterfall-tab-content">
            {traceLoading ? (
              <div class="waterfall-loading-box">
                <span class="spinner-inline" />
                <p>Loading Span Waterfall trace structure...</p>
              </div>
            ) : traceError ? (
              <div class="waterfall-state-card error-card-waterfall">
                {isSyncing ? (
                  <>
                    <span class="state-icon">⏳</span>
                    <h4>Telemetry data synchronizing asynchronously</h4>
                    <p>
                      OpenTelemetry collector is exporting and syncing spans for this run (current status: <code>{telemetryStatus}</code>).
                    </p>
                    <div class="verify-action-group">
                      <button
                        type="button"
                        class="btn-secondary btn-compact"
                        disabled={verifyLoading}
                        onClick={() => handleVerifyTelemetry(false)}
                      >
                        {verifyLoading ? 'Checking...' : '↻ Check Telemetry Status'}
                      </button>
                      <button
                        type="button"
                        class="btn-outline btn-compact"
                        disabled={verifyLoading}
                        onClick={() => handleVerifyTelemetry(true)}
                        title="Bypass rate limit window and force verify"
                      >
                        Force Verify
                      </button>
                    </div>
                  </>
                ) : isUnavailableStatus || traceStatusCode === 404 ? (
                  <>
                    <span class="state-icon">ℹ️</span>
                    <h4>No telemetry trace recorded for this run</h4>
                    <p>This run did not enable distributed tracing or data has been purged.</p>
                    <button
                      type="button"
                      class="btn-secondary btn-compact"
                      disabled={verifyLoading}
                      onClick={() => handleVerifyTelemetry(false)}
                    >
                      {verifyLoading ? 'Checking...' : '↻ Check Again'}
                    </button>
                  </>
                ) : (
                  <>
                    <span class="state-icon">⚠️</span>
                    <h4>Error loading telemetry trace</h4>
                    <p>{traceError}</p>
                    <button
                      type="button"
                      class="btn-secondary btn-compact"
                      onClick={() => fetchTrace(target.spaceId, target.runId, activeGenRef.current)}
                    >
                      ↻ Retry
                    </button>
                  </>
                )}
              </div>
            ) : flattenedSpans.length === 0 ? (
              <div class="waterfall-state-card empty-card-waterfall">
                <span class="state-icon">📊</span>
                <h4>No Span Records</h4>
                <p>No trace spans were returned for this run.</p>
              </div>
            ) : (
              <div class="waterfall-visualizer">
                {/* Timeline Axis Header */}
                <div class="timeline-axis-header">
                  <div class="axis-name-col">Span Hierarchy</div>
                  <div class="axis-ticks-col">
                    <span class="tick-label">0ms</span>
                    <span class="tick-label">{Math.round(totalDuration * 0.25)}ms</span>
                    <span class="tick-label">{Math.round(totalDuration * 0.5)}ms</span>
                    <span class="tick-label">{Math.round(totalDuration * 0.75)}ms</span>
                    <span class="tick-label">{totalDuration}ms</span>
                  </div>
                </div>

                {/* Spans List */}
                <div class="waterfall-spans-list">
                  {flattenedSpans.map(({ node, depth }) => {
                    const safeOffset = Math.max(0, node.offset_ms || 0);
                    const safeDuration = Math.max(0, node.duration_ms || 0);
                    const leftPercent = Math.min(100, Math.max(0, (safeOffset / totalDuration) * 100));
                    const widthPercent = Math.min(
                      100 - leftPercent,
                      Math.max(2, (safeDuration / totalDuration) * 100)
                    );
                    const traceId = traceData?.trace_id || target.runId;
                    const spanKey = `${traceId}:${node.span_id}`;
                    const isExpanded = expandedSpanKeys.has(spanKey);

                    return (
                      <div key={node.span_id} class="waterfall-span-row">
                        <div
                          class={`span-row-main ${isExpanded ? 'expanded' : ''}`}
                          onClick={() => toggleSpanExpand(node.span_id)}
                          role="button"
                          tabIndex={0}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              toggleSpanExpand(node.span_id);
                            }
                          }}
                        >
                          <div
                            class="span-info-col"
                            style={{ paddingLeft: `${Math.min(10, depth) * 16 + 8}px` }}
                          >
                            <span class="span-expand-icon">{isExpanded ? '▼' : '▶'}</span>
                            {renderStatusBadge(node.status)}
                            <span class="span-name-label" title={node.name}>
                              {node.name}
                            </span>
                            <span class="span-duration-text">{safeDuration}ms</span>
                          </div>

                          <div class="span-timeline-track">
                            <div
                              class={`span-bar bar-${node.status}`}
                              style={{
                                left: `${leftPercent}%`,
                                width: `${widthPercent}%`,
                              }}
                              title={`${node.name}: offset ${safeOffset}ms, duration ${safeDuration}ms`}
                            />
                          </div>
                        </div>

                        {/* Expanded Span Inspector */}
                        {isExpanded && (
                          <div class="span-details-card">
                            <div class="details-field">
                              <span class="field-label">Span ID:</span>
                              <code>{node.span_id}</code>
                            </div>
                            {node.parent_span_id && (
                              <div class="details-field">
                                <span class="field-label">Parent ID:</span>
                                <code>{node.parent_span_id}</code>
                              </div>
                            )}
                            <div class="details-field">
                              <span class="field-label">Service:</span>
                              <span>{node.service_name}</span>
                            </div>
                            <div class="details-field">
                              <span class="field-label">Time (Start ~ End):</span>
                              <span>
                                {node.start_time_iso} ~ {node.end_time_iso}
                              </span>
                            </div>
                            {node.error_code && (
                              <div class="details-field field-error">
                                <span class="field-label">Error Code:</span>
                                <span class="error-code-badge">{node.error_code}</span>
                              </div>
                            )}
                            {node.error_message && (
                              <div class="details-field field-error">
                                <span class="field-label">Error Message:</span>
                                <span class="error-msg-text">{node.error_message}</span>
                              </div>
                            )}

                            {node.attributes && Object.keys(node.attributes).length > 0 && (
                              <div class="attributes-section">
                                <div class="attr-title">Allowlisted Attributes:</div>
                                <table class="attr-table">
                                  <tbody>
                                    {Object.entries(node.attributes).map(([k, v]) => (
                                      <tr key={k}>
                                        <td class="attr-key">{k}</td>
                                        <td class="attr-val">
                                          {typeof v === 'object' && v !== null
                                            ? JSON.stringify(v)
                                            : String(v)}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* TAB 2: AI Diagnosis View */}
        {activeTab === 'diagnosis' && (
          <div class="waterfall-tab-content diagnosis-tab-content">
            {!diagnosisData ? (
              <div class="diagnosis-intro-card">
                <div class="diag-intro-icon">🔍</div>
                <h3>Automated Fault & Telemetry Diagnosis</h3>
                <p>
                  Performs structured analysis over trace spans and telemetry evidence to pinpoint root causes and recommend fixes. Read-only and side-effect free.
                </p>

                {diagError && <div class="diag-error-banner">⚠️ {diagError}</div>}

                {countdownSec > 0 && (
                  <div class="countdown-banner">
                    ⏳ Rate limit cooldown: please wait <strong>{countdownSec}</strong>s before retrying analysis
                  </div>
                )}

                <button
                  type="button"
                  class="btn-primary btn-run-diag"
                  disabled={diagLoading || countdownSec > 0}
                  onClick={handleRunDiagnosis}
                >
                  {diagLoading ? (
                    <>
                      <span class="spinner-inline" /> Analyzing Trace Spans...
                    </>
                  ) : countdownSec > 0 ? (
                    `Please wait (${countdownSec}s)`
                  ) : (
                    '🚀 Run AI Diagnosis'
                  )}
                </button>
              </div>
            ) : (
              <div class="diagnosis-result-view">
                {/* Engine and Confidence Bar */}
                <div class="diag-header-row">
                  <div class="diag-engine-group">{renderEngineBadge(diagnosisData)}</div>
                  <div class="diag-pills-group">
                    <span class={`confidence-pill conf-${diagnosisData.confidence}`}>
                      Confidence: {diagnosisData.confidence.toUpperCase()}
                    </span>
                    <span class="action-pill">
                      Suggested Action: {diagnosisData.suggested_action}
                    </span>
                  </div>
                </div>

                {/* Safe Error Summary */}
                <div class="diag-error-box">
                  <div class="diag-box-label">Error Code & Summary</div>
                  <div class="diag-error-code">
                    <code>{diagnosisData.error_code}</code>
                  </div>
                  <div class="diag-error-summary">{diagnosisData.error_summary}</div>
                </div>

                {/* Faulting Span Alert */}
                {diagnosisData.faulting_span && (
                  <div class="faulting-span-card">
                    <div class="faulting-header">
                      <span class="faulting-icon">💥</span>
                      <span class="faulting-title">Faulting Span Identified</span>
                      <span class="faulting-span-name">
                        {diagnosisData.faulting_span.name}
                      </span>
                    </div>
                    <div class="faulting-meta">
                      <span>ID: <code>{diagnosisData.faulting_span.span_id}</code></span>
                      <span>Duration: {diagnosisData.faulting_span.duration_ms}ms</span>
                      <span>Status: {diagnosisData.faulting_span.status}</span>
                    </div>
                  </div>
                )}

                {/* Observations */}
                {diagnosisData.observations && diagnosisData.observations.length > 0 && (
                  <div class="diag-list-section">
                    <h4>Observations</h4>
                    <ul>
                      {diagnosisData.observations.map((item, idx) => (
                        <li key={idx}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Likely Causes */}
                {diagnosisData.likely_causes && diagnosisData.likely_causes.length > 0 && (
                  <div class="diag-list-section">
                    <h4>Likely Causes</h4>
                    <ul>
                      {diagnosisData.likely_causes.map((item, idx) => (
                        <li key={idx}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Recommendations */}
                {diagnosisData.recommendations && diagnosisData.recommendations.length > 0 && (
                  <div class="diag-list-section">
                    <h4>Recommendations</h4>
                    <ol>
                      {diagnosisData.recommendations.map((item, idx) => (
                        <li key={idx}>{item}</li>
                      ))}
                    </ol>
                  </div>
                )}

                {/* Re-run diagnosis button */}
                <div class="diag-footer-actions">
                  <button
                    type="button"
                    class="btn-secondary btn-compact"
                    disabled={diagLoading || countdownSec > 0}
                    onClick={handleRunDiagnosis}
                  >
                    {diagLoading ? 'Re-analyzing...' : countdownSec > 0 ? `Cooldown (${countdownSec}s)` : '↻ Re-analyze'}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </ResponsiveDrawer>
  );
}
