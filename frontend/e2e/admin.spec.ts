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
