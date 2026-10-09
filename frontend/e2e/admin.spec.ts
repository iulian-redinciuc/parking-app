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
