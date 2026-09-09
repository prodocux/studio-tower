import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { executeStagingTeardown, TeardownContext } from './e2e/staging_teardown_core';

describe('Staging Teardown Gate Fault-Injection Contract', () => {
  let tempAuthDir: string;

  beforeEach(() => {
    tempAuthDir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-teardown-auth-'));
  });

  afterEach(() => {
    if (fs.existsSync(tempAuthDir)) {
      fs.rmSync(tempAuthDir, { recursive: true, force: true });
    }
  });

  it('guarantees token revocation and authDir purge even when sandbox teardown fails with HTTP 500', async () => {
    // 1. Write minimal cleanup manifest
    fs.writeFileSync(
      path.join(tempAuthDir, 'session.json'),
      JSON.stringify({ spaceId: 'sp_fault_injection_500' })
    );

    const revokedIdentities: string[] = [];

    // 2. Mock request context where sandbox teardown returns 500
    const mockRequest: TeardownContext = {
      post: vi.fn().mockImplementation(async (url: string, opts?: any) => {
        if (url.includes('/maintenance/sandboxes/sp_fault_injection_500/teardown')) {
          return {
            status: () => 500,
            text: async () => 'Internal Server Error during cascade delete',
          };
        }
        if (url.includes('/maintenance/test-auth/revoke-test-user')) {
          revokedIdentities.push(opts?.data?.identity);
          return {
            status: () => 200,
            text: async () => JSON.stringify({ status: 'revoked' }),
          };
        }
        return { status: () => 404, text: async () => 'not found' };
      }),
      get: vi.fn().mockImplementation(async () => {
        return { status: () => 200, text: async () => '{}', json: async () => ({}) };
      }),
    };

    // 3. Execute teardown and assert it throws a fatal error (fails closed)
    await expect(
      executeStagingTeardown(mockRequest, {
        stagingApiUrl: 'https://staging-api.studiotower.app',
        maintenanceSecret: 'test-maint-secret',
        authDir: tempAuthDir,
      })
    ).rejects.toThrow(/STAGING TEARDOWN GATE FAILURE/);

    // 4. Assert both identities had their refresh tokens revoked in finally block
    expect(revokedIdentities).toContain('staging_test_owner');
    expect(revokedIdentities).toContain('staging_test_coordinator');
    expect(revokedIdentities.length).toBe(2);

    // 5. Assert OS temp auth directory was completely purged
    expect(fs.existsSync(tempAuthDir)).toBe(false);
  });

  it('guarantees token revocation and authDir purge when verify-empty fails (lingering records)', async () => {
    fs.writeFileSync(
      path.join(tempAuthDir, 'session.json'),
      JSON.stringify({ spaceId: 'sp_fault_injection_not_empty' })
    );

    const revokedIdentities: string[] = [];

    const mockRequest: TeardownContext = {
      post: vi.fn().mockImplementation(async (url: string, opts?: any) => {
        if (url.includes('/maintenance/sandboxes/sp_fault_injection_not_empty/teardown')) {
          return {
            status: () => 200,
            text: async () => JSON.stringify({ status: 'teardown_complete' }),
          };
        }
        if (url.includes('/maintenance/test-auth/revoke-test-user')) {
          revokedIdentities.push(opts?.data?.identity);
          return {
            status: () => 200,
            text: async () => JSON.stringify({ status: 'revoked' }),
          };
        }
        return { status: () => 404, text: async () => 'not found' };
      }),
      get: vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/cleanup-status')) {
          return {
            status: () => 200,
            text: async () => JSON.stringify({ phase: 'completed' }),
            json: async () => ({ phase: 'completed' }),
          };
        }
        if (url.includes('/verify-empty')) {
          return {
            status: () => 200,
            text: async () => JSON.stringify({ empty: false, remaining_counts: { messages: 1 } }),
            json: async () => ({ empty: false, remaining_counts: { messages: 1 } }),
          };
        }
        return { status: () => 404, text: async () => 'not found', json: async () => ({}) };
      }),
    };

    await expect(
      executeStagingTeardown(mockRequest, {
        stagingApiUrl: 'https://staging-api.studiotower.app',
        maintenanceSecret: 'test-maint-secret',
        authDir: tempAuthDir,
        maxPollMs: 1000,
        pollIntervalMs: 100,
      })
    ).rejects.toThrow(/Space sp_fault_injection_not_empty is not empty/);

    expect(revokedIdentities).toEqual(['staging_test_owner', 'staging_test_coordinator']);
    expect(fs.existsSync(tempAuthDir)).toBe(false);
  });

  it('guarantees token revocation and authDir purge when session.json is corrupted', async () => {
    // Write invalid/corrupted JSON
    fs.writeFileSync(path.join(tempAuthDir, 'session.json'), '{ corrupted json');

    const revokedIdentities: string[] = [];

    const mockRequest: TeardownContext = {
      post: vi.fn().mockImplementation(async (url: string, opts?: any) => {
        if (url.includes('/maintenance/test-auth/revoke-test-user')) {
          revokedIdentities.push(opts?.data?.identity);
          return {
            status: () => 200,
            text: async () => JSON.stringify({ status: 'revoked' }),
          };
        }
        return { status: () => 404, text: async () => 'not found' };
      }),
      get: vi.fn().mockImplementation(async () => {
        return { status: () => 404, text: async () => 'not found', json: async () => ({}) };
      }),
    };

    await expect(
      executeStagingTeardown(mockRequest, {
        stagingApiUrl: 'https://staging-api.studiotower.app',
        maintenanceSecret: 'test-maint-secret',
        authDir: tempAuthDir,
      })
    ).rejects.toThrow(/Failed to read staging session\.json/);

    expect(revokedIdentities).toEqual(['staging_test_owner', 'staging_test_coordinator']);
    expect(fs.existsSync(tempAuthDir)).toBe(false);
  });

  it('guarantees authDir purge and fails closed even when maintenance secret is completely missing', async () => {
    // Put dummy files in authDir to represent sensitive local auth state (e.g. storageState, tokens)
    fs.writeFileSync(path.join(tempAuthDir, 'coordinator.json'), JSON.stringify({ cookies: ['secret'] }));
    fs.writeFileSync(path.join(tempAuthDir, 'session.json'), JSON.stringify({ spaceId: 'sp_secret_missing' }));

    const mockRequest: TeardownContext = {
      post: vi.fn().mockImplementation(async () => ({ status: () => 200, text: async () => '{}' })),
      get: vi.fn().mockImplementation(async () => ({ status: () => 200, text: async () => '{}', json: async () => ({}) })),
    };

    await expect(
      executeStagingTeardown(mockRequest, {
        stagingApiUrl: 'https://staging-api.studiotower.app',
        maintenanceSecret: '', // Missing secret
        authDir: tempAuthDir,
        allowSkip: false,
      })
    ).rejects.toThrow(/FATAL: Missing STAGING_MAINTENANCE_SECRET/);

    // Assert that despite missing secret, the local OS temp auth directory was destroyed unconditionally
    expect(fs.existsSync(tempAuthDir)).toBe(false);
  });
});

