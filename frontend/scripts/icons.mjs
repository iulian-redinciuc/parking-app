// Renders the SVGs in public/icons/ to the PNG app icons (P3.7) and the notification badge (P6.3).
// Run: node scripts/icons.mjs
// The "P" sits inside the central 80 % circle, so the same art works as a maskable icon. The badge
// is a white "P" on transparent: Android shows only its alpha channel, as a monochrome status-bar
// glyph. PW_CHROMIUM_PATH uses a system Chromium (as in playwright.config.ts).
import { readFile } from 'node:fs/promises'
import { chromium } from '@playwright/test'

const dir = new URL('../public/icons/', import.meta.url)
const icons = {
  '192.png': ['icon.svg', 192],
  '512.png': ['icon.svg', 512],
  'maskable-512.png': ['icon.svg', 512],
  'apple-touch-icon-180.png': ['icon.svg', 180],
  'badge-96.png': ['badge.svg', 96],
}

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM_PATH })
const page = await browser.newPage()
for (const [name, [src, size]] of Object.entries(icons)) {
  const svg = await readFile(new URL(src, dir), 'utf8')
  await page.setViewportSize({ width: size, height: size })
  await page.setContent(
    `<style>*{margin:0;background:transparent}svg{display:block;width:${size}px;height:${size}px}</style>${svg}`,
  )
  await page.locator('svg').screenshot({ path: new URL(name, dir).pathname, omitBackground: true })
}
await browser.close()
