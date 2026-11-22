"""Consistent backups of the archive before startup.

The SQLite database runs in WAL mode, so copying ``tocmemex.db`` file-by-file
can miss committed pages. ``sqlite3.Connection.backup`` produces a
transactionally consistent snapshot instead. The OCR cache is plain files
and is copied into the same zip.

Backups are best-effort: a failure is reported to the launcher, which still
starts the service.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("toc-memex")
    except Exception:
        return "0.1.0"


def _rotate(backups_dir: Path, keep: int) -> None:
    if keep < 1:
        return
    archives = sorted(backups_dir.glob("tocmemex-*.zip"))
    for stale in archives[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass


def backup_data_dir(data_dir: Path, *, keep: int = 3) -> Path | None:
    """Zip ``tocmemex.db`` + ``ocr/`` into ``data_dir/backups``.

    Returns the archive path, or ``None`` when there is no database yet
    (first launch). Keeps the newest ``keep`` archives.
    """
    data_dir = Path(data_dir)
    db_path = data_dir / "tocmemex.db"
    if not db_path.exists():
        return None

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    # ``datetime.now()`` on Windows has ~10ms resolution, so two backups in the
    # same tick get the same microsecond stamp. Every archive carries a
    # zero-padded counter, even the first one: ``-000`` sorts before ``-001``,
    # so ``_rotate``'s plain name sort stays chronological. (Appending a
    # suffix only on collision would sort the newcomer *before* its base name,
    # because '-' < '.', and rotation could delete the backup just written.)
    n = 0
    archive = backups_dir / f"tocmemex-{stamp}-{n:03d}.zip"
    while archive.exists():
        n += 1
        archive = backups_dir / f"tocmemex-{stamp}-{n:03d}.zip"

    with tempfile.TemporaryDirectory(prefix="tocmemex-backup-") as temp:
        snapshot = Path(temp) / "tocmemex.db"
        source = sqlite3.connect(db_path)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        ocr_dir = data_dir / "ocr"
        ocr_files = [
            path
            for path in ocr_dir.rglob("*")
            if path.is_file()
        ] if ocr_dir.is_dir() else []
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "db_bytes": snapshot.stat().st_size,
            "ocr_files": len(ocr_files),
            "app_version": _app_version(),
        }

        # Write under a temporary name and only publish a complete archive;
        # a killed process must not leave a truncated tocmemex-*.zip that
        # rotation treats as a valid backup.
        temp_archive = backups_dir / f".{archive.name}.tmp"
        try:
            with zipfile.ZipFile(
                temp_archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                zf.write(snapshot, "tocmemex.db")
                for path in ocr_files:
                    zf.write(path, f"ocr/{path.relative_to(ocr_dir).as_posix()}")
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            os.replace(temp_archive, archive)
        except Exception:
            try:
                temp_archive.unlink()
            except OSError:
                pass
            raise

    _rotate(backups_dir, keep)
    return archive
