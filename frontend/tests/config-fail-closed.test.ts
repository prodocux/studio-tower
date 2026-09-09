import { describe, it, expect } from 'vitest';
import { execSync } from 'child_process';
import path from 'path';
import fs from 'fs';

describe('Production Firebase Config Fail-Closed Release Gate', () => {
  it('validate-config.js fails with code 1 when required Firebase keys are missing', () => {
    const scriptPath = path.resolve(__dirname, '../scripts/validate-config.js');
    let failed = false;
    let errorMessage = '';

    try {
      execSync(`node "${scriptPath}"`, {
        env: {
          ...process.env,
          IGNORE_ENV_FILES: 'true',
          VITE_AUTH_MODE: 'firebase',
          VITE_FIREBASE_API_KEY: '',
          VITE_FIREBASE_PROJECT_ID: '',
          VITE_FIREBASE_APP_ID: '',
        },
        stdio: 'pipe',
      });
    } catch (err: any) {
      failed = true;
      errorMessage = err.stderr?.toString() || err.message;
    }

    expect(failed).toBe(true);
    expect(errorMessage).toContain('Fail-Closed Release Gate');
    expect(errorMessage).toContain('VITE_FIREBASE_API_KEY');
  });

  it('validate-config.js succeeds when all required Firebase keys are provided', () => {
    const scriptPath = path.resolve(__dirname, '../scripts/validate-config.js');
    const output = execSync(`node "${scriptPath}"`, {
      env: {
        ...process.env,
        VITE_AUTH_MODE: 'firebase',
        VITE_FIREBASE_API_KEY: 'AIzaSyFakeKeyForTestVerification',
        VITE_FIREBASE_PROJECT_ID: 'agentic-cinema-demo-2026',
        VITE_FIREBASE_APP_ID: '1:195557569214:web:fakeapp',
      },
      stdio: 'pipe',
    }).toString();

    expect(output).toContain('Configuration Validation Passed');
  });

  it('verifies zero backdoor hooks are mounted on window in the browser environment', () => {
    expect((window as any).__studiotower_auth__).toBeUndefined();
    expect((window as any).__studiotower_api__).toBeUndefined();
    expect((window as any).__studiotower_sign_in_custom_token__).toBeUndefined();
  });

  it('rejects bare build command to enforce explicit deployment mode targeting', () => {
    const manifest = JSON.parse(
      fs.readFileSync(path.resolve(__dirname, '../package.json'), 'utf-8'),
    );
    expect(manifest.scripts.build).toContain('Bare build command disallowed');
    expect(manifest.scripts.build).toContain('process.exit(1)');
  });

  it('verifies production build output completely excludes TestAuthPage chunk and forbidden tokens', () => {
    const assetsDir = path.resolve(__dirname, '../dist/assets');
    const files = fs.readdirSync(assetsDir);
    expect(files.some((f) => f.includes('TestAuthPage'))).toBe(false);
  });

  it('verifies validate-staging-config rejects production Firebase project ID in staging mode', () => {
    let failed = false;
    let errorMessage = '';
    try {
      execSync('node scripts/validate-staging-config.js', {
        cwd: path.resolve(__dirname, '..'),
        env: {
          ...process.env,
          IGNORE_ENV_FILES: '1',
          VITE_AUTH_MODE: 'firebase',
          VITE_FIREBASE_API_KEY: 'AIzaSyFakeKey',
          VITE_FIREBASE_PROJECT_ID: 'agentic-cinema-demo-2026', // Production project ID!
          VITE_FIREBASE_APP_ID: '1:fake:web:fake',
        },
        stdio: 'pipe',
      });
    } catch (err: any) {
      failed = true;
      errorMessage = err.stderr?.toString() || err.stdout?.toString() || err.message;
    }
    expect(failed).toBe(true);
    expect(errorMessage).toContain('STRICTLY PROHIBITED');
  });

  it('verifies validate-staging-config rejects production API host in staging mode', () => {
    let failed = false;
    let errorMessage = '';
    try {
      execSync('node scripts/validate-staging-config.js', {
        cwd: path.resolve(__dirname, '..'),
        env: {
          ...process.env,
          IGNORE_ENV_FILES: '1',
          VITE_AUTH_MODE: 'firebase',
          VITE_FIREBASE_API_KEY: 'AIzaSyFakeKey',
          VITE_FIREBASE_PROJECT_ID: 'studiotower-staging',
          VITE_FIREBASE_APP_ID: '1:fake:web:fake',
          VITE_API_URL: 'https://api.studiotower.app', // Production API!
        },
        stdio: 'pipe',
      });
    } catch (err: any) {
      failed = true;
      errorMessage = err.stderr?.toString() || err.stdout?.toString() || err.message;
    }
    expect(failed).toBe(true);
    expect(errorMessage).toContain('Production API host is strictly prohibited');
  });
});

