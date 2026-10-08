// Renders public/icons/icon.svg to the PNG app icons (P3.7). Run: node scripts/icons.mjs
// The "P" sits inside the central 80 % circle, so the same art works as a maskable icon.
// PW_CHROMIUM_PATH uses a system Chromium (as in playwright.config.ts).
import { readFile } from 'node:fs/promises'
import { chromium } from '@playwright/test'

const dir = new URL('../public/icons/', import.meta.url)
const svg = await readFile(new URL('icon.svg', dir), 'utf8')
const sizes = {
  '192.png': 192,
  '512.png': 512,
  'maskable-512.png': 512,
  'apple-touch-icon-180.png': 180,
}

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM_PATH })
const page = await browser.newPage()
for (const [name, size] of Object.entries(sizes)) {
  await page.setViewportSize({ width: size, height: size })
  await page.setContent(
    `<style>*{margin:0}svg{display:block;width:${size}px;height:${size}px}</style>${svg}`,
  )
  await page.locator('svg').screenshot({ path: new URL(name, dir).pathname })
}
await browser.close()
