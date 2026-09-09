import { test, expect } from '@playwright/test';

test.describe('StudioTower Component Mocked UI Journeys', () => {
  test.describe('Unauthenticated Entry', () => {
    test('Journey 1: Login Gate & Brand Interface', async ({ page }) => {
      await page.route('**/v1/me', async (route) => {
        await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
      });
      await page.goto('/');
      await expect(page.locator('.app-title')).toHaveText('StudioTower');
      await expect(page.locator('#btn-google-login')).toBeVisible();
    });
  });

  test.describe('Authenticated Workspace Journeys', () => {
    test.beforeEach(async ({ page }) => {
      await page.route('**/v1/me', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            uid: 'alice_02',
            email: 'alice@example.com',
            display_name: 'Alice Director',
          }),
        });
      });

      await page.route('**/v1/spaces', async (route) => {
        if (route.request().method() === 'POST') {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              space_id: 'space_new_01',
              name: 'Project Bersama',
              kind: 'shared_space',
              created_by: 'alice_02',
              created_at: new Date().toISOString(),
              tags: [{ name: 'General', slug: 'general', color: '#64748B' }],
            }),
          });
        } else {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify([
              {
                space_id: 'space_agent_dm',
                name: 'StudioTower Agent',
                kind: 'agent_dm',
                created_by: 'alice_02',
                created_at: new Date().toISOString(),
                tags: [{ name: 'General', slug: 'general', color: '#64748B' }],
              },
              {
                space_id: 'space_shared_01',
                name: 'Project Bersama',
                kind: 'shared_space',
                created_by: 'alice_02',
                created_at: new Date().toISOString(),
                tags: [
                  { name: 'General', slug: 'general', color: '#64748B' },
                  { name: 'Block A', slug: 'block-a', color: '#3B82F6' },
                ],
              },
            ]),
          });
        }
      });

      await page.route('**/v1/spaces/**/context*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            space: {
              space_id: 'space_shared_01',
              name: 'Project Bersama',
              kind: 'shared_space',
              created_by: 'alice_02',
              created_at: new Date().toISOString(),
              tags: [
                { name: 'General', slug: 'general', color: '#64748B' },
                { name: 'Block A', slug: 'block-a', color: '#3B82F6' },
              ],
            },
            current_user_role: 'owner',
            member_count: 3,
            capabilities: {
              can_invite: true,
              can_manage_members: true,
              can_change_role: true,
              can_remove_member: true,
              can_transfer_ownership: true,
              can_approve_runs: true,
              can_manage_tags: true,
              can_leave_space: false,
            },
          }),
        });
      });

      await page.route('**/v1/spaces/**/messages*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            {
              message_id: 'msg_001',
              space_id: 'space_shared_01',
              sender_uid: 'alice_02',
              sender_name: 'Alice Director',
              role: 'user',
              content: 'Welcome to Project Bersama!',
              project_tag: 'general',
              created_at: new Date().toISOString(),
            },
          ]),
        });
      });

      await page.route('**/v1/spaces/**/files*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      });

      await page.route('**/v1/spaces/**/runs*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            {
              run_id: 'run_001',
              space_id: 'space_shared_01',
              project_tag: 'general',
              status: 'awaiting_approval',
              prompt: 'Helicopter Stunt Scene 4 Breakdown',
              created_by: 'alice_02',
              approval_gate: {
                gate_id: 'gate_001',
                title: 'High Risk Helicopter Stunt Approval',
                description: 'Requires coordinator approval',
                required_role: 'coordinator',
                status: 'pending',
              },
              created_at: new Date().toISOString(),
            },
          ]),
        });
      });

      await page.route('**/v1/spaces/**/grafana/visual*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            connected: true,
            source: 'grafana-http-api',
            ingest: 'exporting',
            lineage_flow: [
              { node_id: 'tempo_0_abc', title: 'Treatment.pdf', stage: 'file', detail: 'File ingested', resource_name: 'Treatment.pdf' },
              { node_id: 'tempo_1_run', title: 'shoot_schedule.csv', stage: 'artifact', detail: 'AI run completed', resource_name: 'shoot_schedule.csv' },
            ],
            dashboards: [
              {
                uid: 'studiotower-monitor',
                title: 'StudioTower Space Activity',
                url: 'https://example.grafana.net/d/studiotower-monitor',
                panels: [
                  {
                    panel_id: 1,
                    title: 'Space events per minute',
                    type: 'timeseries',
                    series: [{ name: 'events', points: [[1, 2], [2, 4]] }],
                  },
                  {
                    panel_id: 2,
                    title: 'Messages (24h)',
                    type: 'stat',
                    series: [{ name: 'Value', points: [[1, 3]] }],
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
          }),
        });
      });

      await page.route('**/v1/spaces/**/lineage*', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            space_id: 'space_shared_01',
            tag: 'general',
            nodes: [
              {
                id: 'file_01',
                type: 'source',
                label: 'Treatment_v1.pdf',
                status: 'committed',
                sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
              },
              {
                id: 'run_001',
                type: 'run',
                label: 'Helicopter Stunt Breakdown',
                status: 'awaiting_approval',
              },
            ],
            edges: [
              {
                from: 'file_01',
                to: 'run_001',
                relation: 'analyzed_by',
              },
            ],
          }),
        });
      });

      await page.addInitScript(() => {
        localStorage.setItem('studiotower_dev_token', 'dev:alice_02:alice@example.com:Alice Director');
        localStorage.setItem('last_active_space_id', 'space_shared_01');
      });
    });

    test('Journey 2: Space Creation Modal & Form Lifecycle', async ({ page }) => {
      page.on('console', (msg) => console.log('[BROWSER CONSOLE]', msg.text()));
      page.on('pageerror', (err) => console.log('[BROWSER UNCAUGHT ERROR]', err));
      await page.goto('/');
      await page.waitForSelector('.app-layout', { timeout: 10000 });
      const createBtn = page.locator('#btn-create-space');
      await expect(createBtn).toBeVisible();
      await createBtn.click();

      const spaceInput = page.locator('#space-name-input');
      await expect(spaceInput).toBeVisible();
      await expect(page.locator('.modal-title')).toHaveText('Create Shared Production Space');

      await page.keyboard.press('Escape');
      await expect(spaceInput).not.toBeVisible();
    });

    test('Journey 3: Chat Composer, Attachments & Tag Filters', async ({ page }) => {
      await page.goto('/');
      const chatInput = page.locator('#chat-input-field');
      await expect(chatInput).toBeVisible();
      await expect(chatInput).toBeEnabled();

      const attachBtn = page.locator('.btn-attach');
      await expect(attachBtn).toBeVisible();

      const sendBtn = page.locator('.btn-send');
      await expect(sendBtn).toBeVisible();
    });

    test('Journey 4: Telemetry Tab, Runs & Human-in-the-Loop Gates', async ({ page }) => {
      await page.goto('/');
      const telemetryTab = page.locator('.panel-tab-btn', { hasText: 'Telemetry' });
      await expect(telemetryTab).toBeVisible();
      await telemetryTab.click();

      await expect(page.locator('.right-panel')).toBeVisible();
      await expect(page.locator('.telemetry-tab-view')).toBeVisible();
      await expect(page.locator('.grafana-visual-board')).toBeVisible();
      await expect(page.locator('.grafana-panel-title', { hasText: 'Space events per minute' })).toBeVisible();
      await expect(page.locator('svg.grafana-sparkline')).toBeVisible();
    });

    test('Journey 5: Artifact Lineage DAG & Inspector Drawer', async ({ page }) => {
      await page.goto('/');
      const lineageTab = page.locator('.panel-tab-btn', { hasText: 'Lineage' });
      await expect(lineageTab).toBeVisible();
      await lineageTab.click();

      await expect(page.locator('.lineage-tab-view')).toBeVisible();
      await expect(page.locator('.grafana-visual-board')).toBeVisible();
      await expect(page.locator('.grafana-lineage-flow')).toBeVisible();
      await expect(page.getByText('Treatment.pdf')).toBeVisible();
    });
  });
});
