"""Toc Memex package.

The runtime version comes from the installed distribution so the MCP
handshake, backups and a future ``--version`` all report the same number as
``pyproject.toml``. The fallback covers a source checkout that was never
installed; it must be bumped with ``pyproject.toml``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("toc-memex")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.2.0"
