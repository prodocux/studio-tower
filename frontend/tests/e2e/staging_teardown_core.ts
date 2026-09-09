import * as fs from 'fs';
import * as path from 'path';

export interface TeardownContext {
  post: (
    url: string,
    options?: any
  ) => Promise<{ status: () => number; text: () => Promise<string>; json?: () => Promise<any> }>;
  get: (
    url: string,
    options?: any
  ) => Promise<{ status: () => number; text: () => Promise<string>; json: () => Promise<any> }>;
}

export interface TeardownOptions {
  stagingApiUrl: string;
  maintenanceSecret: string;
  authDir: string;
  allowSkip?: boolean;
  pollIntervalMs?: number;
  maxPollMs?: number;
  onSkip?: (reason: string) => void;
}

export async function executeStagingTeardown(
  request: TeardownContext,
  opts: TeardownOptions
): Promise<{ spaceId: string; status: 'completed' | 'skipped' }> {
  const {
    stagingApiUrl,
    maintenanceSecret,
    authDir,
    allowSkip = false,
    pollIntervalMs = 1000,
    maxPollMs = 30000,
    onSkip,
  } = opts;

  let teardownError: any = null;
  const teardownIssues: string[] = [];
  let spaceId = '';

  try {
    // 1. Validate required environment configuration
    if (!stagingApiUrl) {
      if (allowSkip) {
        onSkip?.('ALLOW_STAGING_SKIP=1 set and STAGING_API_URL missing.');
        return { spaceId: '', status: 'skipped' };
      }
      throw new Error('FATAL: Missing required STAGING_API_URL during teardown. Pipeline fails closed.');
    }

    if (!maintenanceSecret) {
      if (allowSkip) {
        onSkip?.('ALLOW_STAGING_SKIP=1 set and STAGING_MAINTENANCE_SECRET missing.');
        return { spaceId: '', status: 'skipped' };
      }
      throw new Error('FATAL: Missing STAGING_MAINTENANCE_SECRET during teardown. Pipeline fails closed.');
    }

    // 2. Read session manifest
    const sessionFile = path.join(authDir, 'session.json');
    if (fs.existsSync(sessionFile)) {
      try {
        const sess = JSON.parse(fs.readFileSync(sessionFile, 'utf-8'));
        spaceId = sess.spaceId || '';
      } catch (e: any) {
        throw new Error(`FATAL: Failed to read staging session.json: ${e?.message}`);
      }
    }

    if (!spaceId) {
      if (allowSkip) {
        onSkip?.('No spaceId found to teardown, and ALLOW_STAGING_SKIP=1.');
        return { spaceId: '', status: 'skipped' };
      }
      throw new Error('FATAL: Missing spaceId in staging session.json for teardown.');
    }

    console.log(`[Staging Teardown] Beginning durable cascade teardown for sandbox space: ${spaceId}`);

    // 3. Durably initiate cascade teardown
    const tdRes = await request.post(
      `${stagingApiUrl}/v1/maintenance/sandboxes/${spaceId}/teardown`,
      {
        headers: { 'X-StudioTower-Maintenance-Secret': maintenanceSecret },
      }
    );
    if (tdRes.status() !== 200) {
      throw new Error(
        `Teardown initiation failed for space ${spaceId}: HTTP ${tdRes.status()} - ${await tdRes.text()}`
      );
    }

    // 4. Poll cleanup job until phase is strictly 'completed'
    let cleanupCompleted = false;
    const pollStart = Date.now();
    while (Date.now() - pollStart < maxPollMs) {
      const statusRes = await request.get(
        `${stagingApiUrl}/v1/maintenance/sandboxes/${spaceId}/cleanup-status`,
        {
          headers: { 'X-StudioTower-Maintenance-Secret': maintenanceSecret },
        }
      );
      if (statusRes.status() === 200) {
        const data = await statusRes.json();
        if (data.phase === 'completed') {
          cleanupCompleted = true;
          break;
        }
      }
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
    if (!cleanupCompleted) {
      throw new Error(
        `[HARD GATE FAILURE] Sandbox cleanup job did not reach 'completed' phase for space ${spaceId}`
      );
    }

    // 5. Management verification: verify space document, child collections, and blobs are zero
    const verifyRes = await request.get(
      `${stagingApiUrl}/v1/maintenance/sandboxes/${spaceId}/verify-empty`,
      {
        headers: { 'X-StudioTower-Maintenance-Secret': maintenanceSecret },
      }
    );
    if (verifyRes.status() !== 200) {
      throw new Error(
        `[HARD GATE FAILURE] verify-empty check failed for space ${spaceId}: HTTP ${verifyRes.status()} - ${await verifyRes.text()}`
      );
    }
    const verifyData = await verifyRes.json();
    if (!verifyData.empty) {
      throw new Error(`[HARD GATE FAILURE] Space ${spaceId} is not empty: ${JSON.stringify(verifyData)}`);
    }
    console.log(`[Staging Teardown] Cascade cleanup verified empty for sandbox ${spaceId}.`);
  } catch (err: any) {
    teardownError = err;
    if (spaceId) {
      console.error(`\n🚨 [CRITICAL TEARDOWN FAILURE] Sandbox space_id: ${spaceId} requires manual remediation!`);
    }
    console.error(`Error details: ${err?.message || err}\n`);
  } finally {
    // 6. Guaranteed credential revocation (if maintenanceSecret and stagingApiUrl are available)
    if (maintenanceSecret && stagingApiUrl) {
      for (const identity of ['staging_test_owner', 'staging_test_coordinator']) {
        try {
          const revRes = await request.post(
            `${stagingApiUrl}/v1/maintenance/test-auth/revoke-test-user`,
            {
              headers: {
                'X-StudioTower-Maintenance-Secret': maintenanceSecret,
                'Content-Type': 'application/json',
              },
              data: { identity },
            }
          );
          if (revRes.status() !== 200) {
            teardownIssues.push(
              `Token revocation failed for identity ${identity}: HTTP ${revRes.status()} - ${await revRes.text()}`
            );
          } else {
            console.log(`[Staging Teardown] Revoked refresh tokens for ${identity}.`);
          }
        } catch (revErr: any) {
          teardownIssues.push(`Token revocation request exception for ${identity}: ${revErr?.message}`);
        }
      }
    }

    // 7. UNCONDITIONAL auth directory purge from OS temp in ALL execution branches
    // Even if maintenanceSecret was missing or invalid, local auth state MUST be purged
    try {
      if (fs.existsSync(authDir)) {
        fs.rmSync(authDir, { recursive: true, force: true });
      }
      if (fs.existsSync(authDir)) {
        teardownIssues.push(`Failed to remove auth directory: ${authDir}`);
      } else {
        console.log(`[Staging Teardown] Auth state completely destroyed from ${authDir}.`);
      }
    } catch (rmErr: any) {
      teardownIssues.push(`Exception removing auth directory ${authDir}: ${rmErr?.message}`);
    }
  }

  // If teardown was skipped via Playwright's test.skip() mechanism, rethrow the skip error
  if (
    teardownError &&
    (teardownError?.message?.includes('Test is skipped') ||
      teardownError?.name === 'SkipError' ||
      (allowSkip && teardownError?.message?.includes('ALLOW_STAGING_SKIP=1')))
  ) {
    throw teardownError;
  }

  // Fail closed if session parsing, sandbox cleanup, token revocation, or auth purging encountered any failure
  if (teardownError || teardownIssues.length > 0) {
    const errorMessages = [
      teardownError ? `Teardown Execution Failure: ${teardownError.message || teardownError}` : null,
      teardownIssues.length > 0 ? `Security Cleanup Failures: ${teardownIssues.join(' | ')}` : null,
      spaceId ? `Affected Sandbox Space ID: ${spaceId}` : 'Sandbox Space ID: not provisioned or unknown',
    ].filter(Boolean).join('\n');
    throw new Error(`\n=== STAGING TEARDOWN GATE FAILURE ===\n${errorMessages}\n====================================`);
  }

  return { spaceId, status: 'completed' };
}
