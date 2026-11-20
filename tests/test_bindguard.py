import pytest

from app.bindguard import check_bind_host, is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "127.12.0.9", "localhost", "LOCALHOST", "::1"])
def test_loopback_hosts_are_allowed(host):
    assert is_loopback_host(host) is True
    check_bind_host(host, allow_nonlocal=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com", ""])
def test_non_loopback_hosts_are_rejected(host):
    assert is_loopback_host(host) is False
    with pytest.raises(ValueError, match="127.0.0.1"):
        check_bind_host(host, allow_nonlocal=False)


def test_non_loopback_is_allowed_when_explicitly_enabled():
    check_bind_host("0.0.0.0", allow_nonlocal=True)


def test_web_main_exits_before_uvicorn(monkeypatch, tmp_path):
    from app import web_api

    called = False

    def fake_create_app(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("app should not be built")

    monkeypatch.setattr(web_api, "create_app", fake_create_app)
    with pytest.raises(SystemExit, match="127.0.0.1"):
        web_api.main(["--host", "0.0.0.0", "--db", str(tmp_path / "x.db")])
    assert called is False


def test_inspector_main_exits_before_server(monkeypatch):
    from app import inspector

    called = False

    def fake_run_server(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("server should not be built")

    monkeypatch.setattr(inspector, "run_server", fake_run_server)
    with pytest.raises(SystemExit, match="127.0.0.1"):
        inspector.main(["--host", "0.0.0.0"])
    assert called is False


def test_mcp_http_main_exits_before_bind(monkeypatch):
    from types import SimpleNamespace

    from app import mcp_server

    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(mcp_server, "server", SimpleNamespace(run=fake_run))
    with pytest.raises(SystemExit, match="127.0.0.1"):
        mcp_server.main(
            ["--transport", "streamable-http", "--host", "0.0.0.0"]
        )
    assert called is False
