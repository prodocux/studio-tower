import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/preact';
import { GrafanaCloudBoard } from '../src/components/GrafanaCloudBoard';
import { api } from '../src/services/api';

const grafanaBoard = {
  connected: true,
  source: 'grafana-http-api',
  ingest: 'exporting',
  lineage_flow: [
    { node_id: 'tempo_0_aaa', title: 'Treatment.pdf', stage: 'file', detail: 'File ingested', resource_name: 'Treatment.pdf' },
    { node_id: 'tempo_1_bbb', title: 'shoot_schedule.csv', stage: 'artifact', detail: 'AI run completed', resource_name: 'shoot_schedule.csv' },
  ],
  dashboards: [
    {
      uid: 'studiotower-monitor',
      title: 'StudioTower Space Activity',
      url: 'https://loftyladybug3305.grafana.net/d/studiotower-monitor/studiotower-runtime',
      panels: [
        {
          panel_id: 1,
          title: 'Space events per minute',
          type: 'timeseries',
          series: [
            { name: 'events', points: [[1, 10], [2, 20], [3, 15]] },
          ],
        },
        {
          panel_id: 2,
          title: 'Messages (24h)',
          type: 'stat',
          series: [{ name: 'Value', points: [[1, 4]] }],
        },
        {
          panel_id: 6,
          title: 'Lineage resources (24h)',
          type: 'table',
          table_rows: [
            ['Time', 'Value'],
            ['2026-09-09 07:53 UTC', '0'],
          ],
        },
        {
          panel_id: 7,
          title: 'Space lineage traces (Tempo)',
          type: 'table',
          table_rows: [
            ['trace_id', 'span'],
            ['abc123', 'studiotower.space.event'],
          ],
        },
      ],
    },
  ],
};

describe('GrafanaCloudBoard', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders Grafana HTTP API panel series without exposing the stack URL', async () => {
    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue(grafanaBoard);

    const { getByText, container } = render(<GrafanaCloudBoard spaceId="space_test_1" />);
    await waitFor(() => {
      expect(getByText('StudioTower Space Activity')).toBeDefined();
      expect(getByText('Space events per minute')).toBeDefined();
      expect(getByText('Open in Grafana')).toBeDefined();
      expect(container.querySelector('svg.grafana-sparkline')).not.toBeNull();
    });
    expect(container.querySelector('.grafana-visual-kicker')?.textContent || '').not.toMatch(/grafana\.net/i);
    expect(container.textContent).not.toContain('loftyladybug3305');
  });

  it('renders telemetry as stats and a trend chart, not lineage tables', async () => {
    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue(grafanaBoard);
    const { getByText, queryByText, container } = render(
      <GrafanaCloudBoard spaceId="space_test_1" mode="telemetry" />,
    );
    await waitFor(() => {
      expect(getByText('Space events per minute')).toBeDefined();
      expect(getByText('Messages (24h)')).toBeDefined();
      expect(container.querySelector('.grafana-stat-grid')).not.toBeNull();
      expect(container.querySelector('svg.grafana-sparkline')).not.toBeNull();
    });
    expect(queryByText('Lineage resources (24h)')).toBeNull();
    expect(queryByText('Space lineage traces (Tempo)')).toBeNull();
  });

  it('renders Tempo spans as a process flow on the lineage tab', async () => {
    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue(grafanaBoard);
    const { getByText, queryByText, container } = render(
      <GrafanaCloudBoard spaceId="space_test_1" mode="lineage" />,
    );
    await waitFor(() => {
      expect(container.querySelector('.grafana-lineage-flow')).not.toBeNull();
      expect(getByText('Treatment.pdf')).toBeDefined();
      expect(getByText('shoot_schedule.csv')).toBeDefined();
      expect(getByText('AI run completed')).toBeDefined();
    });
    expect(queryByText('Lineage resources (24h)')).toBeNull();
    expect(queryByText('Space lineage traces (Tempo)')).toBeNull();
  });

  it('draws a sparkline even when Grafana returns a single sample', async () => {
    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue({
      ...grafanaBoard,
      dashboards: [
        {
          uid: 'studiotower-monitor',
          title: 'StudioTower Space Activity',
          url: 'https://loftyladybug3305.grafana.net/d/studiotower-monitor/studiotower-runtime',
          panels: [
            {
              panel_id: 1,
              title: 'Space events per minute',
              type: 'timeseries',
              series: [{ name: 'events', points: [[1, 2]] }],
            },
          ],
        },
      ],
    });
    const { getByText, container } = render(
      <GrafanaCloudBoard spaceId="space_test_1" mode="telemetry" />,
    );
    await waitFor(() => {
      expect(getByText('Space events per minute')).toBeDefined();
      expect(container.querySelector('svg.grafana-sparkline')).not.toBeNull();
      expect(container.querySelector('circle')).not.toBeNull();
    });
  });

  it('does not invent charts when Grafana is unconfigured', async () => {
    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue({
      connected: false,
      error: 'Grafana service account token is not configured',
      dashboards: [],
    });
    const { getByText } = render(<GrafanaCloudBoard spaceId="space_test_1" />);
    await waitFor(() => {
      expect(getByText('Grafana service account token is not configured')).toBeDefined();
    });
  });
});
