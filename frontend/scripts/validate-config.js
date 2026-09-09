// Fail-Closed Production Configuration Validator
import fs from 'node:fs';
import path from 'node:path';

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

// Load in priority order unless explicitly disabled for isolated test runs
if (!process.env.IGNORE_ENV_FILES) {
  loadEnvFile('.env');
  loadEnvFile('.env.local');
  loadEnvFile('.env.production');
  loadEnvFile('.env.production.local');
}

const authMode = process.env.VITE_AUTH_MODE || 'firebase';

if (authMode === 'firebase') {
  const requiredKeys = ['VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_PROJECT_ID', 'VITE_FIREBASE_APP_ID'];
  const missing = requiredKeys.filter((k) => !process.env[k]);

  if (missing.length > 0) {
    console.error(`\n❌ [Fail-Closed Release Gate] Production build aborted due to missing Firebase configuration:`);
    console.error(`   Missing required environment variables: ${missing.join(', ')}`);
    console.error(`\n💡 How to resolve:`);
    console.error(`   Option A (Recommended): Create 'frontend/.env.production' file with:`);
    console.error(`      VITE_FIREBASE_API_KEY=AIzaSy... (from Firebase Console -> Project Settings -> General -> Web App)`);
    console.error(`      VITE_FIREBASE_PROJECT_ID=${process.env.VITE_FIREBASE_PROJECT_ID || 'agentic-cinema-demo-2026'}`);
    console.error(`      VITE_FIREBASE_APP_ID=1:... (from Firebase Console)`);
    console.error(`   Option B: Pass parameters to deploy script:`);
    console.error(`      .\\scripts\\deploy.ps1 -FirebaseApiKey "AIzaSy..." -FirebaseAppId "1:..." -ProjectId "agentic-cinema-demo-2026"\n`);
    process.exit(1);
  }
}

console.log('✅ Configuration Validation Passed: Required Firebase environment variables are present.');
