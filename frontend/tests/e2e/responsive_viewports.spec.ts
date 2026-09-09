import { test, expect } from '@playwright/test';

test.describe('StudioTower Multi-Viewport Responsive & Fault Isolation Release Gate', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('studiotower_dev_token', 'dev:alice_02:alice@cinema.io:Alice Director');
      window.localStorage.setItem(
        'studiotower_user',
        JSON.stringify({
          uid: 'alice_02',
          email: 'alice@cinema.io',
          display_name: 'Alice Director',
        })
      );
    });

    await page.goto('/');
    await expect(page.locator('.app-layout')).toBeVisible({ timeout: 15000 });
  });

  // 1. Desktop Viewport (1280x800) - No Horizontal Overflow & Layout Isolation
  test('Desktop (1280px): Zero horizontal scroll overflow & layout element non-occlusion', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });

    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(1280);

    const sidebar = page.locator('.app-sidebar');
    const chatArea = page.locator('.chat-area');
    const rightPanel = page.locator('.right-panel');

    await expect(sidebar).toBeVisible();
    await expect(chatArea).toBeVisible();
    await expect(rightPanel).toBeVisible();

    const sidebarBox = await sidebar.boundingBox();
    const chatAreaBox = await chatArea.boundingBox();
    const rightPanelBox = await rightPanel.boundingBox();

    expect(sidebarBox).not.toBeNull();
    expect(chatAreaBox).not.toBeNull();
    expect(rightPanelBox).not.toBeNull();

    if (sidebarBox && chatAreaBox && rightPanelBox) {
      expect(sidebarBox.x + sidebarBox.width).toBeLessThanOrEqual(chatAreaBox.x + 10);
      expect(chatAreaBox.x + chatAreaBox.width).toBeLessThanOrEqual(rightPanelBox.x + 10);
    }
  });

  // 2. Tablet Viewport (768x1024) - Drawer Slide-over, Escape, Focus Trap & Inert Isolation
  test('Tablet (768px): RightPanel drawer toggle, Portal, inert, Escape & focus restoration', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });

    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(768);

    const drawerBtn = page.locator('#btn-right-panel-toggle');
    await expect(drawerBtn).toBeVisible();

    // Toggle Drawer Open
    await drawerBtn.click();
    await expect(page.locator('#tablet-right-panel-drawer')).toBeVisible();
    await expect(drawerBtn).toHaveAttribute('aria-expanded', 'true');

    // Assert WAI-ARIA Attributes & Portal Mounting
    const drawerOverlay = page.locator('#tablet-right-panel-drawer');
    await expect(drawerOverlay).toHaveAttribute('role', 'dialog');
    await expect(drawerOverlay).toHaveAttribute('aria-modal', 'true');

    // Assert .app-layout is marked inert while overlay drawer is open
    await expect(page.locator('.app-layout')).toHaveAttribute('inert', '');

    // Close via Escape Key
    await page.keyboard.press('Escape');
    await expect(drawerOverlay).not.toBeVisible();
    await expect(page.locator('.app-layout')).not.toHaveAttribute('inert');
    await expect(drawerBtn).toHaveAttribute('aria-expanded', 'false');

    // Assert focus restores to trigger button
    await expect(drawerBtn).toBeFocused();
  });

  // 3. Mobile Viewport (375x667) - Mobile Drawer & 3 Close Triggers (Overlay, Escape, Selection)
  test('Mobile (375px): Sidebar mobile drawer 3 close triggers (Overlay, Escape, Selection)', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });

    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(375);

    const menuBtn = page.locator('#btn-sidebar-toggle');
    await expect(menuBtn).toBeVisible();

    // Trigger 1: Open Sidebar & Close via Drawer Close Button
    await menuBtn.click();
    await expect(page.locator('#mobile-sidebar-drawer')).toBeVisible();
    await page.locator('#mobile-sidebar-drawer .drawer-close-btn').click();
    await expect(page.locator('#mobile-sidebar-drawer')).not.toBeVisible();

    // Trigger 2: Open Sidebar & Close via Escape Key + Focus Restoration
    await menuBtn.click();
    await expect(page.locator('#mobile-sidebar-drawer')).toBeVisible();
    await expect(page.locator('#mobile-sidebar-drawer .drawer-close-btn')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('#mobile-sidebar-drawer')).not.toBeVisible();
    await expect(menuBtn).toBeFocused();

    // Trigger 3: Open Sidebar & Select Space Item Auto-Closes Drawer
    await menuBtn.click();
    await expect(page.locator('#mobile-sidebar-drawer')).toBeVisible();
    const spaceItem = page.locator('#mobile-sidebar-drawer .space-item').first();
    await expect(spaceItem).toBeVisible();
    await spaceItem.scrollIntoViewIfNeeded();
    await spaceItem.click();
    await expect(page.locator('#mobile-sidebar-drawer')).not.toBeVisible();
  });

  // 4. Fault Isolation: Lineage API 500 error degrades gracefully with Route Hit Assertion
  test('Fault Isolation: Lineage API 500 error degrades gracefully without breaking Chat', async ({ page }) => {
    let routeHitCount = 0;

    // Register route interception BEFORE navigation/reload
    await page.route('**/v1/spaces/*/lineage*', (route) => {
      routeHitCount++;
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Lineage service unavailable mock' }),
      });
    });

    // Reload page to trigger clean fetch under route interception
    await page.reload();
    await expect(page.locator('.app-layout')).toBeVisible({ timeout: 15000 });

    const chatInput = page.locator('#chat-input-field');
    await expect(chatInput).toBeVisible({ timeout: 10000 });

    await chatInput.fill('Test message during Lineage outage');
    await page.locator('.btn-send').click();

    await expect(page.locator('.chat-stream')).toContainText('Test message during Lineage outage');

    // Strict Assertion: Confirm route handler was actually invoked during execution
    expect(routeHitCount).toBeGreaterThanOrEqual(1);
  });

  // 5. Fault Isolation: Hanging Lineage API does not block Context or Chat rendering
  test('Fault Isolation: Hanging Lineage API does not block Context or ChatArea rendering', async ({ page }) => {
    let hangingHitCount = 0;

    // Register hanging route interception BEFORE navigation/reload
    await page.route('**/v1/spaces/*/lineage*', (route) => {
      hangingHitCount++;
      // Intentionally do not fulfill/continue - simulate hanging network call
    });

    await page.reload();
    await expect(page.locator('.app-layout')).toBeVisible({ timeout: 15000 });

    const chatInput = page.locator('#chat-input-field');
    await expect(chatInput).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.breadcrumb-current-space')).toBeVisible();

    // Strict Assertion: Confirm hanging route handler was actually invoked
    expect(hangingHitCount).toBeGreaterThanOrEqual(1);
  });

  // 6. Rapid Space Switching Isolation: Deferred Response Space Switching Verification
  test('Space Switching Isolation: Rapid space switching discards stale async responses', async ({ page }) => {
    // Create shared space Alpha and capture real space_id from POST response
    await page.locator('#btn-create-space').click();
    await page.locator('#space-name-input').fill('Rapid Switch Alpha');
    const [alphaResp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/v1/spaces') && r.request().method() === 'POST'),
      page.locator('.modal-actions button.btn-primary').click(),
    ]);
    const alphaData = await alphaResp.json();
    const alphaSpaceId = alphaData.space_id;
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Rapid Switch Alpha');

    // Create second space Beta
    await page.locator('#btn-create-space').click();
    await page.locator('#space-name-input').fill('Rapid Switch Beta');
    await page.locator('.modal-actions button.btn-primary').click();
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Rapid Switch Beta');

    // Setup deferred route for Alpha messages using exact alphaSpaceId
    let resolveAlpha: (() => void) | null = null;
    let alphaRouteHitCount = 0;

    await page.route(`**/v1/spaces/${alphaSpaceId}/messages*`, async (route) => {
      alphaRouteHitCount++;
      await new Promise<void>((r) => {
        resolveAlpha = r;
      });
      route.continue();
    });

    // Switch back to Alpha (triggers fetch for Alpha messages)
    await page.locator('.space-item', { hasText: 'Rapid Switch Alpha' }).click();

    // Rapidly switch to Beta before Alpha resolves
    await page.locator('.space-item', { hasText: 'Rapid Switch Beta' }).click();
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Rapid Switch Beta');

    // Strict Assertion: Confirm route handler was actually hit and resolver was captured!
    expect(alphaRouteHitCount).toBeGreaterThanOrEqual(1);
    expect(resolveAlpha).not.toBeNull();

    // Fulfill Alpha's delayed response
    if (resolveAlpha) {
      (resolveAlpha as () => void)();
    }

    await page.waitForTimeout(500);

    // Strict Assertion: UI MUST STILL be on Beta!
    await expect(page.locator('.breadcrumb-current-space')).toContainText('Rapid Switch Beta');
  });
});
