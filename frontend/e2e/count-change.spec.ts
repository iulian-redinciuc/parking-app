import { expect, test } from '@playwright/test'

// P3.10: a new status from the feed shows on screen. ?mock=estimated turns the mock's random
// stale/unavailable episodes off, so the numbers are always on screen; the fake clock skips the
// 3–8 s waits between updates.
test.use({ viewport: { width: 320, height: 640 } })

test('a count change is reflected on the Live screen', async ({ page }) => {
  await page.clock.install()
  await page.goto('./?mock=estimated#/')
  const count = page.getByTestId('big-count')
  const zones = page.getByRole('list', { name: 'Zones' })
  await expect(count).toHaveText(/^≈ \d+$/)

  const read = async () => {
    const free = [...((await zones.textContent()) ?? '').matchAll(/(\d+) \/ \d+/g)].map((m) =>
      Number(m[1]),
    )
    return { total: await count.textContent(), free }
  }
  const first = await read()
  expect(first.free).toHaveLength(2)
  let next = first
  for (let i = 0; i < 30 && next.free.join() === first.free.join(); i++) {
    await page.clock.runFor(8_000)
    next = await read()
  }
  expect(next.free).not.toEqual(first.free)
  // the big number is the sum of the zones' free counts
  expect(next.total).toBe(`≈ ${next.free[0] + next.free[1]}`)
})
