
const { pathToFileURL } = await import('node:url');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_ENTRY).href);

const cfg = JSON.parse(process.argv[2]);
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  channel: process.env.CHROME_PATH ? undefined : 'chromium',
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  locale: 'zh-CN',
  colorScheme: 'light',
});

for (const [name, route, waitText, action, fullPage] of cfg.shots) {
  const path = route.replace('{doc_id}', cfg.docId);
  await page.goto(cfg.base + path, { waitUntil: 'networkidle' });
  if (waitText) {
    try { await page.waitForSelector(waitText, { timeout: 8000 }); } catch {}
  }
  if (action === 'search') {
    const input = page.locator('input[placeholder*="搜索"]').last();
    await input.fill('table recognition');
    await input.press('Enter');
    await page.waitForTimeout(2000);
  }
  if (action === 'toc') {
    await page.locator('.tabs button', { hasText: '目录' }).first().click();
    await page.waitForTimeout(1200);
  }
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${cfg.out}/${name}`, fullPage });
  console.log('shot', name, '->', path);
}
await browser.close();
