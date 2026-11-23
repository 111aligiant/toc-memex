from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.web_api
from app.web_api import create_app, default_web_dir

PACKAGED_WEB = Path(app.web_api.__file__).resolve().parent / "web_dist"


def test_packaged_web_bundle_is_complete() -> None:
    index = PACKAGED_WEB / "index.html"
    assert index.is_file(), (
        "app/web_dist is missing; run scripts/build_web_dist.py before packaging"
    )
    assets = [path for path in (PACKAGED_WEB / "assets").glob("*") if path.is_file()]
    assert assets, "app/web_dist/assets is empty"


def test_default_web_dir_falls_back_to_packaged_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no local web/dist here
    resolved = default_web_dir()
    assert resolved is not None
    assert Path(resolved).resolve() == PACKAGED_WEB
    assert (Path(resolved) / "index.html").is_file()


def test_default_web_dir_prefers_local_dev_build(tmp_path, monkeypatch) -> None:
    dev_build = tmp_path / "web" / "dist"
    dev_build.mkdir(parents=True)
    (dev_build / "index.html").write_text("<html>dev</html>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert default_web_dir() == Path("web/dist")


def test_create_app_mounts_packaged_bundle(tmp_path) -> None:
    db_path = tmp_path / "tocmemex.db"
    app = create_app(db_path, tmp_path, default_web_dir())
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert b"<div id=\"app\"" in response.content or b"<script" in response.content
