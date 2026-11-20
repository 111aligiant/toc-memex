"""Zip the archive database and OCR cache before a service start.

Run from the project root:

    python scripts/backup.py --data-dir data --keep 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import backup_data_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/backup.py")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args(argv)

    try:
        archive = backup_data_dir(Path(args.data_dir), keep=args.keep)
    except Exception as exc:
        print(f"backup: failed: {exc}")
        return 1
    if archive is None:
        print("backup: skipped (no database)")
    else:
        print(f"backup: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
