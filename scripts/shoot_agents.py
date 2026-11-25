"""Record the public AI-paper external-agent demo for the README.

The recording uses an isolated Web/MCP demo library containing only public
papers and synthetic WorkBuddy/DSH sessions. It never opens the developer's
primary database. A real browser video is captured, then converted to a
20 fps GIF and an MP4; two clean PNG stills are exported as well.

    python scripts/demo_ai_agents.py  # create the real MCP sessions first
    python scripts/shoot_agents.py --base http://127.0.0.1:8793

Requires Node + Playwright, a Chromium executable and imageio-ffmpeg.
PLAYWRIGHT_ENTRY and CHROME_PATH select the local installations on Windows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

JS = r"""
const { pathToFileURL } = await import('node:url');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_ENTRY).href);
const cfg = JSON.parse(process.argv[2]);

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  channel: process.env.CHROME_PATH ? undefined : 'chromium',
});
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  deviceScaleFactor: 1,
  locale: 'zh-CN',
  colorScheme: 'light',
  recordVideo: { dir: cfg.videoDir, size: { width: 1280, height: 800 } },
});
const page = await context.newPage();

await page.addInitScript(() => {
  document.addEventListener('DOMContentLoaded', () => {
    // Browser-level page zoom (what Ctrl+= does), per user preference: 110%.
    document.documentElement.style.zoom = '1.10';
    const style = document.createElement('style');
    style.textContent = `
      #demo-cursor { position: fixed; z-index: 2147483647; width: 30px; height: 36px;
        pointer-events: none; left: 0; top: 0; filter: drop-shadow(0 3px 5px #0f172a55);
        will-change: transform; }
      #demo-cursor svg { width: 30px; height: 36px; }
      .demo-ripple { position: fixed; z-index: 2147483646; width: 20px; height: 20px;
        border: 3px solid #2563eb; border-radius: 50%; pointer-events: none;
        animation: demoRipple .55s ease-out forwards; }
      @keyframes demoRipple { from { opacity: .9; transform: translate(-50%,-50%) scale(.5); }
        to { opacity: 0; transform: translate(-50%,-50%) scale(3.2); } }
      #demo-caption { position: fixed; z-index: 2147483600; left: 50%; bottom: 26px;
        transform: translateX(-50%); max-width: 860px; padding: 11px 22px;
        border-radius: 999px; color: #f8fafc; background: #0f172aeb;
        font: 600 16px/1.45 system-ui, sans-serif; letter-spacing: .02em;
        box-shadow: 0 10px 30px #0f172a40; pointer-events: none;
        opacity: 0; transition: opacity .28s ease; text-align: center; }
      #demo-caption.show { opacity: 1; }
      #demo-caption .step { color: #93c5fd; margin-right: 10px; font-weight: 700; }
      .agent-composer { display: none !important; }
    `;
    document.head.appendChild(style);
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    cursor.innerHTML = `<svg viewBox="0 0 30 36"><path d="M3 2 L25 22 L16 23 L21 32 L16 35 L11 25 L3 31 Z"
      fill="#ffffff" stroke="#0f172a" stroke-width="2.2" stroke-linejoin="round"/></svg>`;
    document.body.appendChild(cursor);
    const caption = document.createElement('div');
    caption.id = 'demo-caption';
    document.body.appendChild(caption);
    let x = innerWidth * .7, y = 90, tx = x, ty = y;
    document.addEventListener('mousemove', (event) => { tx = event.clientX; ty = event.clientY; });
    document.addEventListener('mousedown', (event) => {
      const ripple = document.createElement('div');
      ripple.className = 'demo-ripple';
      ripple.style.left = `${event.clientX}px`; ripple.style.top = `${event.clientY}px`;
      document.body.appendChild(ripple); setTimeout(() => ripple.remove(), 650);
    });
    const tick = () => {
      x += (tx - x) * .28; y += (ty - y) * .28;
      cursor.style.transform = `translate(${x}px, ${y}px)`;
      requestAnimationFrame(tick);
    };
    tick();
    window.__demoCaption = (step, text) => {
      const el = document.getElementById('demo-caption');
      el.classList.remove('show');
      setTimeout(() => {
        el.innerHTML = `<span class="step">${step}</span>${text}`;
        el.classList.add('show');
      }, 220);
    };
  });
});

const pause = (ms) => page.waitForTimeout(ms);
const say = async (step, text, hold = 400) => {
  await page.evaluate(([s, t]) => window.__demoCaption(s, t), [step, text]);
  await pause(hold);
};
async function moveTo(locator, options = {}) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error('Cannot move to an invisible element');
  await page.mouse.move(
    box.x + box.width * (options.x ?? .55),
    box.y + box.height * (options.y ?? .5),
    { steps: options.steps ?? 32 },
  );
  await pause(options.pause ?? 260);
}
async function click(locator) {
  await moveTo(locator);
  await page.mouse.down(); await pause(100); await page.mouse.up();
}
async function still(name) {
  await page.screenshot({ path: `${cfg.stills}/${name}.png` });
}

// ① Task page: external MCP sessions archived as timelines
await page.goto(`${cfg.base}/#/tasks`, { waitUntil: 'networkidle' });
await pause(700);
await say('①', '任务页 · 外部 MCP agent 会话自动归档为时间线');
const all = page.getByText('全部', { exact: false }).first();
await click(all);
await pause(700);

// ② Expand the WorkBuddy session
await say('②', '展开 WorkBuddy 会话 · 检索 / 阅读 / 手记都在这条时间线上');
const task = page.locator('.task-row', { hasText: 'Transformer 注意力机制' }).first();
await click(task);
await pause(1500);
await still('agent-tasks');

// ③ Jump to the test bench replay
await say('③', '进入测试台 · 逐步回放这次会话的 MCP 调用');
const runLink = page.locator('.run-link', { hasText: 'Transformer 注意力机制' }).first();
await click(runLink);
await page.waitForURL(/#\/agent\?run=/);
await page.waitForLoadState('networkidle');
await pause(1100);

// ④ search step
await say('④', 'search · 命中章节带页码与路径');
const searchResult = page.locator('.trace-step', { hasText: '1 条命中' }).first();
await click(searchResult);
await pause(1400);

// ⑤ read_section step
await say('⑤', 'read_section · 读原文切片与引用置信度');
const readResult = page.locator('.trace-step', {
  hasText: 'Scaled Dot-Product Attention · 第 4 页',
}).first();
await click(readResult);
await pause(1200);
await still('agent-replay');

// ⑥ Hold on the evidence panel: section path and PDF page anchor
await say('⑥', '证据面板 · 章节路径与 PDF 页码都挂在同一条记录上');
await pause(2400);

// ⑦ handoff note
await say('⑦', 'add_note · 手记挂到章节，下一次会话可见');
const note = page.locator('.trace-step', { hasText: '下次会话可从这两个节点继续' }).last();
await click(note);
await pause(1800);

const video = page.video();
await context.close();
if (video) await video.saveAs(`${cfg.videoDir}/agent-session.webm`);
await browser.close();
"""


def run(command: list[str]) -> None:
    print("+", " ".join(command[:5]), "...")
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8793")
    parser.add_argument("--sessions", default="web/shots/ai-demo/sessions.json")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", default="assets")
    parser.add_argument("--work", default="web/shots/ai-demo/recording")
    parser.add_argument(
        "--trim-start", type=float, default=2.3,
        help="Drop the initial page-loading frames from the exported video/GIF.",
    )
    parser.add_argument(
        "--speed", type=float, default=0.8,
        help="Playback speed factor; 0.8 slows the recording to 80%% for readability.",
    )
    args = parser.parse_args()

    run_id = args.run_id
    if not run_id:
        sessions = json.loads((ROOT / args.sessions).read_text(encoding="utf-8"))
        run_id = sessions["workbuddy"]["run_id"]

    out_dir = (ROOT / args.out).resolve()
    work_dir = (ROOT / args.work).resolve()
    stills_dir = work_dir / "stills"
    video_dir = work_dir / "video"
    for directory in (out_dir, stills_dir, video_dir):
        directory.mkdir(parents=True, exist_ok=True)

    script = ROOT / "web" / "shots" / "_record_agents.mjs"
    script.write_text(JS, encoding="utf-8")
    payload = json.dumps({
        "base": args.base,
        "runId": run_id,
        "stills": str(stills_dir),
        "videoDir": str(video_dir),
    })
    result = subprocess.run(
        ["node", str(script), payload], cwd=ROOT, text=True,
    )
    if result.returncode:
        return result.returncode

    crop_heights = {"agent-tasks.png": 800, "agent-replay.png": 800}
    for name, height in crop_heights.items():
        with Image.open(stills_dir / name) as image:
            image.crop((0, 0, 1280, height)).save(out_dir / name, optimize=True)
        print("still", name)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = video_dir / "agent-session.webm"
    mp4 = out_dir / "agent-session.mp4"
    gif = out_dir / "agent-session.gif"
    video_filter = (
        f"crop=1280:800:0:0,setpts=PTS/{args.speed},fps=20"
    )
    run([
        ffmpeg, "-y", "-ss", str(args.trim_start), "-i", str(source), "-vf", video_filter,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4),
    ])
    palette = (
        f"[0:v]crop=1280:800:0:0,setpts=PTS/{args.speed},fps=16,split[a][b];"
        "[a]palettegen=max_colors=192:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    run([
        ffmpeg, "-y", "-ss", str(args.trim_start), "-i", str(source),
        "-filter_complex", palette,
        "-loop", "0", str(gif),
    ])
    with Image.open(gif) as image:
        print(
            f"gif: {image.n_frames} frames, {image.size[0]}x{image.size[1]}, "
            f"{gif.stat().st_size / 1024 / 1024:.1f} MiB"
        )
    print(f"mp4: {mp4.stat().st_size / 1024 / 1024:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
