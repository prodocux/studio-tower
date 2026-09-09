// Fail-Closed Staging Bundle Auditor
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const distDir = path.resolve(__dirname, '../dist');
const assetsDir = path.resolve(distDir, 'assets');

if (!fs.existsSync(assetsDir)) {
  console.error('❌ Staging Bundle Audit Failed: dist/assets directory not found. Run "vite build --mode staging" first.');
  process.exit(1);
}

let hasViolation = false;

// 1. Verify /test-auth chunk exists
const files = fs.readdirSync(assetsDir);
const jsFiles = files.filter((f) => f.endsWith('.js'));
const testAuthChunk = jsFiles.find((f) => f.includes('TestAuthPage') || f.startsWith('TestAuthPage'));

if (!testAuthChunk) {
  // Check inside JS files if TestAuthPage code is bundled
  const hasTestAuthCode = jsFiles.some((f) => {
    const content = fs.readFileSync(path.join(assetsDir, f), 'utf-8');
    return content.includes('staging-custom-token-input') || content.includes('btn-submit-staging-token');
  });
  if (!hasTestAuthCode) {
    console.error('❌ [Staging Gate Violation] /test-auth chunk (TestAuthPage) was NOT emitted in staging build!');
    hasViolation = true;
  } else {
    console.log('✅ Verified /test-auth components are present in staging bundle.');
  }
} else {
  console.log(`✅ Verified /test-auth chunk exists: ${testAuthChunk}`);
}

// 2. Production global backdoor hooks MUST NOT exist
const forbiddenHooks = [
  '__studiotower_auth__',
  'window.__studiotower_auth',
  '__studiotower_sign_in_custom_token__',
  'setControlledTestSession',
];

for (const f of jsFiles) {
  const content = fs.readFileSync(path.join(assetsDir, f), 'utf-8');
  for (const hook of forbiddenHooks) {
    if (content.includes(hook)) {
      console.error(`❌ Security Violation in ${f}: Found forbidden production hook '${hook}'!`);
      hasViolation = true;
    }
  }

  // 3. Dev tokens MUST NOT exist
  if (content.includes('studiotower_dev_token') || content.includes('dev:owner')) {
    console.error(`❌ Security Violation in ${f}: Found forbidden dev token string!`);
    hasViolation = true;
  }
}

// 4. Source maps audit: ensure no tokens or secrets are in maps
function scanMaps(dir) {
  if (!fs.existsSync(dir)) return;
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      scanMaps(full);
    } else if (ent.name.endsWith('.map')) {
      const content = fs.readFileSync(full, 'utf-8');
      if (content.includes('STAGING_MAINTENANCE_SECRET') || content.includes('refresh_token=')) {
        console.error(`❌ Security Violation in sourcemap ${ent.name}: Detected sensitive secret in map!`);
        hasViolation = true;
      }
    }
  }
}
scanMaps(distDir);

if (hasViolation) {
  console.error('\n❌ [Staging Release Gate] Staging bundle failed security and isolation audit.');
  process.exit(1);
} else {
  console.log('✅ Staging Bundle Audit Passed: /test-auth present, 0 backdoor hooks, 0 dev tokens, source maps clean.');
}
