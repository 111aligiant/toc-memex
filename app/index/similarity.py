"""Near-duplicate detection for ingested documents.

Exact byte duplicates are already caught by ``documents.content_hash``. The
gap this closes is semantic duplicates: the same job posting screenshotted
on two days, or the same standard saved as a re-print — different bytes, same
content. Exact hashing calls those new documents; a human then has to
compare them by hand.

Two signals, cheapest first:

1. ``document_key(title)`` — the document-designation key (``ACME 100.56``).
   Same key means the same document number, which is a strong match.
2. SimHash over ``title + summary``. A 64-bit fingerprint whose Hamming
   distance stays small for near-identical text and grows with divergence.

Both are hints, never a block: the caller reports ``similar_documents`` and
``duplicate_hint`` and lets the human or agent decide. Short texts return
``None`` — a three-word title has too few features for SimHash to mean
anything, and flagging every short document as "similar" would be noise.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from ..identifiers import document_key

# Below this many shingles a SimHash is dominated by hash noise, not content.
_MIN_SHINGLES = 8
# 64-bit Hamming distance under which two documents are "near duplicates".
# 10/64 ≈ 84% bit agreement: tight enough that unrelated postings (measured
# around 38) never match, loose enough that a re-taken screenshot with a few
# OCR differences still does. This is a hint, not a block.
_NEAR_DISTANCE = 10
_SHINGLE = 3


def _tokens(text: str) -> list[str]:
    """Word tokens, using the same segmenter the index uses."""
    import jieba

    return [token for token in jieba.lcut(text or "") if token.strip()]


def _shingles(tokens: list[str], size: int = _SHINGLE) -> list[str]:
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def simhash64(text: str) -> str | None:
    """64-bit SimHash of *text* as 16 hex chars, or None when too short."""
    shingles = _shingles(_tokens(text))
    if len(shingles) < _MIN_SHINGLES:
        return None
    vector = [0] * 64
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            if value >> bit & 1:
                vector[bit] += 1
            else:
                vector[bit] -= 1
    result = 0
    for bit in range(64):
        if vector[bit] > 0:
            result |= 1 << bit
    return f"{result:016x}"


def hamming(left: str | None, right: str | None) -> int:
    if not left or not right:
        return 64
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return 64


def similarity_text(title: str | None, body: str | None) -> str:
    """The text SimHash is computed over: title plus the head of the body."""
    return f"{title or ''}\n{(body or '')[:500]}"


def fingerprint(title: str | None, body: str | None) -> str | None:
    return simhash64(similarity_text(title, body))


async def body_head(db, doc_id: str, limit: int = 500) -> str:
    """The first ``limit`` characters of a document's body.

    ``documents.summary`` is only written by the curator, so it is empty for
    most ingests; the chunks are the only reliable body text here.
    """
    from sqlalchemy import select

    from ..models import Chunk

    rows = (
        (
            await db.execute(
                select(Chunk.content)
                .where(Chunk.doc_id == doc_id)
                .order_by(Chunk.chunk_index)
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    return "".join(rows)[:limit]


async def ensure_fingerprint(db, doc) -> str | None:
    """Return (and persist) a document's SimHash, computing it if missing."""
    if doc.simhash:
        return doc.simhash
    value = fingerprint(doc.title, await body_head(db, doc.id))
    if value:
        doc.simhash = value
    return value


def similar_to(
    *,
    title: str | None,
    body: str | None,
    simhash: str | None,
    candidates: Iterable[dict],
) -> list[dict]:
    """Rank candidate documents by near-duplicate evidence.

    Each candidate is ``{"doc_id", "title", "simhash"}``. Returns matches
    with ``score`` (1.0 for an identical document key, else distance-based),
    ``reason`` and ``strong`` (whether it should raise ``duplicate_hint``).
    """
    own_key = document_key(title or "")
    own_hash = simhash or fingerprint(title, body)
    matches: list[dict] = []
    for candidate in candidates:
        other_key = document_key(candidate.get("title") or "")
        if own_key and other_key and own_key == other_key:
            matches.append(
                {
                    "doc_id": candidate["doc_id"],
                    "title": candidate.get("title") or "",
                    "score": 1.0,
                    "reason": "same document key",
                    "strong": True,
                }
            )
            continue
        distance = hamming(own_hash, candidate.get("simhash"))
        if distance <= _NEAR_DISTANCE:
            matches.append(
                {
                    "doc_id": candidate["doc_id"],
                    "title": candidate.get("title") or "",
                    "score": round(1 - distance / 64, 4),
                    "reason": f"simhash distance {distance}",
                    # Any match inside the near threshold is worth flagging:
                    # the caller shows it as a hint, and the user decides.
                    "strong": True,
                }
            )
    matches.sort(key=lambda item: (-item["score"], item["doc_id"]))
    return matches[:5]
