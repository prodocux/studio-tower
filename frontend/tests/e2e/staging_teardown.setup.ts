import { test as teardown } from '@playwright/test';
import * as os from 'os';
import * as path from 'path';
import { executeStagingTeardown } from './staging_teardown_core';

/**
 * Tier 2: Staging Global Teardown Project
 *
 * Dedicated teardown project triggered automatically by Playwright after setup & golden path:
 * 1. Reads non-sensitive spaceId from session.json in isolated OS temp authDir.
 * 2. Invokes POST /v1/maintenance/sandboxes/{spaceId}/teardown and hard-asserts HTTP 200.
 * 3. Bounded polling on /v1/maintenance/sandboxes/{spaceId}/cleanup-status until phase is 'completed'.
 * 4. Queries /v1/maintenance/sandboxes/{spaceId}/verify-empty and hard-asserts 0 remaining child records & blobs.
 * 5. In a robust FINALLY block:
 *    - Revokes refresh tokens for staging_test_owner and staging_test_coordinator.
 *    - Durably purges OS temp auth directory.
 * 6. Aggregates all errors and fails closed with exit code 1 if ANY teardown assertion fails,
 *    retaining spaceId for manual operator inspection.
 */

const stagingApiUrl = process.env.STAGING_API_URL || '';
const maintenanceSecret = process.env.STAGING_MAINTENANCE_SECRET || '';
const allowSkip = process.env.ALLOW_STAGING_SKIP === '1';
const authDir = process.env.STAGING_AUTH_DIR || path.join(os.tmpdir(), 'studiotower-staging-auth');

teardown('execute durable cascade teardown, verify clean state, and purge auth', async ({ request }) => {
  await executeStagingTeardown(request, {
    stagingApiUrl,
    maintenanceSecret,
    authDir,
    allowSkip,
    onSkip: (reason) => {
      teardown.skip(true, reason);
    },
  });
});
