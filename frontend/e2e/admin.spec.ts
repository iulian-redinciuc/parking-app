import { expect, test } from '@playwright/test'

// P7.1: #/admin on the mock build (the mock accepts only the password "demo"): wrong password →
// error, sign in → admin home, the token lives in sessionStorage (gone in a new tab), log out.
test('admin login, reload keeps the session, log out', async ({ page }) => {
  await page.goto('./#/admin')
  const password = page.getByLabel('Password')
  await password.fill('wrong')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText('Wrong password.')

  await password.fill('demo')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('parking.adminToken'))).toBeTruthy()
  expect(await page.evaluate(() => localStorage.getItem('parking.adminToken'))).toBeNull()

  await page.reload()
  await expect(page.getByText('Signed in', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page.getByLabel('Password')).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('parking.adminToken'))).toBeNull()
})

// P7.2: the camera list (mock health) and a camera's snapshot shown through a blob URL.
test('camera health list and snapshot', async ({ page }) => {
  await page.goto('./#/admin')
  await page.getByLabel('Password').fill('demo')
  await page.getByRole('button', { name: 'Sign in' }).click()
  const ground = page.getByRole('link', { name: /cam-ground/ })
  await expect(ground).toContainText('OK')
  await expect(ground).toContainText('0.2 fps')
  await expect(page.getByRole('link', { name: /cam-ramp/ })).toContainText('No reports yet')

  await ground.click()
  await expect(page).toHaveURL(/#\/admin\/cameras\/cam-ground$/)
  const image = page.getByRole('img', { name: /cam-ground with slots/ })
  await expect(image).toHaveAttribute('src', /^blob:/)
  await expect
    .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBeGreaterThan(0)

  await page.getByRole('link', { name: '← All cameras' }).click()
  await expect(page).toHaveURL(/#\/admin$/)
  await page.getByRole('link', { name: /cam-ramp/ }).click()
  await expect(page.getByRole('alert')).toContainText('No snapshot right now')
})

// P7.3: the slot editor on the mock build (demo snapshot 640×360, three demo spaces), driven by
// touch: tap four corners and the first one again to add a space, "Move all" + drag, save, then
// save a new reference frame.
test('slot editor: tap to add a space, move all, save', async ({ page }) => {
  await page.goto('./#/admin')
  await page.getByLabel('Password').fill('demo')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await page.getByRole('link', { name: /cam-ground/ }).click()
  await page.getByRole('link', { name: 'Edit parking spaces' }).click()
  await expect(page).toHaveURL(/#\/admin\/cameras\/cam-ground\/edit$/)
  await expect(page.getByText('3 spaces')).toBeVisible()

  // image pixels → page pixels, as the editor fits the picture (98 % of the canvas, centred)
  const canvas = page.getByRole('img', { name: /cam-ground with the shapes/ })
  const box = (await canvas.boundingBox())!
  const scale = Math.min(box.width / 640, box.height / 360) * 0.98
  const at = (x: number, y: number) => ({
    x: box.x + box.width / 2 + (x - 320) * scale,
    y: box.y + box.height / 2 + (y - 180) * scale,
  })
  for (const [x, y] of [
    [420, 40],
    [540, 40],
    [540, 160],
    [420, 160],
    [420, 40],
  ]) {
    const p = at(x, y)
    await page.touchscreen.tap(p.x, p.y)
  }
  await expect(page.getByText('4 spaces')).toBeVisible()
  await expect(page.getByText('Space G04')).toBeVisible()

  await page.getByRole('checkbox', { name: 'Move all' }).check()
  const from = at(300, 100)
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(from.x + 20, from.y + 10, { steps: 5 })
  await page.mouse.up()
  await expect(page.getByText('Unsaved changes.')).toBeVisible()

  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Saved config/slots/cam-ground.json.')).toBeVisible()
  await expect(page.getByText(/The worker reloaded it/)).toBeVisible()
  await page.getByRole('button', { name: 'Save reference frame' }).last().click()
  await expect(page.getByText(/^Reference frame saved/)).toBeVisible()
})
