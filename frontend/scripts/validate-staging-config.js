// Fail-Closed Staging Configuration Validator
import fs from 'node:fs';
import path from 'node:path';

function loadEnvFile(filename) {
  const filepath = path.resolve(process.cwd(), filename);
  if (!fs.existsSync(filepath)) return;
  const content = fs.readFileSync(filepath, 'utf-8');
  content.split('\n').forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return;
    const match = trimmed.match(/^([^=]+)=(.*)$/);
    if (match) {
      const key = match[1].trim();
      let value = match[2].trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (!process.env[key]) {
        process.env[key] = value;
      }
    }
  });
}

if (!process.env.IGNORE_ENV_FILES) {
  loadEnvFile('.env');
  loadEnvFile('.env.local');
  loadEnvFile('.env.staging');
  loadEnvFile('.env.staging.local');
}

const authMode = process.env.VITE_AUTH_MODE || 'firebase';

if (authMode === 'firebase') {
  const requiredKeys = ['VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_PROJECT_ID', 'VITE_FIREBASE_APP_ID'];
  const missing = requiredKeys.filter((k) => !process.env[k]);
  if (missing.length > 0) {
    console.error(`\n❌ [Staging Release Gate] Staging build aborted due to missing Firebase configuration:`);
    console.error(`   Missing required environment variables: ${missing.join(', ')}`);
    process.exit(1);
  }

  // 1. Staging Firebase Project ID check: MUST NOT be production project ID
  const prodProjectId = 'agentic-cinema-demo-2026';
  const stagingProjectId = process.env.VITE_FIREBASE_PROJECT_ID;
  if (stagingProjectId === prodProjectId) {
    console.error(`\n❌ [Staging Security Gate] Production Firebase project ID ('${prodProjectId}') is STRICTLY PROHIBITED in staging build!`);
    console.error(`   Provide a dedicated staging Firebase project ID (e.g. 'studiotower-staging').`);
    process.exit(1);
  }

  // 2. Staging API URL check: MUST NOT be production host
  const apiUrl = process.env.VITE_API_URL || process.env.STAGING_API_URL || '';
  if (apiUrl && (apiUrl.includes('api.studiotower.app') && !apiUrl.includes('staging-api.studiotower.app'))) {
    console.error(`\n❌ [Staging Security Gate] Production API host is strictly prohibited in staging build!`);
    console.error(`   API URL is configured as: ${apiUrl}`);
    process.exit(1);
  }

  // 3. Hosting deployment target check
  const hostingTarget = process.env.FIREBASE_HOSTING_TARGET || '';
  if (hostingTarget === 'studiotower-prod' || hostingTarget === prodProjectId) {
    console.error(`\n❌ [Staging Security Gate] Production hosting target ('${hostingTarget}') cannot be used in staging build!`);
    process.exit(1);
  }
}

console.log('✅ Staging Configuration Validation Passed: Staging Firebase and API endpoints confirmed.');
