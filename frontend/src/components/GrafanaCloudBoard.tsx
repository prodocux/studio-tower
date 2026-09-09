import { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { GrafanaDashboardVisual, GrafanaLineageStep, GrafanaPanelVisual, GrafanaSeries, GrafanaVisualBoard } from '../types';
import { api } from '../services/api';
import { getSafeGrafanaUrl } from '../utils';

const GRAFANA_SERIES_COLORS = ['#73BF69', '#F2CC0C', '#FF9830', '#F2495C', '#5794F2', '#B877D9'];

function sparklinePath(points: number[][], width: number, height: number, domain: { minX: number; maxX: number; minY: number; maxY: number }): string {
  if (points.length === 0) return '';
  const spanX = domain.maxX - domain.minX || 1;
  const spanY = domain.maxY - domain.minY || 1;
  return points
    .map((point, index) => {
      const x = ((point[0] - domain.minX) / spanX) * (width - 8) + 4;
      const y = height - 4 - ((point[1] - domain.minY) / spanY) * (height - 8);
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(' ');
}

function seriesDomain(seriesList: GrafanaSeries[] | undefined): { minX: number; maxX: number; minY: number; maxY: number } | null {
  const points = (seriesList || []).flatMap((series) => series.points);
  if (points.length === 0) return null;
  return {
    minX: Math.min(...points.map((p) => p[0])),
    maxX: Math.max(...points.map((p) => p[0])),
    minY: Math.min(...points.map((p) => p[1])),
    maxY: Math.max(...points.map((p) => p[1])),
  };
}

function lastValue(series: GrafanaSeries): string | null {
  const last = series.points[series.points.length - 1];
  if (!last) return null;
  const value = last[1];
  if (!Number.isFinite(value)) return null;
  return formatNumber(value);
}

function formatNumber(value: number): string {
  if (Number.isInteger(value) || Math.abs(value - Math.round(value)) < 0.05) {
    return String(Math.round(value));
  }
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
}

function isTimeValueTable(rows: string[][]): boolean {
  if (!rows[0] || rows[0].length === 0) return false;
  const header = rows[0].map((cell) => cell.toLowerCase());
  return header.some((cell) => cell.includes('time')) && header.some((cell) => cell.includes('value'));
}

function panelStatValue(panel: GrafanaPanelVisual): string | null {
  const primary = (panel.series || [])[0];
  const fromSeries = primary ? lastValue(primary) : null;
  if (fromSeries) return fromSeries;
  const rows = panel.table_rows || [];
  if (rows.length < 2) return null;
  const cell = rows[rows.length - 1][rows[rows.length - 1].length - 1];
  const value = Number(cell);
  return Number.isFinite(value) ? formatNumber(value) : null;
}

function barItems(rows: string[][]): { name: string; value: number }[] {
  if (rows.length < 2 || isTimeValueTable(rows)) return [];
  const items: { name: string; value: number }[] = [];
  for (const row of rows.slice(1)) {
    if (row.length < 2) continue;
    const value = Number(row[row.length - 1]);
    if (!Number.isFinite(value)) continue;
    items.push({ name: row[0], value });
  }
  return items;
}

function hasSparkline(panel: GrafanaPanelVisual): boolean {
  const series = panel.series || [];
  return Boolean(seriesDomain(series) && series[0] && series[0].points.length >= 1);
}

function sparklineDot(point: number[], width: number, height: number, domain: { minX: number; maxX: number; minY: number; maxY: number }): { x: number; y: number } {
  const spanX = domain.maxX - domain.minX || 1;
  const spanY = domain.maxY - domain.minY || 1;
  return {
    x: ((point[0] - domain.minX) / spanX) * (width - 8) + 4,
    y: height - 4 - ((point[1] - domain.minY) / spanY) * (height - 8),
  };
}

function Sparkline({ panel, tall = false }: { panel: GrafanaPanelVisual; tall?: boolean }): JSX.Element | null {
  if (!hasSparkline(panel)) return null;
  const domain = seriesDomain(panel.series)!;
  const width = 280;
  const height = tall ? 160 : 120;
  const seriesList = panel.series || [];
  return (
    <svg
      class={tall ? 'grafana-sparkline grafana-sparkline-lg' : 'grafana-sparkline'}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={panel.title}
    >
      {seriesList.map((series, index) => (
        <g key={series.name || String(index)}>
          {series.points.length > 1 ? (
            <path
              d={sparklinePath(series.points, width, height, domain)}
              fill="none"
              stroke={GRAFANA_SERIES_COLORS[index % GRAFANA_SERIES_COLORS.length]}
              stroke-width="2"
            />
          ) : null}
          {series.points.map((point, pointIndex) => {
            const dot = sparklineDot(point, width, height, domain);
            return (
              <circle
                key={pointIndex}
                cx={dot.x}
                cy={dot.y}
                r="3"
                fill={GRAFANA_SERIES_COLORS[index % GRAFANA_SERIES_COLORS.length]}
              />
            );
          })}
        </g>
      ))}
    </svg>
  );
}

function PanelCard({ panel, chart = false }: { panel: GrafanaPanelVisual; chart?: boolean }): JSX.Element {
  const imageSrc = panel.image_base64 ? `data:image/png;base64,${panel.image_base64}` : null;
  const isStat = panel.type === 'stat' || panel.type === 'gauge' || panel.type === 'bargauge';
  const isChart = chart || panel.type === 'timeseries' || panel.type === 'graph' || panel.type === 'trend';
  const primary = (panel.series || [])[0];
  const statValue = panelStatValue(panel);
  const tableRows = panel.table_rows || [];
  const bars = barItems(tableRows);
  const maxBar = Math.max(...bars.map((item) => item.value), 0);
  return (
    <article class={`grafana-panel-card${isChart ? ' grafana-panel-chart' : ''}${isStat ? ' grafana-panel-stat' : ''}`}>
      <header class="grafana-panel-title">{panel.title}</header>
      {imageSrc ? (
        <img class="grafana-panel-image" src={imageSrc} alt={panel.title} />
      ) : isChart && hasSparkline(panel) ? (
        <Sparkline panel={panel} tall />
      ) : isChart ? (
        <div class="grafana-chart-empty">
          <svg class="grafana-sparkline grafana-sparkline-lg" viewBox="0 0 280 160" aria-hidden="true">
            <line x1="4" y1="156" x2="276" y2="156" stroke="rgba(204,204,220,0.2)" />
            <line x1="4" y1="4" x2="4" y2="156" stroke="rgba(204,204,220,0.2)" />
          </svg>
          <p class="grafana-panel-empty">
            {panel.query_error || 'Grafana Prometheus has no samples for this Space in the last 7 days.'}
          </p>
        </div>
      ) : isStat && statValue ? (
        <div class="grafana-stat-value" aria-label={panel.title}>
          {statValue}
          {primary?.name && primary.name.toLowerCase() !== 'value' ? (
            <span class="grafana-stat-name">{primary.name}</span>
          ) : null}
        </div>
      ) : bars.length > 0 ? (
        <div class="grafana-bar-list" aria-label={panel.title}>
          {bars.map((item) => (
            <div class="grafana-bar-row" key={item.name}>
              <span class="grafana-bar-label">{item.name}</span>
              <span class="grafana-bar-track">
                <span
                  class="grafana-bar-fill"
                  style={{ width: `${maxBar > 0 ? Math.max(4, (item.value / maxBar) * 100) : 0}%` }}
                />
              </span>
              <span class="grafana-bar-value">{formatNumber(item.value)}</span>
            </div>
          ))}
        </div>
      ) : hasSparkline(panel) ? (
        <Sparkline panel={panel} />
      ) : tableRows.length > 1 && !isTimeValueTable(tableRows) ? (
        <div class="grafana-table-wrap">
          <table class="grafana-data-table">
            <thead>
              <tr>
                {tableRows[0].map((cell) => (
                  <th key={cell}>{cell}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.slice(1).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : statValue ? (
        <div class="grafana-stat-value" aria-label={panel.title}>
          {statValue}
        </div>
      ) : (
        <p class="grafana-panel-empty">
          {panel.query_error || `Grafana panel ${panel.type} loaded; no points in this 7-day window.`}
        </p>
      )}
    </article>
  );
}

function LineageFlow({ steps }: { steps: GrafanaLineageStep[] }): JSX.Element {
  if (steps.length === 0) {
    return (
      <div class="grafana-lineage-flow grafana-lineage-flow-empty">
        <h4>Space activity flow</h4>
        <p>Grafana Tempo has no Space events yet. Confirm a run or send a message, then wait about 20 seconds.</p>
      </div>
    );
  }
  return (
    <div class="grafana-lineage-flow" aria-label="Grafana Tempo space activity flow">
      <h4>Space activity flow</h4>
      <p class="grafana-lineage-flow-caption">Grafana Tempo spans for this Space. Asset names come from span resource_name / resource_id.</p>
      <div class="dag-container">
        {steps.map((step, index) => (
          <div key={step.node_id}>
            <article class={`dag-node grafana-flow-node stage-${step.stage}`}>
              <div class="dag-node-header">
                <span class={`dag-node-type-badge badge-${step.stage === 'file' ? 'source' : step.stage === 'artifact' ? 'manifest' : step.stage === 'message' ? 'extract' : step.stage === 'run' ? 'ai' : step.stage === 'gate' ? 'gate' : 'manifest'}`}>
                  {step.stage === 'artifact' ? 'asset' : step.stage}
                </span>
              </div>
              <div class="dag-node-title">{step.title}</div>
              {step.detail ? <p class="grafana-flow-detail">{step.detail}</p> : null}
            </article>
            {index < steps.length - 1 ? (
              <div class="dag-connector-wrapper">
                <span class="dag-connector-pill">then</span>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function DashboardBlock({
  dashboard,
  mode = 'all',
}: {
  dashboard: GrafanaDashboardVisual;
  mode?: 'all' | 'telemetry' | 'lineage';
}): JSX.Element | null {
  const openUrl = getSafeGrafanaUrl(dashboard.url);
  const panels = dashboard.panels.filter((panel) => panelMatchesMode(panel, mode));
  if (panels.length === 0) return null;
  const stats = panels.filter((panel) => panel.type === 'stat' || panel.type === 'gauge' || panel.type === 'bargauge');
  const charts = panels.filter((panel) => panel.type === 'timeseries' || panel.type === 'graph' || panel.type === 'trend');
  const rest = panels.filter((panel) => !stats.includes(panel) && !charts.includes(panel));
  const useTelemetryLayout = mode === 'telemetry';
  return (
    <section class="grafana-dashboard-block">
      <div class="grafana-dashboard-header">
        <h4>{dashboard.title}</h4>
        {openUrl && (
          <a class="btn-outline btn-compact" href={openUrl} target="_blank" rel="noopener noreferrer">
            Open in Grafana
          </a>
        )}
      </div>
      {useTelemetryLayout ? (
        <div class="grafana-telemetry-layout">
          {stats.length > 0 ? (
            <div class="grafana-stat-grid">
              {stats.map((panel) => (
                <PanelCard key={panel.panel_id} panel={panel} />
              ))}
            </div>
          ) : null}
          {charts.map((panel) => (
            <PanelCard key={panel.panel_id} panel={panel} chart />
          ))}
          {rest.map((panel) => (
            <PanelCard key={panel.panel_id} panel={panel} />
          ))}
        </div>
      ) : (
        <div class="grafana-panel-grid">
          {panels.map((panel) => (
            <PanelCard key={panel.panel_id} panel={panel} />
          ))}
        </div>
      )}
    </section>
  );
}

function panelMatchesMode(panel: GrafanaPanelVisual, mode: 'all' | 'telemetry' | 'lineage'): boolean {
  if (mode === 'all') return true;
  const title = (panel.title || '').toLowerCase();
  const isTempo = title.includes('tempo') || title.includes('trace');
  const isLineageTable = title.includes('lineage');
  if (mode === 'lineage') return isTempo;
  return !isTempo && !isLineageTable;
}

export function GrafanaCloudBoard({
  spaceId,
  mode = 'all',
}: {
  spaceId: string | null;
  mode?: 'all' | 'telemetry' | 'lineage';
}): JSX.Element {
  const [board, setBoard] = useState<GrafanaVisualBoard | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');

  useEffect(() => {
    if (!spaceId) return;
    let cancelled = false;
    let controller = new AbortController();

    const load = (showLoading: boolean) => {
      controller.abort();
      controller = new AbortController();
      if (showLoading) setState('loading');
      api
        .getGrafanaVisual(spaceId, controller.signal)
        .then((payload) => {
          if (cancelled) return;
          setBoard(payload);
          setState('ready');
        })
        .catch((err: { name?: string }) => {
          if (cancelled || controller.signal.aborted || err?.name === 'AbortError') return;
          if (showLoading) setState('error');
        });
    };

    load(true);
    const timer = window.setInterval(() => load(false), 20000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [spaceId]);

  if (!spaceId) {
    return (
      <div class="grafana-visual-empty">
        <h4>Grafana Cloud</h4>
        <p>Join a Space to load Grafana dashboards.</p>
      </div>
    );
  }

  if (state === 'loading') {
    return (
      <div class="grafana-visual-empty">
        <h4>Grafana Cloud</h4>
        <p>Loading dashboards from Grafana HTTP API…</p>
      </div>
    );
  }

  if (state === 'error' || !board) {
    return (
      <div class="panel-error-state grafana-visual-empty">
        <h4>Grafana visuals unavailable</h4>
        <p>Could not reach StudioTower’s Grafana visual endpoint.</p>
      </div>
    );
  }

  if (!board.connected) {
    return (
      <div class="grafana-visual-empty">
        <h4>Grafana Cloud</h4>
        <p>{board.error || 'Grafana is not configured for this revision. This tab will not invent charts.'}</p>
      </div>
    );
  }

  if (mode !== 'lineage' && board.dashboards.length === 0) {
    return (
      <div class="grafana-visual-empty">
        <h4>Grafana Cloud connected</h4>
        <p>{board.error || 'No dashboards are visible to the Grafana service account.'}</p>
      </div>
    );
  }

  return (
    <div class="grafana-visual-board" data-grafana-source={board.source || 'grafana-http-api'}>
      <div class="grafana-visual-kicker">
        Grafana Cloud
        {board.ingest ? ` · ingest ${board.ingest}` : ''}
      </div>
      {board.error ? <p class="grafana-panel-empty">{board.error}</p> : null}
      {mode === 'lineage' ? <LineageFlow steps={board.lineage_flow || []} /> : null}
      {mode !== 'lineage'
        ? board.dashboards.map((dashboard) => (
            <DashboardBlock key={dashboard.uid} dashboard={dashboard} mode={mode} />
          ))
        : null}
    </div>
  );
}
