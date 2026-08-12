import { expect, test, type Route } from '@playwright/test'

const now = '2026-08-11T08:00:00Z'
const node = {
  id: 'demo-node', hostname: 'demo-node', management_ip: '192.0.2.10',
  roles: ['compute'], online_status: 'ONLINE', enabled: true, last_check_time: now,
}
const pkg = {
  id: 'package-1', name: 'e2e-package', revision: 1, component: 'compute',
  bug_id: 'BUG-1', target_roles: ['compute'], updated_at: now,
}
const task = {
  id: 'task-1', package_name: 'e2e-package', package_revision: 1, status: 'SUCCESS',
  target_node_count: 1, success_count: 1, failed_count: 0, created_at: now,
  nodes: [{
    id: 'task-node-1', hostname: 'demo-node', status: 'SUCCESS', failure_reason: null,
    attempts: [{
      id: 'attempt-1', attempt_no: 1, status: 'SUCCESS',
      steps: [{ id: 'step-1', sequence: 1, name: 'fix', type: 'shell', status: 'SUCCESS', exit_code: 0 }],
    }],
  }],
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

test('operator completes login, node intake, package upload, task creation and result review', async ({ page }) => {
  let pending = true
  let uploaded = false
  let loginCount = 0
  let meCount = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/login' && method === 'POST') { loginCount += 1; return json(route, { username: 'admin', csrf_token: 'csrf-e2e' }) }
    if (path === '/auth/me') { meCount += 1; return json(route, { username: 'admin' }) }
    if (path === '/dashboard/summary') return json(route, { nodes: { total: 1, online: 1, offline: 0 }, packages: uploaded ? 1 : 0, tasks: {} })
    if (path === '/dashboard/recent-tasks') return json(route, [])
    if (path === '/nodes/pending' && method === 'GET') return json(route, pending ? [{ id: 'demo-minion' }] : [])
    if (path === '/nodes/pending/demo-minion/accept') { pending = false; return json(route, node) }
    if (path === '/nodes') return json(route, [node])
    if (path === '/packages' && method === 'GET') return json(route, uploaded ? [pkg] : [])
    if (path === '/packages' && method === 'POST') { uploaded = true; return json(route, pkg, 201) }
    if (path === '/tasks/preview') return json(route, { package: pkg, nodes: [node], warnings: [] })
    if (path === '/tasks' && method === 'POST') return json(route, task, 201)
    if (path === '/tasks' && method === 'GET') return json(route, [task])
    if (path === '/tasks/task-1') return json(route, task)
    if (path === '/settings') return json(route, { salt_api_url: 'http://127.0.0.1:8000', salt_api_username: 'automation-center', salt_api_credential: '********', salt_request_timeout: 30, default_step_timeout: 1800, execution_log_retention_days: 7, node_status_check_interval: 60, max_upload_size: 10737418240 })
    if (path.startsWith('/audit-logs')) return json(route, [])
    return json(route, { detail: `unmocked ${method} ${path}` }, 404)
  })

  await page.goto('/login')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill('correct-password')
  await page.getByRole('button', { name: '安全登录' }).click()
  await expect(page.getByRole('heading', { name: '运行总览' })).toBeVisible()
  expect(loginCount).toBe(1)

  await page.getByRole('link', { name: '节点中心' }).click()
  await page.getByRole('button', { name: '接受' }).click()
  await expect(page.getByText('demo-node', { exact: true }).first()).toBeVisible()

  await page.getByRole('link', { name: '维护包中心' }).click()
  await page.locator('input[type=file]').first().setInputFiles({ name: 'e2e-package.tar.gz', mimeType: 'application/gzip', buffer: Buffer.from('fixture') })
  await page.getByRole('button', { name: '上传并校验' }).click()
  await expect(page.getByText('e2e-package', { exact: true })).toBeVisible()

  await page.getByRole('link', { name: '创建任务' }).click()
  await page.getByText('选择当前 Revision', { exact: true }).click()
  await page.getByText('e2e-package · v1', { exact: true }).click()
  await page.getByText('compute', { exact: true }).click()
  await page.getByRole('button', { name: '生成确认快照' }).click()
  await expect(page.getByText('demo-node', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '立即执行' }).click()

  await expect(page.getByRole('heading', { name: '任务详情' })).toBeVisible()
  await expect(page.getByText('task-1', { exact: true })).toBeVisible()
  await expect(page.getByText('SUCCESS', { exact: true }).first()).toBeVisible()

  // 刷新后首次恢复 Session，此后所有导航都应复用 Pinia 状态，不重复请求 auth/me。
  await page.reload()
  await expect(page.getByRole('heading', { name: '任务详情' })).toBeVisible()
  await page.getByRole('link', { name: '运行总览' }).click(); await expect(page.getByRole('heading', { name: '运行总览' })).toBeVisible()
  await page.getByRole('link', { name: '任务中心' }).click(); await expect(page.getByRole('heading', { name: '任务中心' })).toBeVisible()
  await page.getByRole('link', { name: '系统设置' }).click(); await expect(page.getByRole('heading', { name: '系统设置' })).toBeVisible()
  await page.getByRole('link', { name: '操作审计' }).click(); await expect(page.getByRole('heading', { name: '操作审计' })).toBeVisible()
  expect(meCount).toBe(1)
})
