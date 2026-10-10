import { expect, test } from '@playwright/test'

// P8.8 (security-privacy.md §2): the built page carries the strict CSP <meta>, and no screen needs
// anything the policy blocks.
test.describe('content security policy', () => {
  test.use({ viewport: { width: 320, height: 640 } })

  test('is in the page and no screen violates it', async ({ page }) => {
    const blocked: string[] = []
    page.on('console', (msg) => {
      if (/Content[ -]Security[ -]Policy/i.test(msg.text())) blocked.push(msg.text())
    })
    await page.goto('./#/')
    const policy = await page
      .locator('meta[http-equiv="Content-Security-Policy"]')
      .getAttribute('content')
    expect(policy).toContain("default-src 'self'")
    expect(policy).toContain("connect-src 'self'")
    expect(policy).toContain("img-src 'self' data: blob:")
    expect(policy).toContain("object-src 'none'")
    expect(policy).not.toMatch(/script-src|unsafe-eval|\*/)

    await expect(page.getByRole('heading', { name: 'Live' })).toBeVisible()
    for (const hash of ['#/alerts', '#/stats', '#/admin', '#/privacy']) {
      await page.goto(`./${hash}`)
      await expect(page.getByRole('heading').first()).toBeVisible()
    }
    // nothing the app itself does is blocked (checked before the injection below, whose own
    // refusal is worded differently by each browser engine)
    expect(blocked).toEqual([])
    // an inline script is refused: what an injected <script> would meet
    const ran = await page.evaluate(() => {
      const script = document.createElement('script')
      script.textContent = 'window.__inline = true'
      document.head.append(script)
      return '__inline' in window
    })
    expect(ran).toBe(false)
  })
})
