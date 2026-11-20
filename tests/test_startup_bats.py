"""Smoke tests for the Windows launchers.

They cannot start a server here, but they do catch the failures that make a
launcher useless: wrong line endings (``goto`` stops resolving), a BOM, a
missing ``:run`` label, or the startup steps drifting out of order.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_bat(name: str) -> str:
    raw = (ROOT / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} must not start with a UTF-8 BOM"
    assert b"\r\n" in raw, f"{name} must use CRLF line endings"
    text = raw.decode("utf-8")
    assert "\n" not in text.replace("\r\n", ""), f"{name} must not contain bare LF"
    return text


def test_start_web_initialises_backs_up_then_serves():
    text = _read_bat("start-web.bat")

    init = text.index("app.cli init")
    backup = text.index("scripts\\backup.py")
    serve = text.index("app.web_api")
    assert init < backup < serve, "first launch must initialise, back up, then serve"

    assert "goto :run" in text
    assert "\n:run\r\n" in text, "missing :run label"


def test_start_web_rebuilds_stale_frontend_and_replaces_a_leftover_server():
    text = _read_bat("start-web.bat")

    assert "call :ensure_frontend" in text
    assert "call :check_port" in text
    assert ":ensure_frontend\r\n" in text, "missing ensure_frontend subroutine"
    assert ":check_port\r\n" in text, "missing check_port subroutine"

    serve = text.index("app.web_api --db")
    assert text.index("call :ensure_frontend") < serve, "frontend must be rebuilt before serving"
    assert text.index("call :check_port") < serve, "a leftover server must be handled before serving"

    assert "npm run build" in text
    assert "taskkill /PID" in text


def test_start_inspector_refuses_to_run_without_a_database():
    text = _read_bat("start-inspector.bat")

    assert 'if not exist "data\\tocmemex.db"' in text
    assert "app.inspector" in text
    assert "goto :run" in text
