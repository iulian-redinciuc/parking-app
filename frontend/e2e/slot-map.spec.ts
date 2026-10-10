import { expect, test } from '@playwright/test'

// P9.1: the ground card's slot map in mock mode (frontend.md §2.1, slot-map.md).
test.use({ viewport: { width: 320, height: 640 } })

test('the slot map opens, matches the count and names a tapped space', async ({ page }, info) => {
  await page.goto('./?mock=estimated')
  const card = page.getByTestId('zone-ground')
  const toggle = card.getByRole('button', { name: 'Show map' })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByTestId('zone-underground').getByRole('button')).toHaveCount(0)

  await toggle.click()
  const map = card.getByRole('group', { name: 'Map of Ground' })
  await expect(map).toBeVisible()
  await expect(map.locator('[data-slot]')).toHaveCount(40)
  // as many free shapes as the card's number says (read together: the mock feed keeps moving)
  await expect(async () => {
    const free = Number((await card.textContent())!.match(/(\d+) \/ 40/)![1])
    expect(await map.locator('[data-state="free"]').count()).toBe(free)
  }).toPass()

  const slot = map.locator('[data-slot="G07"]')
  await slot.click()
  await expect(slot).toHaveAttribute('aria-pressed', 'true')
  await expect(async () => {
    const state = (await slot.getAttribute('data-state')) === 'free' ? 'Free' : 'Taken'
    expect(await card.getByRole('status').textContent()).toBe(`Space G07: ${state}`)
  }).toPass()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: info.outputPath('slot-map.png'), fullPage: true })

  // the choice survives a reload
  await page.reload()
  await expect(card.getByRole('group', { name: 'Map of Ground' })).toBeVisible()
})
