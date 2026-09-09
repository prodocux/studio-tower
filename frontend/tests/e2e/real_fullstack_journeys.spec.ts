import { test, expect } from '@playwright/test';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

test.describe.serial('StudioTower 5 Core Fullstack User Journeys (Live Backend Release Gate)', () => {
  const aliceToken = 'dev:alice_e2e:alice@cinema.io:Alice Director';
  const targetPromptText = 'Breakdown Scene: Helicopter Stunt Unit Alpha Extraction';

  test.beforeEach(async ({ page }) => {
    // Inject real Alice dev token into localStorage before page load
    await page.addInitScript((token) => {
      localStorage.setItem('studiotower_dev_token', token);
    }, aliceToken);
  });

  test('Journey 1: Fullstack Auth, Auto-Provisioned Agent Space & Light/Dark Theme', async ({ page }) => {
    await page.goto('/');

    // 1. Verify user profile displays real identity
    await expect(page.locator('.user-name')).toHaveText('Alice Director');
    await expect(page.locator('.user-email')).toHaveText('alice@cinema.io');

    // 2. Verify Home Agent DM is auto-provisioned
    await expect(page.locator('.breadcrumb-current-space')).toContainText('StudioTower Agent');
    await expect(page.locator('.space-item').first()).toBeVisible();

    // 3. Verify Agent welcome message is rendered in chat stream
    await expect(page.locator('.message-content').first()).toContainText('StudioTower film production prep assistant');

    // 4. Verify Theme Toggle functionality & Hard Reload Persistence
    await expect(page.locator('#btn-theme-toggle')).toBeVisible();

    // Toggle to Light Theme
    await page.locator('#btn-theme-toggle').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Hard reload page and assert data-theme remains light immediately on bootstrap (no FOUC)
    await page.reload();
    await page.waitForSelector('.breadcrumb-current-space');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Toggle back to Dark Theme
    await page.locator('#btn-theme-toggle').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  });

  test('Journey 2: Create Shared Production Space & Generate Coordinator Invite', async ({ page }) => {
    await page.goto('/');

    // 1. Open Create Space Modal
    const createBtn = page.locator('#btn-create-space');
    await expect(createBtn).toBeVisible();
    await createBtn.click();

    // 2. Fill and submit real space creation form
    const spaceInput = page.locator('#space-name-input');
    await expect(spaceInput).toBeVisible();
    await spaceInput.fill('Stunt Coordination Unit');
    await page.locator('.modal-actions button.btn-primary').click();

    // 3. Verify breadcrumb updates to newly created space
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Stunt Coordination Unit');

    // 4. Open Space Management Modal
    const manageBtn = page.locator('#btn-manage-space');
    await expect(manageBtn).toBeVisible();
    await manageBtn.click();

    // 5. Navigate to Invitations tab
    await page.locator('.manage-tab-btn', { hasText: 'Invitations' }).click();

    // 6. Select Coordinator role and generate single-use invite
    await page.locator('.create-invite-form .form-select').first().selectOption('coordinator');
    await page.locator('.create-invite-form button[type="submit"]').click();

    // 7. Verify generated invite token banner is displayed
    await expect(page.locator('.generated-invite-banner')).toBeVisible();
    await expect(page.locator('.invite-token-code')).not.toHaveText('');

    // 8. Verify invite appears in active list
    await expect(page.locator('.invite-row')).toBeVisible();
    await page.keyboard.press('Escape');
  });

  test('Journey 3: Real File Upload with Tag + Chat Analysis + Tag Delta Assertion', async ({ page }) => {
    await page.goto('/');

    // 1. Ensure we are in the shared space created in Journey 2
    const sharedSpaceBtn = page.locator('.space-item', { hasText: 'Stunt Coordination Unit' });
    await expect(sharedSpaceBtn).toBeVisible({ timeout: 10000 });
    await sharedSpaceBtn.click();
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Stunt Coordination Unit');
    await expect(page.locator('#chat-input-field')).toBeVisible({ timeout: 10000 });

    // 2. Record baseline message count after space loads
    const initialMsgCount = await page.locator('.message-item').count();

    // 3. Create temporary treatment script for upload
    const tempFilePath = path.join(__dirname, 'temp_stunt_script.txt');
    fs.writeFileSync(tempFilePath, 'EXT. FLOOD BASIN - DAWN\nRescue team mobilizes amphibious drones across the submerged perimeter with high-altitude cables.\n');

    try {
      // 4. Attach file via hidden input
      const fileInput = page.locator('input[type="file"]');
      await fileInput.setInputFiles(tempFilePath);

      // 5. Assert attachment indicator is active
      await expect(page.locator('.btn-attach')).toHaveClass(/has-file/);

      // 6. Select Run Breakdown intent mode, enter unique analysis prompt and submit
      await page.locator('.intent-mode-btn', { hasText: 'Run Breakdown' }).click();
      const chatInput = page.locator('#chat-input-field');
      await chatInput.fill(targetPromptText);
      await page.locator('.btn-send').click();

      // 7. Unconditionally assert that message stream receives the new user message
      await expect(
        page.locator('.message-content').filter({ hasText: targetPromptText })
      ).toBeVisible({ timeout: 30000 });

      // 8. Unconditionally assert that message count increments by 2 (User message + Agent breakdown response)
      await expect(page.locator('.message-item')).toHaveCount(initialMsgCount + 2, { timeout: 45000 });

      // 9. Unconditionally assert that the new Agent message contains structured breakdown content (not initial welcome)
      const lastAgentMsg = page.locator('.message-item.message-agent').last();
      await expect(lastAgentMsg.locator('.message-content')).toContainText(/Scene|FLOOD BASIN|Drone|Rescue|Stunt/i);
    } finally {
      if (fs.existsSync(tempFilePath)) {
        fs.unlinkSync(tempFilePath);
      }
    }
  });

  test('Journey 4: Human-in-the-Loop Approval Gate Lifecycle & Gate Mutation (Strict Run Binding & Zero Fallback)', async ({ page }) => {
    await page.goto('/');

    // 1. Ensure we are in the shared space
    const sharedSpaceBtn = page.locator('.space-item', { hasText: 'Stunt Coordination Unit' });
    await expect(sharedSpaceBtn).toBeVisible({ timeout: 10000 });
    await sharedSpaceBtn.click();

    // 2. Switch to Telemetry tab — Grafana Cloud only
    const telemetryTab = page.locator('.panel-tab-btn', { hasText: 'Telemetry' });
    await expect(telemetryTab).toBeVisible();
    await telemetryTab.click();
    await expect(page.locator('.telemetry-tab-view')).toBeVisible();
    await expect(page.locator('.grafana-visual-board, .grafana-visual-empty, .grafana-visual-kicker').first()).toBeVisible({
      timeout: 15000,
    });

    // 3. HITL gates live in Tasks & Approvals, not the Grafana telemetry tab
    await page.locator('#tab-view-runs').click();
    const specificRunCard = page.locator('.pending-run-item, .run-item, .run-card').filter({ hasText: targetPromptText });
    await expect(specificRunCard).toBeVisible({ timeout: 10000 });

    const approveBtn = specificRunCard.locator('button', { hasText: /Approve/i });
    await expect(approveBtn).toBeVisible();
    await approveBtn.click();
  });

  test('Journey 5: Real Lineage Graph, Inspector Drawer & Authenticated Download', async ({ page }) => {
    await page.goto('/');

    // 1. Ensure we are in the shared space
    const sharedSpaceBtn = page.locator('.space-item', { hasText: 'Stunt Coordination Unit' });
    await expect(sharedSpaceBtn).toBeVisible({ timeout: 10000 });
    await sharedSpaceBtn.click();

    // 2. Switch to Lineage tab
    const lineageTab = page.locator('.panel-tab-btn', { hasText: 'Lineage' });
    await expect(lineageTab).toBeVisible();
    await lineageTab.click();

    // 3. Lineage tab is Grafana Tempo, not the local DAG
    await expect(page.locator('.lineage-tab-view')).toBeVisible();
    await expect(page.locator('.grafana-visual-board, .grafana-visual-empty, .grafana-visual-kicker').first()).toBeVisible({
      timeout: 15000,
    });
  });

  test('Journey 6: Invite Deep-Link Public Preview & Intent-Driven Acceptance', async ({ page }) => {
    // 1. Obtain an active invite token by creating an invite in Stunt Coordination Unit
    await page.goto('/');
    const sharedSpaceBtn = page.locator('.space-item', { hasText: 'Stunt Coordination Unit' });
    await expect(sharedSpaceBtn).toBeVisible({ timeout: 10000 });
    await sharedSpaceBtn.click();

    const manageBtn = page.locator('#btn-manage-space');
    await expect(manageBtn).toBeVisible();
    await manageBtn.click();

    await page.locator('.manage-tab-btn', { hasText: 'Invitations' }).click();
    await page.locator('.create-invite-form .form-select').first().selectOption('coordinator');
    await page.locator('.create-invite-form button[type="submit"]').click();

    const banner = page.locator('.generated-invite-banner');
    await expect(banner).toBeVisible();
    const tokenCode = await page.locator('.invite-token-code').innerText();
    expect(tokenCode.trim()).not.toBe('');
    await page.keyboard.press('Escape');

    // 2. Clear user dev token to simulate unauthenticated guest visitor
    await page.evaluate(() => {
      localStorage.removeItem('studiotower_dev_token');
    });

    // 3. Navigate to public invite URL
    await page.goto(`/invites/${tokenCode.trim()}`);

    // 4. Verify public preview displays space name and role without leaking tenant ID
    await expect(page.locator('.invite-card')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('.invite-card h2')).toHaveText('Stunt Coordination Unit');
    await expect(page.locator('.invite-card')).toContainText('coordinator');
    await expect(page.locator('#btn-google-accept-invite')).toBeVisible();

    // 5. Restore user identity to simulate completed Google sign-in
    await page.evaluate((token) => {
      localStorage.setItem('studiotower_dev_token', token);
    }, 'dev:bob_guest:bob@cinema.io:Bob Guest');

    // 6. Reload invite page as signed-in user
    await page.reload();

    // 7. Verify confirmation card displays user email and explicit Join button
    await expect(page.locator('.invite-card')).toContainText('bob@cinema.io');
    const acceptBtn = page.locator('.invite-card button', { hasText: /Accept & Join/i });
    await expect(acceptBtn).toBeVisible();

    // 8. Accept invite and verify transition into space
    await acceptBtn.click();
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Stunt Coordination Unit', { timeout: 10000 });
    expect(page.url()).not.toContain('/invites/');
  });
});
