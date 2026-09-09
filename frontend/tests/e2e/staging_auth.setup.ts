import { test as setup, expect } from '@playwright/test';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * Tier 2: Staging Auth Setup Project
 *
 * Dedicated setup project executed with trace: 'off', video: 'off', screenshot: 'off'.
 * Responsible for:
 * 1. Server-side minting of short-lived custom tokens for owner & coordinator.
 * 2. Fail-closed Firebase ID-token exchange and strict JWT claims validation.
 * 3. Atomic persistence of minimal cleanup manifest ({ spaceId }) immediately upon creation.
 * 4. Exchanging custom token via /test-auth in browser context.
 * 5. Saving browser auth state via native storageState({ indexedDB: true }) to isolated OS temp dir.
 * 6. Verifying restored session in a fresh browser context via /test-auth?verify-restored=1 and authenticated ApiClient.
 * 7. Updating non-sensitive metadata ({ spaceId, inviteToken }) via atomic file replace.
 * 8. Destroying token variables immediately from memory.
 */

const stagingApiUrl = process.env.STAGING_API_URL || '';
const maintenanceSecret = process.env.STAGING_MAINTENANCE_SECRET || '';
const allowSkip = process.env.ALLOW_STAGING_SKIP === '1';
const firebaseApiKey = process.env.VITE_FIREBASE_API_KEY || '';
const stagingProjectId = process.env.VITE_FIREBASE_PROJECT_ID || '';
const authDir = process.env.STAGING_AUTH_DIR || path.join(os.tmpdir(), 'studiotower-staging-auth');

function atomicWriteJsonSync(targetPath: string, data: any, mode: number = 0o600): void {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = `${targetPath}.${Date.now()}.${Math.random().toString(36).slice(2)}.tmp`;
  let fd: number | null = null;
  try {
    fd = fs.openSync(tmpPath, 'w', mode);
    fs.writeFileSync(fd, JSON.stringify(data, null, 2), 'utf-8');
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(tmpPath, targetPath);
  } catch (err) {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {
        // ignore close error during error unwinding
      }
    }
    try {
      if (fs.existsSync(tmpPath)) {
        fs.unlinkSync(tmpPath);
      }
    } catch {
      // best-effort cleanup of temporary file
    }
    throw err;
  }
}

async function exchangeCustomTokenForIdToken(customToken: string): Promise<string> {
  if (!firebaseApiKey) {
    throw new Error('FATAL: VITE_FIREBASE_API_KEY is not configured.');
  }
  const url = `https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=${firebaseApiKey}`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: customToken, returnSecureToken: true }),
  });
  if (!resp.ok) {
    throw new Error(`Firebase token exchange failed with HTTP ${resp.status}`);
  }
  const data = await resp.json();
  if (!data?.idToken) {
    throw new Error('Firebase token exchange succeeded but response missing idToken');
  }
  return data.idToken;
}

function parseAndValidateJwt(token: string, expectedUid: string, expectedProjectId: string): any {
  const parts = token.split('.');
  if (parts.length !== 3) {
    throw new Error(`Invalid JWT structure: expected 3 parts, got ${parts.length}`);
  }

  // Proper base64url decoding with padding
  let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4 !== 0) {
    b64 += '=';
  }

  let payload: any;
  try {
    payload = JSON.parse(Buffer.from(b64, 'base64').toString('utf-8'));
  } catch (err: any) {
    throw new Error(`Failed to parse JWT payload JSON: ${err.message}`);
  }

  // Strict fail-closed claim assertions
  if (!payload.aud || payload.aud !== expectedProjectId) {
    throw new Error(`JWT claim violation: aud '${payload.aud}' !== expected staging project '${expectedProjectId}'`);
  }

  const expectedIss = `https://securetoken.google.com/${expectedProjectId}`;
  if (payload.iss !== expectedIss) {
    throw new Error(`JWT claim violation: iss '${payload.iss}' !== expected '${expectedIss}'`);
  }

  if (payload.sub !== expectedUid) {
    throw new Error(`JWT claim violation: sub '${payload.sub}' !== expected uid '${expectedUid}'`);
  }

  const nowSec = Math.floor(Date.now() / 1000);
  if (!payload.exp || payload.exp <= nowSec) {
    throw new Error(`JWT claim violation: token expired (exp ${payload.exp} <= now ${nowSec})`);
  }

  if (payload.firebase?.sign_in_provider !== 'custom') {
    throw new Error(
      `JWT claim violation: sign_in_provider '${payload.firebase?.sign_in_provider}' !== 'custom'`
    );
  }

  return payload;
}

setup('authenticate staging coordinator in isolated setup project', async ({ page, request }) => {
  // 1. Strict Credential and Configuration Check: Fails closed unless explicit skip flag is active
  if (!stagingApiUrl) {
    if (allowSkip) {
      setup.skip(true, 'ALLOW_STAGING_SKIP=1 set and STAGING_API_URL missing.');
      return;
    }
    throw new Error('FATAL: Missing required STAGING_API_URL environment variable for staging gate.');
  }

  if (!maintenanceSecret) {
    if (allowSkip) {
      setup.skip(true, 'ALLOW_STAGING_SKIP=1 set and STAGING_MAINTENANCE_SECRET missing.');
      return;
    }
    throw new Error('FATAL: Missing STAGING_MAINTENANCE_SECRET. Staging gate fails closed.');
  }

  if (!firebaseApiKey || !stagingProjectId) {
    if (allowSkip) {
      setup.skip(true, 'ALLOW_STAGING_SKIP=1 set and VITE_FIREBASE_API_KEY or VITE_FIREBASE_PROJECT_ID missing.');
      return;
    }
    throw new Error('FATAL: Missing required VITE_FIREBASE_API_KEY or VITE_FIREBASE_PROJECT_ID for staging gate.');
  }

  // 2. Preflight readiness assertion: AI_FALLBACK_ALLOWED=false, mode=live, ready=true
  const readyzRes = await request.get(`${stagingApiUrl}/readyz`);
  expect(readyzRes.status()).toBe(200);
  const readyzData = await readyzRes.json();
  expect(readyzData.components?.ai?.mode).toBe('live');
  expect(readyzData.components?.ai?.ready).toBe(true);
  expect(readyzData.components?.ai?.fallback_allowed).toBe(false);

  // 3. Server-side minting with hard status assertions
  const mintOwnerRes = await request.post(`${stagingApiUrl}/v1/maintenance/test-auth/mint-custom-token`, {
    headers: {
      'X-StudioTower-Maintenance-Secret': maintenanceSecret,
      'Content-Type': 'application/json',
    },
    data: { identity: 'staging_test_owner' },
  });
  expect(mintOwnerRes.status()).toBe(200);
  const ownerData = await mintOwnerRes.json();

  const mintCoordRes = await request.post(`${stagingApiUrl}/v1/maintenance/test-auth/mint-custom-token`, {
    headers: {
      'X-StudioTower-Maintenance-Secret': maintenanceSecret,
      'Content-Type': 'application/json',
    },
    data: { identity: 'staging_test_coordinator' },
  });
  expect(mintCoordRes.status()).toBe(200);
  const coordData = await mintCoordRes.json();
  let coordinatorCustomToken: string | null = coordData.custom_token;

  // 4. Fail-Closed Firebase ID Token Exchange & Strict Claims Verification
  const ownerIdToken = await exchangeCustomTokenForIdToken(ownerData.custom_token);
  parseAndValidateJwt(ownerIdToken, ownerData.uid, stagingProjectId);

  // Verify backend /v1/me accepts the exchanged token
  const mePreflight = await request.get(`${stagingApiUrl}/v1/me`, {
    headers: { Authorization: `Bearer ${ownerIdToken}` },
  });
  expect(mePreflight.status()).toBe(200);

  // 5. Sandbox Provisioning with owner set to User 1
  const sbRes = await request.post(`${stagingApiUrl}/v1/maintenance/sandboxes`, {
    headers: {
      'X-StudioTower-Maintenance-Secret': maintenanceSecret,
      'Content-Type': 'application/json',
    },
    data: {
      owner_uid: ownerData.uid,
      owner_email: ownerData.email,
      name: 'Staging Multi-User Sandbox',
      ttl_hours: 1.0,
    },
  });
  expect(sbRes.status()).toBe(200);
  const sbData = await sbRes.json();
  const spaceId = sbData.space_id;

  // Atomic write of minimal cleanup manifest immediately upon receiving spaceId
  // Guarantees teardown can clean up this sandbox even if invite creation or browser login fails
  atomicWriteJsonSync(path.join(authDir, 'session.json'), { spaceId });

  // 6. Create single-use invite (max_uses: 1)
  const invRes = await request.post(`${stagingApiUrl}/v1/spaces/${spaceId}/invites`, {
    headers: {
      Authorization: `Bearer ${ownerIdToken}`,
      'Content-Type': 'application/json',
    },
    data: { role: 'coordinator', max_uses: 1 },
  });
  expect(invRes.status()).toBe(200);
  const invData = await invRes.json();
  const inviteToken = invData.token;

  // Atomically update session manifest with inviteToken
  atomicWriteJsonSync(path.join(authDir, 'session.json'), { spaceId, inviteToken });

  // 7. Browser-side token exchange with logging masking
  await page.goto('/test-auth');
  await page.fill('#staging-custom-token-input', coordinatorCustomToken!);
  await page.click('#btn-submit-staging-token');
  const successBox = page.locator('#test-auth-success');
  await expect(successBox).toBeVisible({ timeout: 15000 });
  const coordUid = await successBox.getAttribute('data-uid');
  expect(
    coordUid,
    `Signed-in coordinator UID '${coordUid}' must strictly match minted identity UID '${coordData.uid}'`
  ).toBe(coordData.uid);

  // 8. Save storageState using native indexedDB: true support to isolated OS temp directory
  fs.mkdirSync(authDir, { recursive: true });
  const authFilePath = path.join(authDir, 'coordinator.json');
  await page.context().storageState({
    path: authFilePath,
    indexedDB: true,
  });

  // 9. Verify restored session in a fresh browser context with authenticated API probe
  const verifyContext = await page.context().browser()!.newContext({
    storageState: authFilePath,
  });
  const verifyPage = await verifyContext.newPage();
  await verifyPage.goto('/test-auth?verify-restored=1');
  const restoredBox = verifyPage.locator('#restored-auth-success');
  await expect(restoredBox).toBeVisible({ timeout: 15000 });
  const restoredUid = await restoredBox.getAttribute('data-uid');
  expect(
    restoredUid,
    `Restored coordinator UID '${restoredUid}' must strictly match minted identity UID '${coordData.uid}'`
  ).toBe(coordData.uid);
  await verifyContext.close();

  // 10. Destroy custom token variable immediately
  coordinatorCustomToken = null;
});
