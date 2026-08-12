import { expect, test } from '@playwright/test'
test('login screen exposes the fixed account flow', async ({ page }) => { await page.goto('/login'); await expect(page.getByRole('heading', { name: '登录维护中心' })).toBeVisible(); await expect(page.getByRole('button', { name: '安全登录' })).toBeVisible() })

