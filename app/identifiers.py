"""Generic document-identifier helpers.

Many documents carry a designation-like identifier: ``ACME 2024``,
``ACME/TS 100``, ``RPT-17``. The library uses that identifier for three
generic jobs: routing a query to its document, detecting duplicate
documents, and suggesting topic collections. None of those jobs is
specific to a document domain, so the patterns live here instead of being
repeated in every module.
"""

from __future__ import annotations

import re

# Ordinary document words that look like a designation + number but are
# references, not identifiers ("Chapter 12", "Table 3"). They must not
# route a query to a document, invent a duplicate key or create a family.
_RESERVED_PREFIXES = frozenset(
    {
        "ANNEX",
        "APPENDIX",
        "CHAPTER",
        "FIGURE",
        "PAGE",
        "PART",
        "SECTION",
        "TABLE",
    }
)

_RESERVED_LOOKAHEAD = "|".join(sorted(_RESERVED_PREFIXES))

# A short Latin designation, optionally with a second short Latin token
# after a separator: ACME, ACME/TS, ACME-TS, ACME_TS.
_DESIGNATION_PREFIX = (
    rf"(?!(?:{_RESERVED_LOOKAHEAD})\b)"
    r"[A-Za-z]{1,8}(?:[/∕／+_.\-][A-Za-z]{1,4})?"
)

DOCUMENT_ID_RE = re.compile(
    rf"\b{_DESIGNATION_PREFIX}\s*[/∕／]?\s*[-–—]?\s*\d{{2,6}}(?:[.\-‑/]\d+)*[A-Za-z]?"
    rf"|\b{_DESIGNATION_PREFIX}\s*[/∕／]?\s*[-–—]\s*\d+[A-Za-z0-9\-]*",
    re.IGNORECASE,
)

_TRANSLATION = str.maketrans(
    {
        "∕": "/",
        "／": "/",
        "‑": "-",
        "–": "-",
        "—": "-",
        "－": "-",
        "．": ".",
    }
)

_KEY_RE = re.compile(
    r"([A-Z]{1,12})[\s\-]*(\d+(?:\.\d+)*)([A-Z]?)",
    re.IGNORECASE,
)

_FAMILY_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z\s_/／∕⁄+\-]{0,8}?)\s*(\d+)", re.UNICODE
)

_BARE_RE = re.compile(
    rf"^\s*{_DESIGNATION_PREFIX}\s*[/∕／]?\s*[-–—]?\s*\d{{1,6}}"
    r"(?:[.\-‑/]\d+)*[A-Za-z]?\s*$",
    re.IGNORECASE,
)


def normalize_identifier(value: str | None) -> str:
    """Collapse spelling variants to one comparable key.

    Spaces, underscores, slashes and plus signs are dropped; dots and
    dashes stay so a leading number can still be told apart from a longer
    one (``2423.1`` vs ``2423.17``).
    """
    text = (value or "").upper().translate(_TRANSLATION)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9A-Z.\-]", "", text)
    # A separator between a letter prefix and its number is spelling, not a
    # digit boundary: ACME-100, ACME 100 and ACME/100 are the same key.
    # Hyphens between digits stay, so a year suffix is still distinguishable
    # from a longer section number.
    text = re.sub(r"(?<=[A-Z])-(?=\d)", "", text)
    return re.sub(r"(?<=\d)-(?=[A-Z])", "", text)


def document_key(title: str | None) -> str:
    """The normalized identifier of a document title, or ``""``.

    Used for duplicate detection: ``ACME 100``, ``ACME-100`` and
    ``ACME/100`` all yield ``ACME100``.
    """
    text = normalize_identifier(title)
    match = _KEY_RE.search(text)
    if match is None:
        return ""
    prefix = match.group(1).upper()
    if prefix in _RESERVED_PREFIXES:
        return ""
    return f"{prefix}{match.group(2)}{match.group(3).upper()}"


# Backwards-compatible descriptive alias for callers that name the input.
identifier_key = document_key


def family_key(title: str | None, filename: str | None = None) -> str | None:
    """Group key for auto-partitioning a numbered document library.

    Returns the designation plus its first number, e.g. ``ACME 100``;
    ``None`` when the name carries no designation, because inventing a
    collection from an arbitrary title is worse than leaving it unfiled.
    """
    for value in (title or "", filename or ""):
        match = _FAMILY_RE.match(value)
        if match is None:
            continue
        prefix = re.sub(r"[^A-Za-z]", "", match.group(1)).upper()
        if not prefix or prefix in _RESERVED_PREFIXES:
            continue
        return f"{prefix} {match.group(2)}"
    return None


def is_bare_designation(text: str | None) -> bool:
    """True when a line contains only a document identifier.

    Running headers and address blocks often repeat a document number on
    every page; a TOC or PDF outline entry made of nothing else is noise.
    A heading that adds title text is not matched.
    """
    return bool(_BARE_RE.match((text or "").strip()))
