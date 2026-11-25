"""Record the ingest-flow and notes-flow demos for the README.

Both flows run against the isolated AI-paper demo library (public papers
only). The ingest flow actually uploads the DDPM paper through the Web
upload dialog and watches the parse queue; the notes flow posts a reply to
a real handoff note. Same visual language as scripts/shoot_agents.py:
110% page zoom, synthetic cursor, step captions, 0.8x playback.

    python scripts/shoot_flows.py --flow ingest   # uploads DDPM for real
    python scripts/shoot_flows.py --flow notes    # reply to a handoff note
    python scripts/shoot_flows.py --flow all

Requires Node + Playwright and a Chromium executable
(PLAYWRIGHT_ENTRY / CHROME_PATH environment variables).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

PROLOG_JS = r"""
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
"""

INIT_JS = r"""
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
"""

HELPERS_JS = r"""
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
    { steps: options.steps ?? 28 },
  );
  await pause(options.pause ?? 240);
}
async function click(locator, options = {}) {
  await moveTo(locator, options);
  await page.mouse.down(); await pause(100); await page.mouse.up();
}
async function still(name) {
  await page.screenshot({ path: `${cfg.stills}/${name}.png` });
}
"""

INGEST_JS = PROLOG_JS + INIT_JS + HELPERS_JS + r"""
const fs = await import('node:fs');
const timings = { videoStart: Date.now() };

// ① Library: two papers already indexed
await page.goto(`${cfg.base}/#/library`, { waitUntil: 'networkidle' });
await pause(800);
await say('①', '知识库 · 每份文档都有页数、节点数与覆盖率');
await still('ingest-library-before');
await pause(400);

// ② Upload dialog
await say('②', '上传对话框 · 选文件、填入库备注');
await click(page.getByRole('button', { name: '上传', exact: true }));
await page.waitForSelector('.modal-backdrop', { state: 'visible' });
await pause(600);
await page.setInputFiles('input[type="file"]', cfg.pdfPath);
await pause(900);
const note = page.locator('.note-field textarea');
await note.click();
await note.type(cfg.ingestNote, { delay: 28 });
await pause(700);
await still('ingest-dialog');

// ③ Submit: file enters the parse queue
await say('③', '开始上传 · 文件进入解析队列');
timings.queueShown = null;
await click(page.getByRole('button', { name: /开始上传/ }));
await pause(900);
await click(page.getByRole('button', { name: /关闭|后台继续/ }));

// ④ Parse queue: stages advance second by second (sped up in post)
await say('④', '解析队列 · 抽取 → 目录重建 → 切片 → 索引，分阶段推进');
await click(page.getByText('解析队列', { exact: false }).first());
await page.waitForURL(/#\/jobs/);
await pause(400);
timings.queueShown = Date.now();
await page.waitForSelector('.job-row', { state: 'visible', timeout: 60000 });
await pause(1600);
await still('ingest-jobs');
await page.waitForSelector('.job-row', { state: 'detached', timeout: 300000 });
timings.queueDone = Date.now();

// ⑤ Back to the library: the new document is ready
await say('⑤', '入库完成 · 新文档出现在知识库，目录与页码就绪');
await click(page.getByText('知识库', { exact: false }).first());
await page.waitForURL(/#\/library/);
await page.getByText(cfg.docTitle, { exact: false }).first().waitFor({ state: 'visible', timeout: 60000 });
await pause(1400);
await still('ingest-library-after');
await pause(1600);

timings.videoEnd = Date.now();
fs.writeFileSync(cfg.timingsPath, JSON.stringify(timings));
const video = page.video();
await context.close();
if (video) await video.saveAs(`${cfg.videoDir}/ingest-flow.webm`);
await browser.close();
"""

NOTES_JS = PROLOG_JS + INIT_JS + HELPERS_JS + r"""
// ① Notes: the shared, per-source anchor area
await page.goto(`${cfg.base}/#/notes`, { waitUntil: 'networkidle' });
await pause(900);
await say('①', '手记 · AI 留痕的公共区，每条都挂在 doc / node / page 上');
await still('notes-page');

// Set the local author first (submit is blocked otherwise)
const author = page.locator('.author-input');
await click(author);
await author.type('dsh-mcp-client', { delay: 24 });
await author.press('Enter');
await pause(600);

// ② Reply to the WorkBuddy handoff
await say('②', '回复手记 · 补充信息留给下一次会话');
const handoff = page.locator('.ui-note', { hasText: '已核对 Transformer 的两类注意力结构' }).first();
await click(handoff.getByRole('button', { name: '回复' }));
await pause(700);
const box = page.locator('.notes-side textarea').first();
const prefix = await box.inputValue();
await box.click();
await box.type(cfg.replyText, { delay: 26 });
await pause(600);
await still('notes-reply-draft');
await click(page.getByRole('button', { name: '发布' }));
await page.getByText(cfg.replyText.slice(0, 18), { exact: false }).first()
  .waitFor({ state: 'visible', timeout: 30000 });
await pause(700);

// ③ The reply nests under its parent; next session reads it first
await say('③', '回复已发布 · 挂在同一条手记下面，刷新后仍在');
const replyRow = page.locator('.note-row.reply').first();
await replyRow.scrollIntoViewIfNeeded();
await pause(800);
await still('notes-reply');
await pause(1400);

const video = page.video();
await context.close();
if (video) await video.saveAs(`${cfg.videoDir}/notes-flow.webm`);
await browser.close();
"""


def run(command: list[str]) -> None:
    print("+", " ".join(command[:5]), "...")
    subprocess.run(command, check=True)


def export_video(source: Path, mp4: Path, gif: Path, speed: float,
                 segments: list[tuple[float, float, float]] | None,
                 queue_speed: float) -> None:
    """Encode webm -> mp4/gif at `speed` playback; optionally speed up the
    middle segment (queue progress) by `queue_speed`."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def chain(fps: int, tail: str) -> str:
        head = f"crop=1280:800:0:0,setpts=PTS/{speed:g},fps={fps},{tail}"
        if segments:
            a, b, _ = segments
            pre = (
                f"[0:v]trim=start=0:end={a:.3f},setpts=PTS-STARTPTS[s0];"
                f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{queue_speed:g}[s1];"
                f"[0:v]trim=start={b:.3f},setpts=PTS-STARTPTS[s2];"
                "[s0][s1][s2]concat=n=3:v=1,setsar=1[cat];"
                f"[cat]{head}"
            )
            return pre
        return f"[0:v]{head}"

    # MP4
    if segments:
        run([
            ffmpeg, "-y", "-i", str(source), "-filter_complex",
            chain(20, "format=yuv420p[vout]"),
            "-map", "[vout]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-movflags", "+faststart", str(mp4),
        ])
    else:
        run([
            ffmpeg, "-y", "-i", str(source), "-vf",
            f"crop=1280:800:0:0,setpts=PTS/{speed:g},fps=20",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4),
        ])
    # GIF（960 宽 / 12fps / 128 色，控制 README 体积）
    gif_tail = (
        "scale=960:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=128:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    run([
        ffmpeg, "-y", "-i", str(source), "-filter_complex",
        chain(12, gif_tail),
        "-loop", "0", str(gif),
    ])
    with Image.open(gif) as image:
        print(
            f"gif: {image.n_frames} frames, {image.size[0]}x{image.size[1]}, "
            f"{gif.stat().st_size / 1024 / 1024:.1f} MiB"
        )
    print(f"mp4: {mp4.stat().st_size / 1024 / 1024:.1f} MiB")


def record(js: str, payload: dict, work_dir: Path, script_name: str) -> None:
    script = work_dir.parent / f"_{script_name}.mjs"
    script.write_text(js, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), json.dumps(payload)], cwd=ROOT, text=True,
    )
    if result.returncode:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=["ingest", "notes", "all"], default="all")
    parser.add_argument("--base", default="http://127.0.0.1:8793")
    parser.add_argument("--out", default="assets")
    parser.add_argument("--work", default="web/shots/ai-demo/recording")
    parser.add_argument("--speed", type=float, default=0.8,
                        help="Global playback speed (0.8 = slowed for readability).")
    parser.add_argument("--queue-speed", type=float, default=6.0,
                        help="Speed-up factor applied to the parse-queue segment.")
    args = parser.parse_args()

    out_dir = (ROOT / args.out).resolve()
    work_dir = (ROOT / args.work).resolve()
    stills_dir = work_dir / "stills"
    video_dir = work_dir / "video"
    for d in (out_dir, stills_dir, video_dir):
        d.mkdir(parents=True, exist_ok=True)

    common = {
        "base": args.base,
        "stills": str(stills_dir),
        "videoDir": str(video_dir),
    }

    if args.flow in ("ingest", "all"):
        timings_path = work_dir / "ingest-timings.json"
        pdf = ROOT / "web/shots/ai-demo/papers/Denoising Diffusion Probabilistic Models.pdf"
        record(INGEST_JS, {
            **common,
            "pdfPath": str(pdf),
            "ingestNote": (
                "Denoising Diffusion Probabilistic Models（DDPM），arXiv:2006.11239，"
                "2020-06-19 提交。文本层 PDF，无需 OCR。用于扩散模型方向的引用核对。"
            ),
            "docTitle": "Denoising Diffusion Probabilistic Models",
            "timingsPath": str(timings_path),
        }, work_dir, "record_ingest")
        t = json.loads(timings_path.read_text())
        start = t["videoStart"]
        a = max((t["queueShown"] - start) / 1000 - 0.4, 1.0)
        b = (t["queueDone"] - start) / 1000 + 0.3
        print(f"queue segment: {a:.1f}s - {b:.1f}s (x{args.queue_speed:g})")
        export_video(
            video_dir / "ingest-flow.webm",
            out_dir / "ingest-flow.mp4",
            out_dir / "ingest-flow.gif",
            args.speed, (a, b, None), args.queue_speed,
        )
        for name, height in {"ingest-jobs": 800, "ingest-library-after": 800}.items():
            with Image.open(stills_dir / f"{name}.png") as image:
                image.crop((0, 0, 1280, height)).save(out_dir / f"{name}.png", optimize=True)
            print("still", name)

    if args.flow in ("notes", "all"):
        record(NOTES_JS, {
            **common,
            "replyText": "收到。Multi-Head 的参数矩阵切分我接着核对 §3.2.2，结果会补在这条下面。",
        }, work_dir, "record_notes")
        export_video(
            video_dir / "notes-flow.webm",
            out_dir / "notes-flow.mp4",
            out_dir / "notes-flow.gif",
            args.speed, None, args.queue_speed,
        )
        for name in ("notes-page", "notes-reply"):
            with Image.open(stills_dir / f"{name}.png") as image:
                image.crop((0, 0, 1280, 800)).save(out_dir / f"{name}.png", optimize=True)
            print("still", name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
