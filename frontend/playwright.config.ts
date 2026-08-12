import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/e2e',
  reporter: 'line',
  timeout: 15_000,
  use: { baseURL: 'http://127.0.0.1:5173', browserName: 'chromium', channel: 'chrome', headless: true },
  webServer: { command: 'npm run dev', port: 5173, reuseExistingServer: true, timeout: 30_000 },
})
