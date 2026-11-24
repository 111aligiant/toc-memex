import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_ENTRY).href);
const b64 = readFileSync('assets/icon-favicon.png').toString('base64');
const b = await chromium.launch({ executablePath: process.env.CHROME_PATH });
const p = await b.newPage({ viewport: { width: 360, height: 220 }, deviceScaleFactor: 2, colorScheme: 'light' });
await p.setContent(`
<html><body style="margin:20px;background:#fafafa;color:#222;font-family:system-ui">
  <h3 style="margin:0 0 14px">favicon 透明角验证</h3>
  <div style="display:flex;gap:30px;align-items:center">
    <div>
      <div style="background:#fff;border:1px solid #ddd;border-radius:10px;padding:8px 12px;display:inline-block;font-size:15px">
        <img width="20" height="20" style="vertical-align:-4px" src="data:image/png;base64,${b64}"> toc-memex
      </div>
      <div style="color:#888;font-size:12px;margin-top:6px">白色标签页（角应透明）</div>
    </div>
    <div>
      <img width="96" height="96" src="data:image/png;base64,${b64}">
      <div style="color:#888;font-size:12px;margin-top:6px">README 大图</div>
    </div>
    <div style="background:#0d1117;border-radius:12px;padding:14px">
      <img width="40" height="40" src="data:image/png;base64,${b64}">
      <div style="color:#555;font-size:12px;margin-top:6px">深色底</div>
    </div>
  </div>
</body></html>`);
await p.waitForTimeout(300);
await p.screenshot({ path: 'assets/icon-favicon-check.png' });
await b.close();
console.log('saved');