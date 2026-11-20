/* Verify the built app: capture screenshots of each view + console errors.
   Run: node scripts/verify-web.mjs  (from web/) */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8791'
const OUT = 'shots'
mkdirSync(OUT, { recursive: true })

const pages = [
  { name: 'overview', hash: '#/overview' },
  { name: 'library', hash: '#/library' },
  { name: 'search', hash: '#/search?q=%E6%B8%A9%E5%BA%A6', wait: '.result-row, .empty' },
  { name: 'quality', hash: '#/quality' },
  { name: 'jobs', hash: '#/jobs' },
  { name: 'settings', hash: '#/settings' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const errors = []
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(`[console] ${msg.text()}`)
})
page.on('pageerror', (err) => errors.push(`[pageerror] ${err.message}`))

for (const item of pages) {
  await page.goto(`${BASE}/${item.hash}`, { waitUntil: 'networkidle' })
  if (item.wait) await page.waitForSelector(item.wait, { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${item.name}.png` })
  console.log(`captured ${item.name}`)
}

// document workspace: open first doc from library
await page.goto(`${BASE}/#/library`, { waitUntil: 'networkidle' })
await page.waitForSelector('table.data tbody tr')
const firstRow = page.locator('table.data tbody tr').first()
await firstRow.click()
await page.waitForTimeout(1200)
await page.screenshot({ path: `${OUT}/doc-read.png` })
console.log('captured doc-read')

for (const tab of ['toc', 'chunks', 'quality', 'info']) {
  await page.click(`.tabs button:has-text("${{ toc: '目录', chunks: '切片', quality: '质量', info: '信息' }[tab]}")`)
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/doc-${tab}.png` })
  console.log(`captured doc-${tab}`)
}

// dark theme
await page.goto(`${BASE}/#/overview`, { waitUntil: 'networkidle' })
await page.evaluate(() => {
  document.documentElement.dataset.theme = 'dark'
  localStorage.setItem('kb-theme', 'dark')
})
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/overview-dark.png` })
console.log('captured overview-dark')

console.log(errors.length ? `ERRORS:\n${errors.join('\n')}` : 'no console errors')
await browser.close()
