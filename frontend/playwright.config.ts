import { defineConfig } from '@playwright/test';
import { execSync } from 'child_process';
import path from 'path';

function getPythonCommand(): string {
  if (process.env.PYTHON_BIN) return process.env.PYTHON_BIN;
  if (process.env.VIRTUAL_ENV) {
    const isWin = process.platform === 'win32';
    return path.join(process.env.VIRTUAL_ENV, isWin ? 'Scripts' : 'bin', isWin ? 'python.exe' : 'python');
  }

  const localAppData = process.env.LOCALAPPDATA || '';
  const candidates = [
    'python',
    'python3',
    localAppData ? path.join(localAppData, 'Programs', 'Python', 'Python312', 'python.exe') : '',
    localAppData ? path.join(localAppData, 'Programs', 'Python', 'Python311', 'python.exe') : '',
    localAppData ? path.join(localAppData, 'Programs', 'Python', 'Python310', 'python.exe') : '',
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      execSync(`"${candidate}" -c "import uvicorn"`, { stdio: 'ignore' });
      return candidate;
    } catch {
      // Continue to next candidate
    }
  }

  return 'python';
}

const pythonCmd = getPythonCommand();

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60000,
  expect: {
    timeout: 15000,
  },
  workers: 1, // Sequential execution ensures clean transactional DB state
  use: {
    baseURL: 'http://localhost:5173',
    headless: true,
  },
  webServer: [
    {
      command: `"${pythonCmd}" -m uvicorn app.main:app --port 8000`,
      cwd: '../backend',
      url: 'http://127.0.0.1:8000/docs',
      reuseExistingServer: false,
      env: {
        AI_FALLBACK_ALLOWED: 'true',
      },
      timeout: 30000,
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: false,
      env: {
        VITE_AUTH_MODE: 'dev',
      },
      timeout: 30000,
    },
  ],
});
