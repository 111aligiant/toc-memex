from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

EXPECTED_SCRIPTS = {
    "toc-memex": "app.cli:main",
    "toc-memex-mcp": "app.mcp_server:main",
    "toc-memex-web": "app.web_api:main",
}


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_project_scripts_are_declared() -> None:
    scripts = _load_pyproject()["project"]["scripts"]
    assert scripts == EXPECTED_SCRIPTS


def test_entrypoint_targets_are_importable_mains() -> None:
    for target in EXPECTED_SCRIPTS.values():
        module_name, func_name = target.split(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, func_name)), target


def test_requires_python_allows_312_and_313() -> None:
    spec = _load_pyproject()["project"]["requires-python"]
    bounds = re.findall(r"(>=|<=|>|<)\s*(\d+)\.(\d+)", spec)
    assert bounds, spec

    def allowed(major: int, minor: int) -> bool:
        for op, bound_major, bound_minor in bounds:
            value = (int(bound_major), int(bound_minor))
            current = (major, minor)
            if op == ">=" and not current >= value:
                return False
            if op == ">" and not current > value:
                return False
            if op == "<=" and not current <= value:
                return False
            if op == "<" and not current < value:
                return False
        return True

    assert allowed(3, 12)
    assert allowed(3, 13)
    assert not allowed(3, 11)
    assert not allowed(3, 14)
