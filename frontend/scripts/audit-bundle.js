import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const distDir = path.resolve(__dirname, '../dist/assets');

if (!fs.existsSync(distDir)) {
  console.error('❌ Bundle Audit Failed: dist/assets directory not found. Run "vite build" first.');
  process.exit(1);
}

// Helper to load and parse .env files into process.env if not already set
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

// Load in priority order unless explicitly disabled
if (!process.env.IGNORE_ENV_FILES) {
  loadEnvFile('.env');
  loadEnvFile('.env.local');
  loadEnvFile('.env.production');
  loadEnvFile('.env.production.local');
}

// 1. Strict Configuration Validation
const isStrict = process.argv.includes('--strict');
const authMode = process.env.VITE_AUTH_MODE || 'firebase';

if (isStrict && authMode === 'firebase') {
  const requiredKeys = ['VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_PROJECT_ID', 'VITE_FIREBASE_APP_ID'];
  const missing = requiredKeys.filter((k) => !process.env[k]);
  if (missing.length > 0) {
    console.error(`\n❌ [Fail-Closed] Missing required Firebase configuration: ${missing.join(', ')}`);
    process.exit(1);
  }
}

// 2. Bundle Content Token Isolation Audit
const files = fs.readdirSync(distDir).filter((f) => f.endsWith('.js'));
let hasViolation = false;

console.log(`🔍 Auditing ${files.length} production bundle chunks for forbidden tokens...`);

for (const file of files) {
  const filePath = path.join(distDir, file);
  const content = fs.readFileSync(filePath, 'utf-8');

  // Check for forbidden dev auth tokens and test backdoor hooks in production
  const forbiddenKeywords = [
    'dev:',
    'studiotower_dev_token',
    '__studiotower_auth__',
    'setControlledTestSession',
    'controlledTestToken',
    '__studiotower_sign_in_custom_token__',
    'TestAuthPage',
    'staging-custom-token-input',
    'btn-submit-staging-token',
  ];
  for (const kw of forbiddenKeywords) {
    if (content.includes(kw)) {
      console.error(`❌ Security Violation in ${file}: Found forbidden '${kw}' in production build!`);
      hasViolation = true;
    }
  }
}

if (hasViolation) {
  console.error('❌ Release Gate Failed: Production bundle contains dev credentials or dev switcher code.');
  process.exit(1);
} else {
  console.log('✅ Bundle Audit Passed: 0 dev tokens found. Production bundle is cleanly isolated.');
}
