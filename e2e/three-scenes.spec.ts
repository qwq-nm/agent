import { expect, test } from '@playwright/test'

const cases = [
  {
    name: '日志响应',
    goal: '分析示例日志中的攻击行为',
    scene: '日志应急响应',
    fixture: 'demo_cases/logs/access_attack.log',
    expected: 'WEB-SCAN-002',
  },
  {
    name: '源码审计',
    goal: '静态审计示例源码',
    scene: '静态源码审计',
    fixture: 'demo_cases/source_audit/vulnerable_app.zip',
    expected: 'PY-CMD-001',
  },
  {
    name: 'Web 分析',
    goal: '被动分析授权测试站点',
    scene: '被动 Web 分析',
    url: 'http://web-demo/',
    expected: 'action=/search',
  },
]

for (const item of cases) {
  test(`${item.name} produces a traceable report`, async ({ page }) => {
    await page.goto('/tasks/new')
    await page.getByLabel('任务目标').fill(item.goal)
    await page.getByLabel('授权范围').fill('仅限内置 Demo 材料和 web-demo')
    await page.getByLabel('场景', { exact: true }).selectOption({ label: item.scene })
    if (item.fixture) {
      await page.getByLabel('上传材料').setInputFiles(item.fixture)
    }
    if (item.url) await page.getByLabel('授权 URL（Web 场景）').fill(item.url)
    await page.getByRole('button', { name: '创建任务' }).click()
    await page.getByRole('button', { name: '开始执行' }).click()
    if (item.name === 'Web 分析') {
      const dialog = page.getByRole('dialog')
      await expect(dialog.getByRole('heading', { name: '确认中风险工具调用' })).toBeVisible()
      await expect(dialog.getByText('http_fetch', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: '批准并继续' }).click()
    }
    await expect(page.getByText('completed', { exact: true })).toBeVisible({
      timeout: 120_000,
    })
    await page.getByRole('link', { name: '查看报告' }).click()
    await expect(page.getByText(item.expected)).toBeVisible()
    await expect(page.getByText('证据链')).toBeVisible()
  })
}
