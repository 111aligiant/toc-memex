import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import app.backup as backup_mod
from app.backup import backup_data_dir


def _make_db(data: Path) -> None:
    con = sqlite3.connect(data / "tocmemex.db")
    con.execute("create table t (x text)")
    con.execute("insert into t values ('hello')")
    con.commit()
    con.close()


def test_backup_copies_db_and_ocr(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data)
    (data / "ocr" / "doc1").mkdir(parents=True)
    (data / "ocr" / "doc1" / "pages.json").write_text("[]", encoding="utf-8")

    path = backup_data_dir(data, keep=3)
    assert path is not None and path.exists()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "tocmemex.db" in names
        assert "manifest.json" in names
        assert "ocr/doc1/pages.json" in names


def test_backup_rotates_and_is_readable(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data)

    paths = [backup_data_dir(data, keep=2) for _ in range(3)]
    assert len({p.name for p in paths}) == 3
    remaining = sorted((data / "backups").glob("tocmemex-*.zip"))
    assert len(remaining) == 2
    with zipfile.ZipFile(remaining[-1]) as zf:
        zf.extract("tocmemex.db", tmp_path / "extract")
    with sqlite3.connect(tmp_path / "extract" / "tocmemex.db") as restored:
        assert restored.execute("select x from t").fetchone()[0] == "hello"


def test_backup_same_tick_keeps_the_newest(tmp_path, monkeypatch):
    """Windows' coarse clock makes same-tick collisions real. Rotation must
    keep the archive just written, which a collision-only suffix breaks:
    'tocmemex-<stamp>-1.zip' sorts *before* 'tocmemex-<stamp>.zip'."""
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 1, 1, 12, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(backup_mod, "datetime", FrozenDatetime)

    first = backup_data_dir(data, keep=2)
    second = backup_data_dir(data, keep=1)
    assert first is not None and second is not None
    assert first != second
    assert second.exists()
    assert not first.exists()


def test_missing_db_is_a_noop(tmp_path):
    assert backup_data_dir(tmp_path / "data", keep=3) is None


def test_backup_includes_uncheckpointed_wal_commits(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "tocmemex.db")
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.execute("create table t (x text)")
    con.execute("insert into t values ('uncheckpointed')")
    con.commit()

    path = backup_data_dir(data, keep=3)
    # Keep the writer open until after the snapshot so the WAL is live.
    con.close()

    with zipfile.ZipFile(path) as zf:
        zf.extract("tocmemex.db", tmp_path / "extract")
    with sqlite3.connect(tmp_path / "extract" / "tocmemex.db") as restored:
        assert restored.execute("select x from t").fetchone()[0] == "uncheckpointed"


def test_backup_publishes_atomically(tmp_path, monkeypatch):
    import app.backup as backup_mod

    data = tmp_path / "data"
    data.mkdir()
    _make_db(data)

    def boom(source, target):
        raise OSError("rename failed")

    monkeypatch.setattr(backup_mod.os, "replace", boom)
    try:
        backup_data_dir(data, keep=3)
    except OSError as exc:
        assert "rename failed" in str(exc)
    else:
        raise AssertionError("backup should have failed")
    assert not list((data / "backups").glob("tocmemex-*.zip"))
    assert not list((data / "backups").glob("*.tmp"))
