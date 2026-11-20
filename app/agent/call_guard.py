"""Idempotent replay for identical tool calls.

The built-in console loop and external MCP sessions share one policy:

- a successful call runs once per run/connection; repeating the same
  ``(tool, args)`` returns the first result with a note instead of re-running;
- a failed call is never cached, so a retry reaches the tool and the real
  error surfaces instead of being masked as a duplicate.

Only ``dict`` payloads are cached: a bare list cannot carry the replay note
without changing its shape.

This module deliberately imports nothing from ``app`` so both the console
(``app.agent.tools``) and the MCP wrapper (``app.mcp_server`` /
``app.agent.external``) can use it without an import cycle.
"""

from __future__ import annotations

import copy
import json
from collections import OrderedDict
from typing import Any

REPLAY_NOTE = (
    "该调用此前已成功执行，以下是上次的结果（本次未重复执行）；"
    "需要新信息时请换参数或工具。"
)


def call_key(tool: str, args: dict | None) -> tuple[str, str]:
    canonical = json.dumps(
        args or {}, sort_keys=True, ensure_ascii=False, default=str
    )
    return (tool, canonical)


def _copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class CallGuard:
    """Successful ``(tool, args)`` results for one run/connection."""

    def __init__(self, max_entries: int | None = None) -> None:
        self._entries: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._max = max_entries

    def replay(self, tool: str, args: dict | None) -> Any | None:
        key = call_key(tool, args)
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return _copy(self._entries[key])

    def remember(self, tool: str, args: dict | None, result: Any) -> None:
        if not isinstance(result, dict) or result.get("error"):
            return
        key = call_key(tool, args)
        self._entries[key] = _copy(result)
        self._entries.move_to_end(key)
        if self._max is not None:
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def reset(self) -> None:
        self._entries.clear()


_MAX_GUARDS = 128
_REGISTRY: OrderedDict[tuple[str, str], CallGuard] = OrderedDict()


def guard_for(namespace: str, session_id: str, max_entries: int = 64) -> CallGuard:
    """One guard per (database, MCP connection).

    The database path is part of the key because in-process tests reuse the
    same ``session_id`` against different temp databases.
    """
    key = (namespace, session_id)
    guard = _REGISTRY.get(key)
    if guard is None:
        guard = CallGuard(max_entries=max_entries)
        _REGISTRY[key] = guard
    _REGISTRY.move_to_end(key)
    while len(_REGISTRY) > _MAX_GUARDS:
        _REGISTRY.popitem(last=False)
    return guard


def reset_session(session_id: str) -> None:
    for key in [item for item in _REGISTRY if item[1] == session_id]:
        _REGISTRY.pop(key, None)
