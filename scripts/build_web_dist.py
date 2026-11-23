"""Build the Vue frontend and copy the bundle into ``app/web_dist``.

The bundle ships inside the wheel so ``toc-memex-web`` needs no npm install.
Run from anywhere:

    python scripts/build_web_dist.py

Requires Node.js + npm on PATH. Exits non-zero on any failure and never
writes a partial bundle: ``app/web_dist`` is only replaced after a successful
``npm run build``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DIST_DIR = WEB_DIR / "dist"
TARGET_DIR = ROOT / "app" / "web_dist"


def _fail(message: str) -> int:
    print(f"[build_web_dist] {message}", file=sys.stderr)
    return 1


def _run_npm(npm: str, args: list[str]) -> None:
    print(f"[build_web_dist] npm {' '.join(args)} (cwd={WEB_DIR})")
    subprocess.run([npm, *args], cwd=WEB_DIR, check=True)


def _tree_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def main() -> int:
    if not (WEB_DIR / "package.json").is_file():
        return _fail(f"web/package.json not found under {WEB_DIR}")

    npm = shutil.which("npm")
    if npm is None:
        return _fail(
            "npm not found on PATH; install Node.js 20+ to build the web bundle"
        )

    try:
        _run_npm(npm, ["ci"])
        _run_npm(npm, ["run", "build"])
    except subprocess.CalledProcessError as exc:
        return _fail(f"npm failed with exit code {exc.returncode}")

    if not (DIST_DIR / "index.html").is_file():
        return _fail(f"{DIST_DIR / 'index.html'} missing after build")
    assets = sorted((DIST_DIR / "assets").glob("*")) if (DIST_DIR / "assets").is_dir() else []
    if not assets:
        return _fail(f"{DIST_DIR / 'assets'} is missing or empty after build")

    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    shutil.copytree(DIST_DIR, TARGET_DIR)

    files = _tree_files(TARGET_DIR)
    total = sum(path.stat().st_size for path in files)
    print(
        f"[build_web_dist] copied {len(files)} files, "
        f"{total / 1024:.1f} KiB -> {TARGET_DIR}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
