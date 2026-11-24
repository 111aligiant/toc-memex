"""Capture README screenshots from a running web UI.

Usage:
    python scripts/shoot_readme.py --base http://127.0.0.1:8899 --out docs/images

Requires Node's global playwright and its bundled Chromium.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SHOTS = [
    # (filename, hash route, wait text, action, full page?)
    ("overview.png", "/#/overview", "text=概览", None, False),
    ("library.png", "/#/library", "text=知识库", None, False),
    ("doc-read.png", "/#/doc/{doc_id}", None, None, False),
    ("search.png", "/#/search", None, "search", False),
    ("doc-toc.png", "/#/doc/{doc_id}", None, "toc", False),
]

DOC_ID = "f52e440d-7ede-43b4-9ce1-658ee230eb06"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8899")
    parser.add_argument("--out", default="docs/images")
    args = parser.parse_args()

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    script = ROOT / "scripts" / "_shoot.mjs"
    script.write_text(JS, encoding="utf-8")

    payload = json.dumps({
        "base": args.base,
        "out": str(out_dir),
        "docId": DOC_ID,
        "shots": SHOTS,
    })
    result = subprocess.run(
        ["node", str(script), payload],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


JS = r"""
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
"""


if __name__ == "__main__":
    raise SystemExit(main())
