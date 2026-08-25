import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: 'http://127.0.0.1:18080',
    trace: 'on-first-retry',
    launchOptions: { args: ['--no-proxy-server'] },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'docker compose up --build',
    url: 'http://127.0.0.1:18080/api/health/live',
    timeout: 240_000,
    reuseExistingServer: !process.env.CI,
  },
})
