import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, waitFor, act } from '@testing-library/preact';
import { SpanWaterfallDrawer } from '../src/components/SpanWaterfallDrawer';
import { MetricsSummaryBar } from '../src/components/MetricsSummaryBar';
import { ConfirmDialog } from '../src/components/primitives/ConfirmDialog';
import { RightPanel } from '../src/components/RightPanel';
import { getSafeGrafanaUrl } from '../src/utils';
import {
  selectedWaterfallTargetSignal,
  runsSignal,
  spaceContextSignal,
  activeSpaceIdSignal,
  activeRightTabSignal,
  rightPanelOpenSignal,
} from '../src/services/store';
import { api } from '../src/services/api';
import { Run, RunTraceResponse, DiagnosisRecord, TelemetryVerificationResult, TelemetryMetricsSummary } from '../src/types';

describe('Phase E6: Span Waterfall Drawer & Task Monitoring Telemetry UI', () => {
  const mockRunA: Run = {
    run_id: 'run_alpha_001',
    space_id: 'space_test_1',
    project_tag: 'scene-1',
    status: 'failed',
    prompt: 'Analyze Scene 1 Screenplay Breakdown',
    created_by: 'user_director',
    failure_code: 'PDX_EXECUTION_FAILURE',
    is_retryable: true,
    created_at: '2026-09-05T10:00:00Z',
    telemetry: {
      duration_ms: 1250,
      llm_latency_ms: 800,
      pdx_exec_ms: 450,
      tool_calls: ['pdx_extract_nodes'],
      grafana_dashboard_url: 'https://grafana.internal.studiotower.ai/d/studiotower-runs?var-run_id=run_alpha_001',
      has_real_telemetry: true,
      telemetry_status: 'available',
      telemetry_generation: 1,
    },
  };

  const mockRunB: Run = {
    run_id: 'run_beta_002',
    space_id: 'space_test_1',
    project_tag: 'scene-2',
    status: 'completed',
    prompt: 'Generate Delivery Package for Episode 2',
    created_by: 'user_director',
    is_retryable: false,
    created_at: '2026-09-05T10:05:00Z',
    telemetry: {
      duration_ms: 540,
      llm_latency_ms: 300,
      pdx_exec_ms: 240,
      tool_calls: [],
      grafana_dashboard_url: 'http://insecure.grafana.com', // Insecure URL (should be rejected)
      has_real_telemetry: false,
      telemetry_status: 'not_instrumented',
      telemetry_generation: 1,
    },
  };

  const mockRunSyncing: Run = {
    run_id: 'run_sync_003',
    space_id: 'space_test_1',
    project_tag: 'scene-3',
    status: 'running',
    prompt: 'Extracting live characters',
    created_by: 'user_actor',
    created_at: '2026-09-05T10:10:00Z',
    telemetry: {
      duration_ms: 0,
      llm_latency_ms: 0,
      pdx_exec_ms: 0,
      tool_calls: [],
      has_real_telemetry: true,
      telemetry_status: 'exporting',
      telemetry_generation: 1,
    },
  };

  const mockTraceA: RunTraceResponse = {
    trace_id: 'trace_alpha_1234567890abcdef',
    run_id: 'run_alpha_001',
    space_id: 'space_test_1',
    service_name: 'studiotower-api',
    total_spans: 3,
    grafana_dashboard_url: 'https://grafana.internal.studiotower.ai/d/studiotower-runs?var-run_id=run_alpha_001',
    has_real_telemetry: true,
    spans: [
      {
        span_id: 'span_root',
        parent_span_id: null,
        name: 'pdx.orchestration',
        service_name: 'studiotower-api',
        start_time_iso: '2026-09-05T10:00:00.000Z',
        end_time_iso: '2026-09-05T10:00:01.250Z',
        duration_ms: 1250,
        offset_ms: 0,
        status: 'error',
        attributes: {
          run_id: 'run_alpha_001',
          action_type: 'breakdown_generation',
          stage: 'orchestration',
        },
        error_code: 'PDX_EXECUTION_FAILURE',
        error_message: 'Sub-action dispatch timed out',
      },
      {
        span_id: 'span_child_1',
        parent_span_id: 'span_root',
        name: 'gemini.generate_content',
        service_name: 'studiotower-api',
        start_time_iso: '2026-09-05T10:00:00.100Z',
        end_time_iso: '2026-09-05T10:00:00.900Z',
        duration_ms: 800,
        offset_ms: 100,
        status: 'ok',
        attributes: {
          model_id: 'gemini-2.0-flash',
          input_tokens: 1540,
          output_tokens: 420,
        },
      },
      {
        span_id: 'span_child_2',
        parent_span_id: 'span_root',
        name: 'pdx.bundle_artifacts',
        service_name: 'studiotower-api',
        start_time_iso: '2026-09-05T10:00:00.950Z',
        end_time_iso: '2026-09-05T10:00:01.250Z',
        duration_ms: 300,
        offset_ms: 950,
        status: 'error',
        attributes: {
          retry_count: 0,
        },
        error_code: 'PDX_EXECUTION_FAILURE',
        error_message: 'Deterministic bundle write error',
      },
    ],
  };

  const mockTraceB: RunTraceResponse = {
    trace_id: 'trace_beta_9876543210fedcba',
    run_id: 'run_beta_002',
    space_id: 'space_test_1',
    service_name: 'studiotower-api',
    total_spans: 1,
    has_real_telemetry: false,
    spans: [
      {
        span_id: 'span_b_root',
        name: 'mock.simple_pass',
        service_name: 'studiotower-api',
        start_time_iso: '2026-09-05T10:05:00.000Z',
        end_time_iso: '2026-09-05T10:05:00.540Z',
        duration_ms: 540,
        offset_ms: 0,
        status: 'ok',
        attributes: {},
      },
    ],
  };

  const mockDiagnosisA: DiagnosisRecord = {
    diagnosis_id: 'diag_alpha_001',
    space_id: 'space_test_1',
    run_id: 'run_alpha_001',
    trace_id: 'trace_alpha_1234567890abcdef',
    telemetry_generation: 1,
    schema_version: 1,
    engine: 'gemini_2_flash',
    model_id: 'gemini-2.0-flash',
    diagnostic_status: 'complete',
    faulting_span: {
      span_id: 'span_child_2',
      name: 'pdx.bundle_artifacts',
      duration_ms: 300,
      status: 'error',
      error_code: 'PDX_EXECUTION_FAILURE',
      key_attributes: { retry_count: 0 },
    },
    error_code: 'PDX_EXECUTION_FAILURE',
    error_summary: 'PDX action execution encountered an unexpected processing failure.',
    observations: ['Span pdx.bundle_artifacts returned status=error after 300ms'],
    likely_causes: ['Transient I/O latency or storage conflict during bundle write'],
    recommendations: ['Retry run execution to allow transient storage synchronization to recover'],
    evidence_span_ids: ['span_child_2'],
    is_retryable: true,
    suggested_action: 'retry',
    confidence: 'high',
    grafana_dashboard_url: 'https://grafana.internal.studiotower.ai/d/studiotower-runs?var-run_id=run_alpha_001',
    has_real_telemetry: true,
    is_local_diagnostic: false,
    created_at: '2026-09-05T10:00:05Z',
  };

  const mockMetrics: TelemetryMetricsSummary = {
    space_id: 'space_test_1',
    project_tag: null,
    time_window_hours: 24,
    rollup_schema_version: 2,
    data_status: 'available',
    cost_data_status: 'available',
    sample_count: 15,
    total_runs: 15,
    completed_runs: 12,
    failed_runs: 3,
    success_rate: 0.8,
    latency_percentile_method: 'histogram',
    latency_histogram_buckets: [25, 50, 100, 250, 500, 1000, 2500, 5000],
    latency_p50_ms: 320.5,
    latency_p95_ms: 1240.8,
    latency_percentile_capped: false,
    total_tokens_used: 28400,
    estimated_cost_usd: 0.0426,
    pricing_version: '2026-08-gemini-ga',
    pricing_versions_truncated: false,
    currency: 'USD',
    failures_by_code: {
      PDX_EXECUTION_FAILURE: 2,
      STORAGE_CONFLICT: 1,
    },
    generated_at: '2026-09-05T10:00:00Z',
  };

  beforeEach(() => {
    runsSignal.value = [mockRunA, mockRunB, mockRunSyncing];
    activeSpaceIdSignal.value = 'space_test_1';
    spaceContextSignal.value = {
      space: { space_id: 'space_test_1', name: 'Test Space', kind: 'shared_space', created_by: 'u1', created_at: '2026-01-01', tags: [] },
      current_user_role: 'admin',
      member_count: 3,
      capabilities: {
        can_invite: true,
        can_manage_members: true,
        can_change_role: true,
        can_remove_member: true,
        can_transfer_ownership: false,
        can_approve_runs: true,
        can_manage_tags: true,
        can_leave_space: true,
      },
    };
    selectedWaterfallTargetSignal.value = null;
    vi.restoreAllMocks();
  });

  afterEach(() => {
    selectedWaterfallTargetSignal.value = null;
  });

  // 1. Single #span-waterfall-drawer in DOM
  it('renders at most one #span-waterfall-drawer dialog in the DOM when opened', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);

    // Initial state: not open
    expect(document.getElementById('span-waterfall-drawer')).toBeNull();

    const { unmount } = render(<SpanWaterfallDrawer />);

    // Open target
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        triggerId: 'btn-trigger-1',
        requestGeneration: 1,
      };
    });

    await waitFor(() => {
      const drawers = document.querySelectorAll('#span-waterfall-drawer');
      expect(drawers.length).toBe(1);
    });

    // Switching target still preserves exactly one drawer element
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        triggerId: 'btn-trigger-2',
        requestGeneration: 2,
      };
    });

    const drawersAfterSwitch = document.querySelectorAll('#span-waterfall-drawer');
    expect(drawersAfterSwitch.length).toBe(1);

    unmount();
  });

  // 2. A -> B rapid switching cancellation
  it('discards late responses from run A after rapidly switching to run B', async () => {
    let resolveTraceA: (val: any) => void;
    const delayedPromiseA = new Promise((resolve) => {
      resolveTraceA = resolve;
    });

    vi.spyOn(api, 'getRunTrace').mockImplementation((spaceId, runId) => {
      if (runId === 'run_alpha_001') return delayedPromiseA as any;
      if (runId === 'run_beta_002') return Promise.resolve(mockTraceB);
      return Promise.reject(new Error('not found'));
    });

    render(<SpanWaterfallDrawer />);

    // 1. Select Run A
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 101,
      };
    });

    // 2. Rapidly switch to Run B before Run A resolves
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        requestGeneration: 102,
      };
    });

    // Wait for Run B to render
    await waitFor(() => {
      expect(document.body.textContent).toContain('mock.simple_pass');
    });

    // 3. Fulfill Run A's late promise
    act(() => {
      resolveTraceA!(mockTraceA);
    });

    // Verify Run A's spans do NOT overwrite Run B
    expect(document.body.textContent).toContain('mock.simple_pass');
    expect(document.body.textContent).not.toContain('pdx.orchestration');
  });

  // 3. 429 Real Retry-After Countdown
  it('parses real Retry-After header on 429, displays countdown, and re-enables button without auto-submitting', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);

    const err429: any = new Error('Rate limit exceeded: user quota 10/min exceeded');
    err429.status = 429;
    err429.retryAfter = 2; // 2 seconds

    const diagSpy = vi.spyOn(api, 'diagnoseRun').mockRejectedValueOnce(err429);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 201,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('Span Waterfall (3)');
    });

    // Switch to Diagnosis tab
    const diagTabBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.waterfall-tab-btn'))
      .find((b) => b.textContent?.includes('AI 根因診斷') || b.textContent?.includes('AI Diagnosis'));
    expect(diagTabBtn).toBeDefined();
    fireEvent.click(diagTabBtn!);

    // Click Run Diagnosis
    const runDiagBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-run-diag'))
      .find((b) => b.textContent?.includes('開始執行 AI 診斷') || b.textContent?.includes('Run AI Diagnosis'));
    expect(runDiagBtn).toBeDefined();

    await act(async () => {
      fireEvent.click(runDiagBtn!);
    });

    // Verify 429 error and countdown banner
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Rate limit cooldown|頻率冷卻中/);
      expect(document.body.textContent).toMatch(/please wait|請等待 2 秒/);
    });

    expect(diagSpy).toHaveBeenCalledTimes(1);

    // Advance 2.2 seconds to allow countdown to complete
    await act(async () => {
      await new Promise((r) => setTimeout(r, 2200));
    });

    // Button should be re-enabled for manual click, but NOT automatically re-submitted
    await waitFor(() => {
      const recoveredBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-run-diag'))
        .find((b) => b.textContent?.includes('開始執行 AI 診斷') || b.textContent?.includes('Run AI Diagnosis'));
      expect(recoveredBtn).toBeDefined();
      expect(recoveredBtn?.disabled).toBe(false);
    });

    // Strict assertion: diagSpy was called ONCE only (no automated resubmission)
    expect(diagSpy).toHaveBeenCalledTimes(1);
  });

  // 4. Telemetry Status Handling
  it('correctly handles exporting, delayed, unavailable, and 404 telemetry statuses', async () => {
    vi.spyOn(api, 'getRunTrace').mockRejectedValue({ status: 404, message: 'Trace not found' });

    render(<SpanWaterfallDrawer />);

    // Case 1: Run with telemetry_status = 'exporting'
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_sync_003',
        requestGeneration: 301,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Telemetry data synchronizing asynchronously|遙測資料尚在非同步同步中/);
      expect(document.body.textContent).toMatch(/Check Telemetry Status|檢查遙測狀態/);
    });

    // Case 2: Run with telemetry_status = 'not_instrumented'
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        requestGeneration: 302,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/No telemetry trace recorded for this run|此任務未記錄遙測追蹤/);
    });
  });

  // 5. Waterfall bounds safety (zero length, negative offsets, huge duration)
  it('clamps waterfall durations safely between 0% and 100% without illegal CSS values', async () => {
    const edgeTrace: RunTraceResponse = {
      trace_id: 'trace_edge',
      run_id: 'run_alpha_001',
      space_id: 'space_test_1',
      service_name: 'studiotower-api',
      total_spans: 3,
      has_real_telemetry: true,
      spans: [
        {
          span_id: 'span_zero',
          name: 'zero_duration',
          service_name: 'studiotower-api',
          start_time_iso: '2026-09-05T10:00:00Z',
          end_time_iso: '2026-09-05T10:00:00Z',
          duration_ms: 0,
          offset_ms: -50, // negative offset
          status: 'ok',
          attributes: {},
        },
        {
          span_id: 'span_huge',
          name: 'huge_span',
          service_name: 'studiotower-api',
          start_time_iso: '2026-09-05T10:00:00Z',
          end_time_iso: '2026-09-05T10:00:10Z',
          duration_ms: 10000,
          offset_ms: 200,
          status: 'warning',
          attributes: {},
        },
      ],
    };

    vi.spyOn(api, 'getRunTrace').mockResolvedValue(edgeTrace);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 401,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('zero_duration');
    });

    const bars = document.querySelectorAll<HTMLElement>('.span-bar');
    expect(bars.length).toBe(2);

    bars.forEach((bar) => {
      const left = parseFloat(bar.style.left);
      const width = parseFloat(bar.style.width);
      expect(Number.isFinite(left)).toBe(true);
      expect(Number.isFinite(width)).toBe(true);
      expect(left).toBeGreaterThanOrEqual(0);
      expect(left).toBeLessThanOrEqual(100);
      expect(width).toBeGreaterThanOrEqual(2); // min 2% visible width
      expect(left + width).toBeLessThanOrEqual(100.1); // clamped within 100%
    });
  });

  // 6. Span tree robustness: handles cyclic parent references and orphan spans
  it('protects against cyclic parent relationships and missing parent IDs without crashing', async () => {
    const cyclicTrace: RunTraceResponse = {
      trace_id: 'trace_cycle',
      run_id: 'run_alpha_001',
      space_id: 'space_test_1',
      service_name: 'studiotower-api',
      total_spans: 3,
      has_real_telemetry: true,
      spans: [
        {
          span_id: 'span_a',
          parent_span_id: 'span_b', // Cycle A -> B -> A
          name: 'span_cycle_a',
          service_name: 'studiotower-api',
          start_time_iso: '2026-09-05T10:00:00Z',
          end_time_iso: '2026-09-05T10:00:01Z',
          duration_ms: 100,
          offset_ms: 0,
          status: 'ok',
          attributes: {},
        },
        {
          span_id: 'span_b',
          parent_span_id: 'span_a', // Cycle B -> A
          name: 'span_cycle_b',
          service_name: 'studiotower-api',
          start_time_iso: '2026-09-05T10:00:00Z',
          end_time_iso: '2026-09-05T10:00:01Z',
          duration_ms: 80,
          offset_ms: 20,
          status: 'ok',
          attributes: {},
        },
        {
          span_id: 'span_orphan',
          parent_span_id: 'non_existent_parent_id', // Orphan
          name: 'span_orphan_node',
          service_name: 'studiotower-api',
          start_time_iso: '2026-09-05T10:00:00Z',
          end_time_iso: '2026-09-05T10:00:01Z',
          duration_ms: 50,
          offset_ms: 10,
          status: 'ok',
          attributes: {},
        },
      ],
    };

    vi.spyOn(api, 'getRunTrace').mockResolvedValue(cyclicTrace);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 501,
      };
    });

    // Should render without stack overflow
    await waitFor(() => {
      expect(document.body.textContent).toContain('span_cycle_a');
      expect(document.body.textContent).toContain('span_cycle_b');
      expect(document.body.textContent).toContain('span_orphan_node');
    });
  });

  // 7. Accurate Engine Branding: renders Gemini model_id or Rule-Based Engine
  it('accurately renders model_id branding for Gemini and Rule-Based for fallback without hardcoding versions', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 601,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('pdx.orchestration');
    });

    // Switch to Diagnosis tab
    const diagTab = Array.from(document.querySelectorAll<HTMLButtonElement>('.waterfall-tab-btn'))
      .find((b) => b.textContent?.includes('AI 根因診斷') || b.textContent?.includes('AI Diagnosis'));
    fireEvent.click(diagTab!);

    // 1. Mock Gemini diagnosis with model_id
    vi.spyOn(api, 'diagnoseRun').mockResolvedValueOnce(mockDiagnosisA);
    const runDiagBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-run-diag'))[0];
    await act(async () => {
      fireEvent.click(runDiagBtn);
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('Gemini · gemini-2.0-flash');
      expect(document.body.textContent).toMatch(/Confidence: HIGH|信心度: HIGH/);
      expect(document.body.textContent).toContain('pdx.bundle_artifacts');
    });

    // 2. Mock Rule-Based Fallback diagnosis
    const mockRuleDiag: DiagnosisRecord = {
      ...mockDiagnosisA,
      engine: 'rule_based',
      model_id: null,
      error_code: 'PDX_EXECUTION_FAILURE',
    };
    vi.spyOn(api, 'diagnoseRun').mockResolvedValueOnce(mockRuleDiag);

    const reRunBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-compact'))
      .find((b) => b.textContent?.includes('重新分析') || b.textContent?.includes('Run AI Diagnosis'));
    if (reRunBtn) {
      await act(async () => {
        fireEvent.click(reRunBtn);
      });
    }

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Rule-Based Engine|Gemini/);
    });
  });

  // 8. Side-Effect-Free Diagnosis: does not modify Run status or approval gate
  it('executing diagnosis is strictly read-only and does not mutate Run, status, or triggers retry', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);
    vi.spyOn(api, 'diagnoseRun').mockResolvedValue(mockDiagnosisA);
    const retrySpy = vi.spyOn(api, 'retryRun');

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 701,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('Span Waterfall');
    });

    const initialRunStatus = runsSignal.value.find((r) => r.run_id === 'run_alpha_001')?.status;
    expect(initialRunStatus).toBe('failed');

    const diagTab = Array.from(document.querySelectorAll<HTMLButtonElement>('.waterfall-tab-btn'))
      .find((b) => b.textContent?.includes('AI 根因診斷') || b.textContent?.includes('AI Diagnosis'));
    fireEvent.click(diagTab!);

    const runDiagBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-run-diag'))[0];
    await act(async () => {
      fireEvent.click(runDiagBtn);
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Faulting Span Identified|故障 Span 標定/);
    });

    // Run in store remains exactly in 'failed' status; retryRun was never called
    const runAfterDiag = runsSignal.value.find((r) => r.run_id === 'run_alpha_001');
    expect(runAfterDiag?.status).toBe('failed');
    expect(retrySpy).not.toHaveBeenCalled();
  });

  // 9. Verify Request Contract & Throttling
  it('sends { force: false } by default to verifyRunTelemetry and handles throttled=true state', async () => {
    vi.spyOn(api, 'getRunTrace').mockRejectedValue({ status: 404 });
    const verifySpy = vi.spyOn(api, 'verifyRunTelemetry').mockResolvedValue({
      run_id: 'run_sync_003',
      space_id: 'space_test_1',
      trace_id: 'trace_sync_1',
      previous_status: 'exporting',
      current_status: 'exporting',
      telemetry_generation: 1,
      matched: true,
      throttled: true,
      stale_rejected: false,
      attempts: 2,
    });

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_sync_003',
        requestGeneration: 801,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Check Telemetry Status|檢查遙測狀態/);
    });

    const checkBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-compact'))
      .find((b) => b.textContent?.includes('檢查遙測狀態') || b.textContent?.includes('Check Telemetry Status'));
    expect(checkBtn).toBeDefined();

    await act(async () => {
      fireEvent.click(checkBtn!);
    });

    expect(verifySpy).toHaveBeenCalledWith('space_test_1', 'run_sync_003', false, expect.any(AbortSignal));
  });

  // 10. 4-Tier Focus Restoration
  it('restores focus to triggerId button or fallback on drawer close', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);

    // Create a mock trigger button in document.body
    const triggerBtn = document.createElement('button');
    triggerBtn.id = 'btn-my-telemetry';
    triggerBtn.textContent = '遙測';
    document.body.appendChild(triggerBtn);
    triggerBtn.focus();
    expect(document.activeElement).toBe(triggerBtn);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        triggerId: 'btn-my-telemetry',
        requestGeneration: 901,
      };
    });

    await waitFor(() => {
      expect(document.getElementById('span-waterfall-drawer')).toBeDefined();
    });

    // Close drawer via Escape key
    fireEvent.keyDown(window, { key: 'Escape' });

    await waitFor(() => {
      expect(selectedWaterfallTargetSignal.value).toBeNull();
      expect(document.activeElement).toBe(triggerBtn);
    });

    document.body.removeChild(triggerBtn);
  });

  // 11. Metrics Summary Bar Verification
  it('renders space metrics summary with Success Rate, P50/P95, and unversioned cost display', async () => {
    vi.spyOn(api, 'getSpaceMetrics').mockResolvedValue(mockMetrics);

    const { getByText } = render(
      <MetricsSummaryBar spaceId="space_test_1" activeTag="all" />
    );

    await waitFor(() => {
      expect(getByText('80.0%')).toBeDefined();
      expect(getByText('321')).toBeDefined();
      expect(getByText('1241')).toBeDefined();
      expect(getByText('$0.0426')).toBeDefined();
      expect(getByText('PDX_EXECUTION_FAILURE: 2')).toBeDefined();
    });

    // Test when cost is unavailable
    vi.spyOn(api, 'getSpaceMetrics').mockResolvedValueOnce({
      ...mockMetrics,
      cost_data_status: 'unavailable',
      estimated_cost_usd: null,
    });

    const { getByText: getByText2 } = render(
      <MetricsSummaryBar spaceId="space_test_1" activeTag="scene-1" />
    );

    await waitFor(() => {
      expect(getByText2(/Unavailable|成本資料不可用/)).toBeDefined();
    });
  });

  // 12. [P0 Fix] Retry confirmation prompt destructuring (Cancel = 0 calls, Confirm = 1 call)
  it('does not invoke api.retryRun when user cancels promptConfirm, and invokes it exactly once on confirm', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);
    const retrySpy = vi.spyOn(api, 'retryRun').mockResolvedValue({ ...mockRunA, status: 'running' });

    render(
      <div>
        <SpanWaterfallDrawer />
        <ConfirmDialog />
      </div>
    );

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 1201,
      };
    });

    await waitFor(() => {
      expect(document.querySelector('.btn-retry-run')).toBeDefined();
    });

    const retryBtn = document.querySelector<HTMLButtonElement>('.btn-retry-run')!;

    // Scenario A: User clicks Retry, but clicks Cancel in ConfirmDialog
    await act(async () => {
      fireEvent.click(retryBtn);
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Are you sure you want to retry run #run_alpha_001\?|確定要重新執行任務 #run_alpha_001 嗎？/);
    });

    const cancelBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => b.textContent === '取消' || b.textContent === 'Cancel');
    expect(cancelBtn).toBeDefined();

    await act(async () => {
      fireEvent.click(cancelBtn!);
    });

    // Verification: retryRun was NEVER called
    expect(retrySpy).toHaveBeenCalledTimes(0);

    // Scenario B: User clicks Retry, and clicks Confirm in ConfirmDialog
    await act(async () => {
      fireEvent.click(retryBtn);
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Are you sure you want to retry run #run_alpha_001\?|確定要重新執行任務 #run_alpha_001 嗎？/);
    });

    const confirmBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => b.textContent === '確認重試' || b.textContent === 'Retry Task');
    expect(confirmBtn).toBeDefined();

    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    // Verification: retryRun was called EXACTLY ONCE
    await waitFor(() => {
      expect(retrySpy).toHaveBeenCalledTimes(1);
      expect(retrySpy).toHaveBeenCalledWith('space_test_1', 'run_alpha_001', expect.any(AbortSignal));
    });
  });

  // 13. [P0 Concurrency] In-flight retry for Run A is ignored if switched to Run B or closed
  it('discards late retry resolution for Run A if user switches to Run B before retry completes', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);
    let resolveRetryA: (val: any) => void;
    const delayedRetryPromise = new Promise((res) => {
      resolveRetryA = res;
    });
    vi.spyOn(api, 'retryRun').mockReturnValue(delayedRetryPromise as any);
    const listRunsSpy = vi.spyOn(api, 'listRuns');

    render(
      <div>
        <SpanWaterfallDrawer />
        <ConfirmDialog />
      </div>
    );

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 1301,
      };
    });

    await waitFor(() => {
      expect(document.querySelector('.btn-retry-run')).toBeDefined();
    });

    // Initiate retry
    await act(async () => {
      fireEvent.click(document.querySelector<HTMLButtonElement>('.btn-retry-run')!);
    });

    const confirmBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => b.textContent === '確認重試' || b.textContent === 'Retry Task');
    expect(confirmBtn).toBeDefined();
    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    // Rapidly switch target to Run B before retry A finishes
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        requestGeneration: 1302,
      };
    });

    // Late resolve of retry A
    await act(async () => {
      resolveRetryA!({ ...mockRunA, status: 'running' });
    });

    // The store's runsSignal should not be re-polled by the orphaned run A retry
    expect(listRunsSpy).toHaveBeenCalledTimes(0);
  });

  // 14. [P1 Fix] Immediate metrics clearing on space/tag/window change
  it('clears metrics immediately when switching space or tag so old values do not persist during loading', async () => {
    let resolveMetricsB: (val: any) => void;
    const delayedMetricsPromiseB = new Promise((res) => {
      resolveMetricsB = res;
    });

    vi.spyOn(api, 'getSpaceMetrics').mockImplementation((spaceId) => {
      if (spaceId === 'space_test_1') return Promise.resolve(mockMetrics);
      if (spaceId === 'space_test_2') return delayedMetricsPromiseB as any;
      return Promise.reject(new Error('not found'));
    });

    const { rerender } = render(
      <MetricsSummaryBar spaceId="space_test_1" activeTag="all" />
    );

    await waitFor(() => {
      expect(document.body.textContent).toContain('80.0%');
      expect(document.body.textContent).toContain('$0.0426');
    });

    // Switch to space_test_2
    rerender(<MetricsSummaryBar spaceId="space_test_2" activeTag="all" />);

    // IMMEDIATELY: Space 1 metrics should be cleared, loading state shown
    expect(document.body.textContent).not.toContain('80.0%');
    expect(document.body.textContent).not.toContain('$0.0426');
    expect(document.querySelector('.metrics-bar-loading')).toBeDefined();

    // Resolve space 2
    await act(async () => {
      resolveMetricsB!({
        ...mockMetrics,
        space_id: 'space_test_2',
        success_rate: 0.95,
        estimated_cost_usd: 0.1234,
      });
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('95.0%');
      expect(document.body.textContent).toContain('$0.1234');
    });
  });

  // 15. [P1 Fix] Grafana HTTPS URL enforcement across Drawer and RightPanel
  it('enforces https-only Grafana URLs using getSafeGrafanaUrl and rejects http:// in both Drawer and RightPanel', async () => {
    // 1. Direct utility test
    expect(getSafeGrafanaUrl('https://demo.grafana.net/d/123')).toBe('https://demo.grafana.net/d/123');
    expect(getSafeGrafanaUrl('https://grafana.internal.com/d/123')).toBeNull();
    expect(getSafeGrafanaUrl('http://insecure.grafana.com')).toBeNull();
    expect(getSafeGrafanaUrl('javascript:alert(1)')).toBeNull();
    expect(getSafeGrafanaUrl(null)).toBeNull();

    // 2. In SpanWaterfallDrawer with mockRunB (which has http://insecure.grafana.com)
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceB);

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        requestGeneration: 1501,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('mock.simple_pass');
    });

    // Insecure link should NOT be rendered as an anchor tag
    const grafanaAnchor = document.querySelector('a.btn-grafana-link');
    expect(grafanaAnchor).toBeNull();
  });

  // 16. [P1 Fix] Authoritative top-level telemetry_status on Run
  it('authoritatively reads top-level run.telemetry_status even when nested run.telemetry is missing or empty', async () => {
    const runWithTopLevelOnly: Run = {
      run_id: 'run_toplevel_099',
      space_id: 'space_test_1',
      project_tag: 'scene-99',
      status: 'running',
      prompt: 'Top-level status test',
      created_by: 'director',
      created_at: '2026-09-05T12:00:00Z',
      telemetry_status: 'exporting',
      telemetry_generation: 4,
    };

    runsSignal.value = [...runsSignal.value, runWithTopLevelOnly];
    vi.spyOn(api, 'getRunTrace').mockRejectedValue({ status: 404 });

    render(<SpanWaterfallDrawer />);

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_toplevel_099',
        requestGeneration: 1601,
      };
    });

    await waitFor(() => {
      expect(document.body.textContent).toContain('EXPORTING');
      expect(document.body.textContent).toMatch(/Telemetry data synchronizing asynchronously|遙測資料尚在非同步同步中/);
    });
  });

  // 17. [P1 Fix] Target switch during open Confirm dialog cancels retry
  it('does not invoke api.retryRun if user switches target Run while confirm dialog is open before confirming', async () => {
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);
    const retrySpy = vi.spyOn(api, 'retryRun').mockResolvedValue({ ...mockRunA, status: 'running' });

    render(
      <div>
        <SpanWaterfallDrawer />
        <ConfirmDialog />
      </div>
    );

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 1701,
      };
    });

    await waitFor(() => {
      expect(document.querySelector('.btn-retry-run')).toBeDefined();
    });

    // 1. Open confirmation modal for Run A
    await act(async () => {
      fireEvent.click(document.querySelector<HTMLButtonElement>('.btn-retry-run')!);
    });

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Are you sure you want to retry run #run_alpha_001\?|確定要重新執行任務 #run_alpha_001 嗎？/);
    });

    // 2. While dialog is open, target changes to Run B (e.g. from notification, list click, or background)
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_beta_002',
        requestGeneration: 1702,
      };
    });

    // 3. User now clicks Confirm on the still-open dialog
    const confirmBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => b.textContent === 'Retry Task' || b.textContent === '確認重試');
    expect(confirmBtn).toBeDefined();

    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    // 4. Verification: retryRun must NOT be called (0 calls) because target switched!
    expect(retrySpy).toHaveBeenCalledTimes(0);
  });

  // 18. [P1 Fix] Switching to another failed/retryable Run resets retrying loading state
  it('resets retrying state immediately when switching to another failed/retryable Run', async () => {
    const mockRunFailed2: Run = {
      run_id: 'run_failed_002',
      space_id: 'space_test_1',
      project_tag: 'scene-2',
      status: 'failed',
      prompt: 'Retryable Second Run',
      created_by: 'user_director',
      failure_code: 'PDX_EXECUTION_FAILURE',
      is_retryable: true,
      created_at: '2026-09-05T10:05:00Z',
      telemetry: {
        duration_ms: 100,
        llm_latency_ms: 50,
        pdx_exec_ms: 50,
        tool_calls: [],
        has_real_telemetry: true,
        telemetry_status: 'available',
        telemetry_generation: 1,
      },
    };

    runsSignal.value = [...runsSignal.value, mockRunFailed2];
    vi.spyOn(api, 'getRunTrace').mockResolvedValue(mockTraceA);

    let resolveRetryA: (val: any) => void;
    const pendingRetryPromiseA = new Promise((res) => {
      resolveRetryA = res;
    });
    vi.spyOn(api, 'retryRun').mockReturnValue(pendingRetryPromiseA as any);

    render(
      <div>
        <SpanWaterfallDrawer />
        <ConfirmDialog />
      </div>
    );

    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_alpha_001',
        requestGeneration: 1801,
      };
    });

    await waitFor(() => {
      expect(document.querySelector('.btn-retry-run')).toBeDefined();
    });

    // Start retry for Run A
    await act(async () => {
      fireEvent.click(document.querySelector<HTMLButtonElement>('.btn-retry-run')!);
    });

    const confirmBtn = Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => b.textContent === 'Retry Task' || b.textContent === '確認重試');
    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    // Run A's button should now be in loading state
    await waitFor(() => {
      expect(document.querySelector<HTMLButtonElement>('.btn-retry-run')?.textContent).toMatch(/Dispatching retry\.\.\.|Retrying|重試派送中/);
      expect(document.querySelector<HTMLButtonElement>('.btn-retry-run')?.disabled).toBe(true);
    });

    // Switch directly to Run 2 (which is also failed and retryable)
    act(() => {
      selectedWaterfallTargetSignal.value = {
        spaceId: 'space_test_1',
        runId: 'run_failed_002',
        requestGeneration: 1802,
      };
    });

    // Button for Run 2 MUST NOT be stuck in '重試派送中...'
    await waitFor(() => {
      const btn = document.querySelector<HTMLButtonElement>('.btn-retry-run');
      expect(btn).toBeDefined();
      expect(btn?.textContent).toMatch(/Retry Run|重試任務/);
      expect(btn?.disabled).toBe(false);
    });

    // Resolve old retry A to clean up promise
    await act(async () => {
      resolveRetryA!({ ...mockRunA, status: 'running' });
    });
  });
});
