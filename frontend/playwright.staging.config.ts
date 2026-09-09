import { defineConfig } from '@playwright/test';
import * as os from 'os';
import * as path from 'path';

const stagingUrl = process.env.STAGING_APP_URL || 'https://staging.studiotower.app';

// Isolated auth directory in OS temp directory (never committed, never uploaded as CI artifact)
const authDir = process.env.STAGING_AUTH_DIR || path.join(os.tmpdir(), 'studiotower-staging-auth');
const authStorageFile = path.join(authDir, 'coordinator.json');

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 180000, // 3 minutes total budget for live cloud services
  expect: {
    timeout: 30000,
  },
  workers: 1,
  projects: [
    {
      name: 'staging-auth-setup',
      testMatch: /staging_auth\.setup\.ts/,
      teardown: 'staging-teardown',
      use: {
        baseURL: stagingUrl,
        headless: true,
        trace: 'off',
        video: 'off',
        screenshot: 'off',
      },
    },
    {
      name: 'staging-golden-path',
      testMatch: /staging_golden_path\.spec\.ts/,
      dependencies: ['staging-auth-setup'],
      use: {
        baseURL: stagingUrl,
        headless: true,
        storageState: authStorageFile, // Unconditionally configured; setup dependency guarantees creation
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
      },
    },
    {
      name: 'staging-teardown',
      testMatch: /staging_teardown\.setup\.ts/,
      use: {
        baseURL: stagingUrl,
        headless: true,
        trace: 'off',
        video: 'off',
        screenshot: 'off',
      },
    },
  ],
  // Strictly zero local webServer blocks - connects exclusively to live deployed staging
});
