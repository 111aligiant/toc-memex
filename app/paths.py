"""Path-safety helpers for identifiers that become filesystem components.

A document id reaches the disk as a directory name under ``data/ocr/<id>``,
``data/resources/<id>`` and so on. The id comes from a URL path segment, from
an MCP tool argument, or from a request body — none of which is trustworthy.
On POSIX a segment captured with ``[^/]+`` cannot contain a separator, but on
Windows both ``/`` and ``\\`` separate path components, and ``unquote`` turns
``%5c`` back into ``\\``. ``..\\..\\..`` therefore escapes the data directory
and reads (or deletes) arbitrary files.

``is_safe_doc_id`` is the single gate every doc_id → path join must pass: it
rejects separators (both slashes), ``..`` traversal, NUL/control characters
and the reserved Windows device names, so a hostile id can never name a path
outside the intended cache directory.
"""

from __future__ import annotations

import re

# Control characters (C0 and DEL) never belong in a filesystem component and
# are the classic trick to smuggle a NUL past a length check.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Reserved device names on Windows. A component like "CON" or "NUL" (with or
# without an extension) redirects I/O to a device regardless of the directory.
_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def is_safe_doc_id(doc_id: object) -> bool:
    """True when ``doc_id`` can be joined into a path without escaping it.

    Accepts any non-empty string with no path separators (``/`` or ``\\``),
    no parent-directory hop (``.`` / ``..``), no control characters and no
    reserved Windows device name. Real document ids are UUIDs or short slugs,
    so every one of these rejections is a hostile input, not a false positive.
    """
    if not isinstance(doc_id, str) or not doc_id:
        return False
    if doc_id in (".", ".."):
        return False
    if "/" in doc_id or "\\" in doc_id:
        return False
    if _CONTROL_RE.search(doc_id):
        return False
    # ``Path("ocr/CON")`` still opens the CON device on Windows.
    stem = doc_id.split(".", 1)[0].upper()
    if stem in _RESERVED_NAMES:
        return False
    return True
