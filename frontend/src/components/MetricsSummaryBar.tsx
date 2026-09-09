import { JSX } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import { api } from '../services/api';
import { TelemetryMetricsSummary } from '../types';

interface MetricsSummaryBarProps {
  spaceId: string;
  activeTag?: string;
}

export function MetricsSummaryBar({ spaceId, activeTag }: MetricsSummaryBarProps): JSX.Element | null {
  const [metrics, setMetrics] = useState<TelemetryMetricsSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [timeWindowHours, setTimeWindowHours] = useState<number>(24);

  const requestGenRef = useRef<number>(0);
  const acRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!spaceId) {
      setMetrics(null);
      setLoading(false);
      setError(null);
      return;
    }

    const gen = ++requestGenRef.current;
    acRef.current?.abort();
    const ac = new AbortController();
    acRef.current = ac;

    setMetrics(null);
    setLoading(true);
    setError(null);

    const effectiveTag = activeTag && activeTag !== 'all' ? activeTag : undefined;

    api
      .getSpaceMetrics(spaceId, timeWindowHours, effectiveTag, ac.signal)
      .then((res) => {
        if (requestGenRef.current === gen && !ac.signal.aborted) {
          setMetrics(res);
          setLoading(false);
        }
      })
      .catch((err: any) => {
        if (requestGenRef.current !== gen || ac.signal.aborted) return;
        setLoading(false);
        setError(err.message || 'Failed to load telemetry metrics');
      });

    return () => {
      ac.abort();
    };
  }, [spaceId, activeTag, timeWindowHours]);

  if (!spaceId) return null;

  const renderDataStatusBadge = (status?: string) => {
    switch (status) {
      case 'available':
        return <span class="metrics-status-badge status-available">✓ Telemetry Ready</span>;
      case 'warming_up':
        return <span class="metrics-status-badge status-warming">⏳ Warming Up</span>;
      default:
        return <span class="metrics-status-badge status-unavailable">⚠️ No Data Yet</span>;
    }
  };

  const renderCostDisplay = () => {
    if (!metrics) return '-';
    if (metrics.cost_data_status === 'available' && metrics.estimated_cost_usd !== null && metrics.estimated_cost_usd !== undefined) {
      return (
        <span class="cost-value">
          ${metrics.estimated_cost_usd.toFixed(4)}{' '}
          <span class="cost-currency">{metrics.currency}</span>
          {metrics.pricing_version && (
            <span class="cost-version" title={`Pricing version: ${metrics.pricing_version}`}>
              ({metrics.pricing_version})
            </span>
          )}
        </span>
      );
    }
    if (metrics.cost_data_status === 'partial') {
      return <span class="cost-unavailable">Partial Data</span>;
    }
    return <span class="cost-unavailable">Unavailable</span>;
  };

  const topFailures = metrics?.failures_by_code
    ? Object.entries(metrics.failures_by_code).filter(([_, count]) => count > 0)
    : [];

  return (
    <div class="metrics-summary-bar-card" aria-label="Space Telemetry Metrics Summary">
      <div class="metrics-bar-header">
        <div class="metrics-title-group">
          <span class="metrics-icon">📈</span>
          <h3 class="metrics-title">Telemetry Overview & SLO</h3>
          {metrics && renderDataStatusBadge(metrics.data_status)}
        </div>
        <div class="metrics-controls-group">
          <label htmlFor="metrics-window-select" class="metrics-window-label">
            Time Window:
          </label>
          <select
            id="metrics-window-select"
            class="metrics-window-select"
            value={timeWindowHours}
            onChange={(e) => setTimeWindowHours(Number((e.target as HTMLSelectElement).value))}
          >
            <option value={1}>Last 1 hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={72}>Last 3 days</option>
            <option value={168}>Last 7 days</option>
          </select>
        </div>
      </div>

      {loading && !metrics ? (
        <div class="metrics-loading-row">
          <span class="spinner-inline" /> Aggregating telemetry metrics...
        </div>
      ) : error && !metrics ? (
        <div class="metrics-error-row">
          <span>⚠️ {error}</span>
        </div>
      ) : (
        <div class="metrics-stats-grid">
          {/* Tile 1: Success Rate */}
          <div class="metric-stat-tile">
            <span class="tile-label">Success Rate</span>
            <div class="tile-main-val">
              {metrics ? `${(metrics.success_rate * 100).toFixed(1)}%` : '-'}
            </div>
            <span class="tile-subtext">
              {metrics ? `${metrics.completed_runs} completed / ${metrics.total_runs} total` : '-'}
            </span>
          </div>

          {/* Tile 2: Latency Percentiles */}
          <div class="metric-stat-tile">
            <span class="tile-label">Latency (P50 / P95)</span>
            <div class="tile-main-val">
              {metrics ? (
                <>
                  <span>{metrics.latency_p50_ms.toFixed(0)}</span>
                  <span class="val-sub">ms</span>
                  <span class="val-sep">/</span>
                  <span>{metrics.latency_p95_ms.toFixed(0)}</span>
                  <span class="val-sub">ms</span>
                </>
              ) : (
                '-'
              )}
            </div>
            <span class="tile-subtext">
              {metrics?.latency_percentile_capped ? '⚠️ Percentile capped at upper bound' : 'Estimated via histogram interpolation'}
            </span>
          </div>

          {/* Tile 3: Estimated Cost */}
          <div class="metric-stat-tile">
            <span class="tile-label">Estimated AI Cost</span>
            <div class="tile-main-val">{renderCostDisplay()}</div>
            <span class="tile-subtext">
              {metrics ? `Total: ${metrics.total_tokens_used.toLocaleString()} Tokens` : '-'}
            </span>
          </div>

          {/* Tile 4: Failure Taxonomy */}
          <div class="metric-stat-tile">
            <span class="tile-label">Failures</span>
            <div class="tile-main-val">
              {metrics ? `${metrics.failed_runs} failed` : '-'}
            </div>
            <div class="tile-failures-tags">
              {topFailures.length === 0 ? (
                <span class="no-failures-text">No failures recorded ✓</span>
              ) : (
                topFailures.slice(0, 2).map(([code, count]) => (
                  <span key={code} class="failure-tag-pill" title={`${code}: ${count} failures`}>
                    {code}: {count}
                  </span>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
