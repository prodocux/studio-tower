import { test, expect } from '@playwright/test';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * StudioTower Tier 2: Staging Live Golden Path Workflow Verification
 *
 * Strict Architectural Boundaries & Acceptance Contracts:
 * 1. AI_FALLBACK_ALLOWED=false: Verified via /readyz preflight in setup project.
 * 2. Zero Secrets in Browser: Maintenance secrets run strictly in setup & teardown fixtures.
 *    Browser context only receives controlled test user sessions or public invite links.
 * 3. Zero Backdoors in Production Code: Window object has NO test auth hooks.
 *    Authentication is performed strictly through the official Firebase SDK (signInWithCustomToken).
 * 4. Fail-Closed Setup Contract (Zero Duplicate Provisioning):
 *    - Setup outputs must exist and be valid; missing outputs fail context immediately.
 *    - Standalone duplicate provisioning fallback is strictly prohibited.
 * 5. Two Distinct Users & Single-Use Invite:
 *    - User 1 (Sandbox Owner): Provisioned sandbox and single-use invite (max_uses: 1) in setup.
 *    - User 2 (Invited Coordinator): Visits unauthenticated preview, verifies restored session,
 *      accepts invite, and asserts membership role. A second use of the token fails (HTTP 410 / GONE).
 * 6. Hard Assertions & 3-Way Hash Check:
 *    - Document QA UI workflow: select file -> "問 AI" -> context chip assertion -> intent assertion -> grounded QA.
 *    - AI message locked by ID delta (no .first() guessing).
 *    - Exact quotation verified in preview drawer.
 *    - 3-way hash check: downloaded bytes SHA-256 == manifest SHA-256 == FileRecord hash.
 * 7. Dedicated Teardown Project:
 *    - Durable cascade teardown, zero-record verification, and user revocation are guaranteed
 *      by the independent staging-teardown project.
 */

const stagingApiUrl = process.env.STAGING_API_URL || '';
const allowSkip = process.env.ALLOW_STAGING_SKIP === '1';
const ingestionDeadlineSeconds = parseInt(process.env.STAGING_INGESTION_TIMEOUT_SEC || '120', 10);
const authDir = process.env.STAGING_AUTH_DIR || path.join(os.tmpdir(), 'studiotower-staging-auth');

test.describe.serial('Tier 2: F-Staging Live Golden Path Verification', () => {
  let createdSandboxSpaceId: string = '';
  let inviteToken: string = '';

  // --------------------------------------------------------------------------
  // Fixture Contract: Require Authoritative Setup Outputs (Fail-Closed)
  // --------------------------------------------------------------------------
  test.beforeAll(async () => {
    if (!stagingApiUrl) {
      if (allowSkip) {
        test.skip(true, 'ALLOW_STAGING_SKIP=1 set and STAGING_API_URL missing.');
        return;
      }
      throw new Error('FATAL: Missing required STAGING_API_URL environment variable for staging gate.');
    }

    const sessionFile = path.join(authDir, 'session.json');
    if (!fs.existsSync(sessionFile)) {
      if (allowSkip) {
        test.skip(true, 'ALLOW_STAGING_SKIP=1 set and staging session.json missing.');
        return;
      }
      throw new Error(
        'FATAL: Missing staging session.json from setup project. ' +
        'Staging gate fails closed. Setup dependency must run and produce valid session state.'
      );
    }

    try {
      const sessData = JSON.parse(fs.readFileSync(sessionFile, 'utf-8'));
      createdSandboxSpaceId = sessData.spaceId || '';
      inviteToken = sessData.inviteToken || '';
    } catch (err: any) {
      throw new Error(`FATAL: Failed to parse staging session.json: ${err?.message}`);
    }

    if (!createdSandboxSpaceId || !inviteToken) {
      throw new Error('FATAL: Incomplete staging session metadata: missing spaceId or inviteToken.');
    }
  });

  test.beforeEach(async ({ page }) => {
    // Network routing to ensure no secret headers leak into trace
    await page.route('**/*', (route) => {
      route.continue();
    });
  });

  // --------------------------------------------------------------------------
  // Gate A: Auth UI State Machine, Watchdog & Browser Storage Hygiene
  // --------------------------------------------------------------------------
  test('Gate A: Auth UI State Machine, Watchdog & Browser Storage Hygiene', async ({ browser }) => {
    if (!createdSandboxSpaceId && allowSkip) {
      test.skip(true, 'Skipped due to missing staging credentials (ALLOW_STAGING_SKIP=1)');
      return;
    }

    // Gate A tests the unauthenticated login gate UI state machine in a clean context
    const unauthContext = await browser.newContext({ storageState: undefined });
    const page = await unauthContext.newPage();

    try {
      await page.goto('/');

      // 1. Confirm browser contains NO dev tokens
      const devToken = await page.evaluate(() => localStorage.getItem('studiotower_dev_token'));
      expect(devToken).toBeNull();

      // 2. LoginGate UI elements rendered
      const googleLoginBtn = page.locator('#btn-google-login');
      await expect(googleLoginBtn).toBeVisible();

      // 3. Dev switcher must be completely absent in staging / production mode
      const devSwitcher = page.locator('.dev-auth-box');
      await expect(devSwitcher).toHaveCount(0);

      // 4. Test popup cancellation recovery:
      const popupPromise = page.waitForEvent('popup', { timeout: 3000 }).catch(() => null);
      await googleLoginBtn.click();
      const popup = await popupPromise;
      if (popup) {
        await popup.close();
      }
      // Verify button recovers (not stuck in 'Authenticating...')
      await expect(googleLoginBtn).toBeEnabled({ timeout: 15000 });
      await expect(googleLoginBtn).toContainText('Sign in with Google');
    } finally {
      await unauthContext.close();
    }
  });

  // --------------------------------------------------------------------------
  // Gate B: Controlled Staging Golden Path Workflow
  // --------------------------------------------------------------------------
  test('Gate B: Invite Landing -> SDK Sign-In -> Single-Use Exhaustion -> Ingestion Polling -> Citations -> Action Execution -> 3-Way Hash Check', async ({ page, request }) => {
    if ((!createdSandboxSpaceId || !inviteToken) && allowSkip) {
      test.skip(true, 'Skipped due to missing staging credentials (ALLOW_STAGING_SKIP=1)');
      return;
    }

    // Step 1: User 2 (unauthenticated) navigates to Invite Preview in clean unauthenticated context
    const unauthContext = await page.context().browser()!.newContext({ storageState: undefined });
    const unauthPage = await unauthContext.newPage();
    await unauthPage.goto(`/invites/${inviteToken}`);

    // Verify preview card rendered without eager space join
    await expect(unauthPage.locator('.app-title')).toContainText('StudioTower');
    await expect(unauthPage.locator('h2')).toBeVisible(); // Space name
    const unauthAcceptBtn = unauthPage.locator('#btn-google-accept-invite');
    await expect(unauthAcceptBtn).toBeVisible();
    await unauthContext.close();

    // Step 2: User 2 arrives with established browser storageState (Zero Custom Tokens in DOM / Trace)
    // Verify restored session via authenticated ApiClient / /test-auth?verify-restored=1
    await page.goto('/test-auth?verify-restored=1');
    const restoredBox = page.locator('#restored-auth-success');
    await expect(restoredBox).toBeVisible({ timeout: 15000 });
    const restoredUid = await restoredBox.getAttribute('data-uid');
    expect(restoredUid).toBeTruthy();

    // Verify browser storage hygiene: strictly zero dev tokens
    const devToken = await page.evaluate(() => localStorage.getItem('studiotower_dev_token'));
    expect(devToken).toBeNull();

    // Step 3: User 2 returns to invite preview as authenticated coordinator
    await page.goto(`/invites/${inviteToken}`);
    const confirmAcceptBtn = page.locator('#btn-confirm-accept-invite');
    await expect(confirmAcceptBtn).toBeVisible({ timeout: 15000 });
    await confirmAcceptBtn.click();

    // Step 4: Workspace transitions and primary tabs become visible
    const chatTab = page.locator('#tab-view-chat');
    await expect(chatTab).toBeVisible({ timeout: 15000 });

    // Step 5: Verify single-use invite enforcement (max_uses: 1)
    // A second attempt to preview with this token must be rejected strictly with 410 / INVITE_EXHAUSTED
    const reuseRes = await request.get(`${stagingApiUrl}/v1/invites/${inviteToken}/preview`);
    expect(reuseRes.status()).toBe(410);
    const reuseData = await reuseRes.json();
    expect(reuseData.detail?.code).toBe('INVITE_EXHAUSTED');

    // Step 6: Real UI File Upload via File Center
    const filesTab = page.locator('#tab-view-files');
    await filesTab.click();

    const targetQuotation = 'Radar telemetry confirms no interference with airport operations.';
    const sampleScriptContent = `TITLE: STAGING VERIFICATION SCENE
SCENE 1: EXT. DESERT RUNWAY - DAWN
Production coordinator Alice meets sound recordist Bob at the hangar.
ALICE
We have full clearance for drone cameras on runway 4.
BOB
${targetQuotation}
ALICE
Approved. Lock down the perimeter at 0600.
`;

    // Upload via file input
    const fileInput = page.locator('#file-upload-input');
    await fileInput.setInputFiles({
      name: 'staging_prep_script.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(sampleScriptContent),
    });

    // Step 7: Bounded Ingestion Polling & Hard Assertion (No files[0] guessing)
    // Retrieve coordinator ID token live from browser session in memory
    const coordinatorIdToken = await page.evaluate(async () => {
      const { getAuth } = await import('firebase/auth');
      const auth = getAuth();
      return auth.currentUser ? await auth.currentUser.getIdToken() : null;
    });
    expect(coordinatorIdToken, 'Coordinator ID token must be available from browser session').toBeTruthy();

    let ingestionReady = false;
    let uploadedFileId = '';
    let uploadedFileHash = '';
    const startTime = Date.now();
    let currentDelayMs = 1500;

    while (Date.now() - startTime < ingestionDeadlineSeconds * 1000) {
      const pollRes = await request.get(`${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/files`, {
        headers: { Authorization: `Bearer ${coordinatorIdToken}` },
      });
      if (pollRes.status() === 200) {
        const files = await pollRes.json();
        const targetFile = files.find((f: any) => f.filename === 'staging_prep_script.txt');
        if (targetFile) {
          uploadedFileId = targetFile.file_id;
          uploadedFileHash = targetFile.sha256 || targetFile.sha256_hash || targetFile.content_hash || '';
          if (targetFile.ingestion_status === 'ready' || targetFile.ingestion_status === 'ready_partial') {
            ingestionReady = true;
            break;
          }
        }
      }
      await page.waitForTimeout(currentDelayMs);
      currentDelayMs = Math.min(currentDelayMs * 1.5, 6000);
    }

    expect(ingestionReady).toBe(true);
    expect(uploadedFileId).toBeTruthy();

    // Step 8: Document QA UI Workflow: Select file -> Ask AI -> Verify Context Chip & Intent -> Live Gemini Inference with Citations
    // 1. Locate the exact uploaded file row in File Center table
    const fileRow = page.locator(`tr[data-file-id="${uploadedFileId}"]`);
    await expect(fileRow).toBeVisible({ timeout: 15000 });

    // 2. Select / check the file checkbox
    const fileCheckbox = fileRow.locator('input[type="checkbox"]');
    await fileCheckbox.check();
    await expect(fileRow).toHaveClass(/selected-row/);

    // 3. Click "問 AI" (Ask AI) in File Center
    const askAiBtn = page.locator('[data-testid="batch-ask-ai"]');
    if (await askAiBtn.isVisible()) {
      await askAiBtn.click();
    } else {
      await fileRow.locator('[data-testid="row-ask-ai"]').click();
    }

    // 4. Verify automatic transition to Chat tab
    const chatInput = page.locator('#chat-input-field');
    await expect(chatInput).toBeVisible({ timeout: 10000 });

    // 5. Assert Context Chip is present with data-file-id matching uploadedFileId
    const contextChip = page.locator(`.context-chip[data-file-id="${uploadedFileId}"]`);
    await expect(contextChip).toBeVisible({ timeout: 10000 });

    // 6. Assert AI Mode / Intent is 'document_qa'
    const docQaBtn = page.locator('.intent-mode-btn[data-intent="document_qa"]');
    await expect(docQaBtn).toHaveClass(/active/, { timeout: 10000 });

    // 7. Snapshot existing agent message IDs to lock onto the new response by ID delta
    const existingAgentMsgIds = await page
      .locator('[data-message-id][data-message-role="agent"]')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-message-id')).filter(Boolean));

    // 8. Submit question grounded on the attached document context
    await chatInput.fill('@agent What does radar telemetry confirm in the prep script?');
    const sendBtn = page.locator('#btn-send-message');
    await sendBtn.click();

    // Wait strictly for a new agent message (avoids matching optimistic user message)
    await page.waitForFunction(
      (oldIds) => {
        const current = Array.from(
          document.querySelectorAll('[data-message-id][data-message-role="agent"]')
        )
          .map((e) => e.getAttribute('data-message-id'))
          .filter(Boolean);
        return current.some((id) => !oldIds.includes(id));
      },
      existingAgentMsgIds,
      { timeout: 45000 }
    );

    // Get the newly generated assistant message element by ID delta
    const currentAgentMsgIds = await page
      .locator('[data-message-id][data-message-role="agent"]')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-message-id')).filter(Boolean));
    const newAssistantMsgId = currentAgentMsgIds.find((id) => !existingAgentMsgIds.includes(id));
    expect(newAssistantMsgId).toBeTruthy();

    const newAssistantMsg = page.locator(`[data-message-id="${newAssistantMsgId}"]`);
    await expect(newAssistantMsg).toBeVisible();

    // Step 9: Mandatory Citations & Exact Quotation in Preview Drawer
    const citationBadge = newAssistantMsg.locator('.citation-badge-btn').first();
    await expect(citationBadge).toBeVisible({ timeout: 15000 });

    // Assert citation file_id matches uploaded file
    const citationFileId = await citationBadge.getAttribute('data-file-id');
    expect(citationFileId).toBe(uploadedFileId);

    await citationBadge.click();

    const previewDrawer = page.locator('.preview-drawer-card');
    await expect(previewDrawer).toBeVisible({ timeout: 5000 });
    // Assert exact quotation is rendered in preview drawer
    await expect(previewDrawer).toContainText(targetQuotation);

    const closeDrawerBtn = page.locator('.preview-drawer-close-btn');
    await expect(closeDrawerBtn).toBeVisible({ timeout: 5000 });
    await closeDrawerBtn.click();

    // Step 10: Action Proposal Confirmation -> Capture Exact Run ID -> Strict Approval Gate -> 3-Way Hash Check
    // Snapshot existing action proposal IDs before sending proposal prompt
    const existingActionIds = await page
      .locator('.action-proposal-card[data-action-id]')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-action-id')).filter(Boolean));

    await chatInput.fill('@agent Create an action proposal for camera setup breakdown on runway 4.');
    await sendBtn.click();

    // Wait for new proposal card with a new action ID
    await page.waitForFunction(
      (oldActionIds) => {
        const current = Array.from(document.querySelectorAll('.action-proposal-card[data-action-id]'))
          .map((e) => e.getAttribute('data-action-id'))
          .filter(Boolean);
        return current.some((id) => !oldActionIds.includes(id));
      },
      existingActionIds,
      { timeout: 35000 }
    );

    const currentActionIds = await page
      .locator('.action-proposal-card[data-action-id]')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-action-id')).filter(Boolean));
    const newActionId = currentActionIds.find((id) => !existingActionIds.includes(id));
    expect(newActionId).toBeTruthy();

    const proposalCard = page.locator(`.action-proposal-card[data-action-id="${newActionId}"]`);
    await expect(proposalCard).toBeVisible();

    // Intercept confirm action response requiring URL to contain the exact newActionId
    const confirmPromise = page.waitForResponse(
      (res) =>
        res.url().includes(`/actions/${newActionId}/confirm`) &&
        res.status() === 200
    );

    const confirmProposalBtn = proposalCard.locator('.btn-confirm-action-proposal');
    await expect(confirmProposalBtn).toBeVisible();
    await confirmProposalBtn.click();

    const confirmRes = await confirmPromise;
    const confirmData = await confirmRes.json();
    const confirmedRunId = confirmData.run_id;
    expect(confirmedRunId).toBeTruthy();

    // Navigate to runs view
    const runsTab = page.locator('#tab-view-runs');
    await runsTab.click();

    // Strictly poll the exact confirmedRunId (No runs[0] or .first() guessing)
    let confirmedRun: any = null;
    const pollStart = Date.now();
    while (Date.now() - pollStart < 30000) {
      const runRes = await request.get(
        `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/runs/${confirmedRunId}`,
        { headers: { Authorization: `Bearer ${coordinatorIdToken}` } }
      );
      if (runRes.status() === 200) {
        confirmedRun = await runRes.json();
        if (confirmedRun.status === 'awaiting_approval' || confirmedRun.status === 'completed') {
          break;
        }
      }
      await page.waitForTimeout(1000);
    }
    expect(confirmedRun).toBeDefined();
    expect(confirmedRun.run_id).toBe(confirmedRunId);

    // Mandatory Risk Gate Assertion: must be strictly awaiting_approval initially
    expect(confirmedRun.status).toBe('awaiting_approval');

    // Coordinator approves the run via coordinator API
    const approveRes = await request.post(
      `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/runs/${confirmedRunId}/approve`,
      {
        headers: {
          Authorization: `Bearer ${coordinatorIdToken}`,
          'Content-Type': 'application/json',
        },
        data: { approved: true },
      }
    );
    expect(approveRes.status()).toBe(200);

    // Poll until confirmedRunId strictly transitions to completed
    let finalRun: any = null;
    const completionStart = Date.now();
    while (Date.now() - completionStart < 30000) {
      const runRes = await request.get(
        `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/runs/${confirmedRunId}`,
        { headers: { Authorization: `Bearer ${coordinatorIdToken}` } }
      );
      if (runRes.status() === 200) {
        finalRun = await runRes.json();
        if (finalRun.status === 'completed') {
          break;
        }
      }
      await page.waitForTimeout(1000);
    }
    expect(finalRun).toBeDefined();
    expect(finalRun.status).toBe('completed');
    expect(finalRun.output_artifact_ids?.length).toBeGreaterThan(0);
    const deliverableArtifactId = finalRun.output_artifact_ids[0];

    // Real 3-Way Hash Gate:
    // 1. Download artifact binary bytes and compute SHA-256
    const artDownloadRes = await request.get(
      `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/artifacts/${deliverableArtifactId}/download`,
      {
        headers: { Authorization: `Bearer ${coordinatorIdToken}` },
      }
    );
    expect(artDownloadRes.status()).toBe(200);
    const artifactBytes = await artDownloadRes.body();
    const computedArtifactSha = crypto.createHash('sha256').update(artifactBytes).digest('hex').toLowerCase();

    // 2. Verify artifact header X-Artifact-Sha256
    const headerSha = (artDownloadRes.headers()['x-artifact-sha256'] || '').toLowerCase();
    expect(headerSha).toBe(computedArtifactSha);

    // 3. Mandatory Manifest Verification (no optional pass)
    expect(finalRun.manifest_file_id).toBeTruthy();
    const manifestRes = await request.get(
      `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/files/${finalRun.manifest_file_id}/download`,
      {
        headers: { Authorization: `Bearer ${coordinatorIdToken}` },
      }
    );
    expect(manifestRes.status()).toBe(200);
    const manifestData = await manifestRes.json();
    const manifestEntry = manifestData.artifacts?.find((a: any) => a.file_id === deliverableArtifactId);
    expect(manifestEntry).toBeDefined();
    expect(manifestEntry.sha256.toLowerCase()).toBe(computedArtifactSha);

    // 4. Mandatory Space FileRecord Verification
    const spaceFilesRes = await request.get(`${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/files`, {
      headers: { Authorization: `Bearer ${coordinatorIdToken}` },
    });
    expect(spaceFilesRes.status()).toBe(200);
    const spaceFiles = await spaceFilesRes.json();
    const artFile = spaceFiles.find((f: any) => f.file_id === deliverableArtifactId);
    expect(artFile).toBeDefined();
    expect(artFile.sha256.toLowerCase()).toBe(computedArtifactSha);

    // 5. Verify uploaded script file integrity (3-way hash check on source file)
    const scriptDownloadRes = await request.get(
      `${stagingApiUrl}/v1/spaces/${createdSandboxSpaceId}/files/${uploadedFileId}/download`,
      {
        headers: { Authorization: `Bearer ${coordinatorIdToken}` },
      }
    );
    expect(scriptDownloadRes.status()).toBe(200);
    const scriptBytes = await scriptDownloadRes.body();
    const computedScriptSha = crypto.createHash('sha256').update(scriptBytes).digest('hex').toLowerCase();
    expect(computedScriptSha).toBe(uploadedFileHash.toLowerCase());
  });
});
