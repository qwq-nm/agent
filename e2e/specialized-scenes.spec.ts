import { expect, test } from '@playwright/test'

const cases = [
  {
    title: '漏洞挖掘',
    goal: '对授权上传源码进行漏洞挖掘，定位命令执行和调试配置风险',
    expected: 'VULN-CMD-001',
  },
  {
    title: '逆向分析',
    goal: '对授权上传样本进行逆向静态分析，提取字符串和文件元数据，禁止执行样本',
    expected: 'reverse_artifact',
  },
]

for (const item of cases) {
  test(`${item.title} completes a traceable task`, async ({ page }) => {
    await page.goto('/login')
    await page.getByLabel('Username').fill(process.env.SECAGENT_E2E_USER || 'admin')
    await page.getByLabel('Password').fill(process.env.SECAGENT_E2E_PASSWORD || 'admin')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await page.goto('/tasks/new')
    await page.locator('button.template-card').filter({ hasText: item.title }).click()
    await page.getByLabel('任务目标').fill(item.goal)
    await page.getByLabel('上传材料').setInputFiles('demo_cases/source_audit/vulnerable_app.zip')
    await page.getByRole('button', { name: '生成执行计划' }).click()
    await page.getByRole('button', { name: '开始执行' }).click()

    await expect(page.getByText('completed', { exact: true })).toBeVisible({ timeout: 120_000 })
    await page.getByRole('link', { name: '查看报告' }).click()
    await expect(page.getByText(item.expected, { exact: false })).toBeVisible()
    await expect(page.getByText('证据链')).toBeVisible()
  })
}
