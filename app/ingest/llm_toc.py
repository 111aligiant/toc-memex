"""LLM-assisted printed-TOC parsing, ported from PageIndex Classic.

This module ports the prompt text and control flow of these PageIndex
(VectifyAI/PageIndex, MIT) functions from ``pageindex/page_index_classic.py``:

- ``toc_detector_single_page`` (per-page "is this a TOC page" detection)
- ``find_toc_pages`` (the run logic: stop at the first non-TOC page)

as part of the LLM TOC proposal layer consumed by ``app.ingest.toc``.
Attribution details in ``vendor/pageindex/NOTICE.md``.

Every function here degrades gracefully: an LLM failure is surfaced to the
caller (usually by returning no results / None), and ``build_toc`` falls
back to the rule-based path. No LLM call is made unless the caller checks
``llm_available`` first.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..llm.client import extract_json, llm_complete
from ..ocr.models import OcrBlock, ParsedDocument
from ..textnorm import normalize_text
from .printed_toc import (
    MappingContext,
    TocEntry,
    _printed_key,
    build_mapping_context,
)

# Pages this far into a document are never proposed as TOC pages by the
# LLM detector (cost cap; printed TOCs live at the front).
DEFAULT_CHECK_LIMIT = 30


# ── 纲目 config (prompts / tuning from app.llm.purpose) ──────────────────


def gangmu_config(settings: Settings) -> dict:
    """The effective 纲目 config attached by ``purpose.resolve_runtime``.

    Empty means shipped defaults — bench runs, tests and direct CLI calls
    never resolve a purpose config and keep the behaviour they had.
    """
    return getattr(settings, "tocmemex_gangmu", None) or {}


def gangmu_available(settings: Settings) -> bool:
    """True when the endpoint is configured AND 纲目 is switched on."""
    from ..llm.client import llm_available

    return gangmu_config(settings).get("enabled", True) and llm_available(settings)


def _prompt(settings: Settings, key: str, default: str) -> str:
    prompts = gangmu_config(settings).get("prompts") or {}
    return prompts.get(key) or default


def _render(settings: Settings, key: str, default: str, **values) -> str:
    """Fill one prompt's placeholders, falling back to the shipped template.

    A user prompt missing ``{content}`` is fine (``str.format`` ignores the
    extra kwarg). One with a malformed brace raises — use the shipped text
    for that call instead of failing the ingest without substitutions.
    """
    template = _prompt(settings, key, default)
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return default.format(**values)

# Furniture labels never sent to the LLM (mirrors chunker's skip set plus
# OCR footer page numbers).
_SKIP_LABELS = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "number",
    "doc_title",
}


def _page_body(page) -> str:
    lines: list[str] = []
    ordered = sorted(
        page.blocks,
        key=lambda b: (b.order is None, b.order or 0, b.bbox[1], b.bbox[0]),
    )
    for block in ordered:
        if block.label in _SKIP_LABELS or block.is_empty:
            continue
        text = normalize_text(block.content).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def page_texts_for_llm(parsed: ParsedDocument) -> list[str]:
    """1-based page texts wrapped in ``<physical_index_N>`` markers.

    Index i of the returned list is physical page i+1, matching the PageIndex
    prompt convention so extracted ``<physical_index_X>`` answers convert as
    ``0-based = X - 1``. OCR noise is cleaned with ``normalize_text`` first.
    """
    texts: list[str] = []
    for page in parsed.pages:
        body = _page_body(page)
        n = page.page_index + 1
        texts.append(f"<physical_index_{n}>\n{body}\n<physical_index_{n}>")
    return texts


_TOC_DETECT_PROMPT = """
Your job is to detect if there is a table of content provided in the given text.

Given text:
{content}

return the following JSON format:
{{
    "thinking": <why do you think there is a table of content in the given text>
    "toc_detected": "<yes or no>",
}}

Directly return the final JSON structure. Do not output anything else.
Please note: abstract,summary, notation list, figure list, table list, etc. are not table of contents."""


def detect_toc_pages_llm(
    page_texts: list[str],
    settings: Settings,
    *,
    check_limit: int | None = None,
    doc_id: str = "",
) -> list[int]:
    """0-based indices of the LLM says are TOC pages.

    Only the first ``check_limit`` pages are scanned; once a page answers
    yes, scanning stops at the first following page that answers no (the
    printed TOC is one contiguous run). A failed or unparseable answer
    counts as "no" and never raises. ``check_limit`` defaults to the 纲目
    config, then to ``DEFAULT_CHECK_LIMIT``.
    """
    effective_limit = (
        check_limit
        if check_limit is not None
        else int(gangmu_config(settings).get("check_limit") or DEFAULT_CHECK_LIMIT)
    )
    found: list[int] = []
    seen_yes = False
    for offset, text in enumerate(page_texts[:effective_limit]):
        prompt = _render(settings, "detect", _TOC_DETECT_PROMPT, content=text)
        try:
            answer = extract_json(llm_complete(prompt, settings, doc_id=doc_id))
            detected = str(answer.get("toc_detected", "no")).lower()
        except (ValueError, KeyError, TypeError, AttributeError):
            # AttributeError: a non-dict answer (array/bare string) from
            # extract_json counts as "no", like any other parse failure.
            detected = "no"
        if detected == "yes":
            found.append(offset)
            seen_yes = True
        elif seen_yes:
            break
    return found


# --- TOC entry extraction (PageIndex toc_transformer port) ---

_TOC_TRANSFORM_PROMPT = """
You are given a table of contents, Your job is to transform the whole table of content into a JSON format included table_of_contents.

structure is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc. Unnumbered entries (前言, 附录, Annex A, ...) have structure None.

page is the printed page number shown at the end of the entry (an Arabic number, a Roman numeral, or a chapter-local label like "14-2"), or None when the entry shows no page number.

The response should be in the following JSON format:
{
"table_of_contents": [
    {
        "structure": <structure index, "x.x.x" or None> (string),
        "title": <title of the section>,
        "page": <page number or None>,
    },
    ...
    ],
}
You should transform the full table of contents in one go.
Directly return the final JSON structure, do not output anything else. """

# Dot leaders: 5+ consecutive dots (fullwidth or halfwidth, possibly
# space-separated) between a title and its page number.
_DOT_LEADERS = re.compile(r"(?:\.{5,}|．{5,}|(?:\. ){5,}\.?)")

_LLM_ENTRY_CONFIDENCE = 0.85


def _normalize_toc_text(text: str) -> str:
    """Clean OCR noise and collapse dot leaders before the prompt."""
    text = normalize_text(text)
    return _DOT_LEADERS.sub(" : ", text)


def _structure_to_level(structure: object) -> int:
    if not structure:
        return 1
    parts = str(structure).strip().split(".")
    return max(1, len(parts))


def _clean_printed_page(raw: object, page_count: int | None) -> str | None:
    """Normalize a printed page token; invalid or out-of-range -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    key = _printed_key(text)
    if key is None:
        return None
    if key.startswith("n:"):
        number = int(key[2:])
        if page_count is not None and number > page_count:
            return None
    return text


def extract_toc_entries_llm(
    toc_page_texts: list[str],
    settings: Settings,
    *,
    page_count: int | None = None,
    doc_id: str = "",
) -> list[TocEntry]:
    """Transform printed-TOC page text into TocEntry list (evidence="llm").

    Ported from PageIndex ``toc_transformer``: one call turns the whole TOC
    into structured JSON. Entries with an invalid or out-of-range printed
    page keep ``printed_page=None`` (never guessed).
    """
    content = "\n".join(_normalize_toc_text(text) for text in toc_page_texts)
    prompt = (
        _prompt(settings, "transform", _TOC_TRANSFORM_PROMPT)
        + "\n Given table of contents\n:"
        + content
    )
    try:
        payload = extract_json(llm_complete(prompt, settings, doc_id=doc_id))
    except (ValueError, AttributeError, TypeError):
        return []
    if isinstance(payload, dict):
        items = payload.get("table_of_contents", [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    entries: list[TocEntry] = []
    for order, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        structure = item.get("structure")
        heading_num = str(structure).strip() if structure else None
        # Keep the number prefix like the rule path does ("8.3 方法说明");
        # display, heading_path and body-heading matching all compare
        # number-prefixed strings.
        if heading_num and not title.startswith(heading_num):
            title = f"{heading_num} {title}"
        entries.append(
            TocEntry(
                title=title,
                heading_num=heading_num,
                level=_structure_to_level(structure),
                printed_page=_clean_printed_page(item.get("page"), page_count),
                page_index=None,
                confidence=_LLM_ENTRY_CONFIDENCE,
                is_master=True,
                order=order,
                evidence="llm",
            )
        )
    return entries


# --- Verify + fix loop (PageIndex verify_toc / fix_incorrect_toc port) ---

_VERIFY_PROMPT = """
Your job is to check if the given section appears or starts in the given page_text.

Note: do fuzzy matching, ignore any space inconsistency in the page_text.

The given section title is {title}.
The given page_text is:
{page_text}

Reply format:
{{
    "thinking": <why do you think the section appears or starts in the page_text>
    "answer": "yes or no" (yes if the section appears or starts in the page_text, no otherwise)
}}
Directly return the final JSON structure. Do not output anything else."""

_FIX_PROMPT = """
You are given a section title and several pages of a document, your job is to find the physical index of the start page of the section in the partial document.

The provided pages contains tags like <physical_index_X> and </physical_index_X> to indicate the physical location of the page X.

Reply in a JSON format:
{{
    "thinking": <explain which page, started and closed by <physical_index_X>, contains the start of this section>,
    "physical_index": "<physical_index_X>" (keep the format)
}}
Directly return the final JSON structure. Do not output anything else."""

_VERIFY_SAMPLE_CAP = 10
_FIX_MAX_ATTEMPTS = 2
_FIXED_CONFIDENCE = 0.75

_PHYSICAL_INDEX_RE = re.compile(r"<physical_index_(\d+)>")


def verify_entries_llm(
    entries: list[TocEntry],
    page_texts: list[str],
    settings: Settings,
    *,
    sample: int | None = None,
    doc_id: str = "",
) -> list[int]:
    """Indices of entries whose title is not on their claimed page.

    Ported from PageIndex ``verify_toc`` / ``check_title_appearance``:
    each sampled entry gets one LLM call over its claimed page. Entries
    without a mapped page are skipped (nothing to verify). Unparseable
    answers count as "no" (suspicious). ``sample`` defaults to the 纲目
    config, then to ``_VERIFY_SAMPLE_CAP``.
    """
    effective_sample = (
        sample
        if sample is not None
        else int(gangmu_config(settings).get("verify_sample") or _VERIFY_SAMPLE_CAP)
    )
    candidates = [
        index
        for index, entry in enumerate(entries)
        if entry.page_index is not None and 0 <= entry.page_index < len(page_texts)
    ]
    step = (
        max(1, len(candidates) // max(1, effective_sample))
        if len(candidates) > effective_sample
        else 1
    )
    sampled = (
        candidates[::step][:effective_sample]
        if len(candidates) > effective_sample
        else candidates
    )

    bad: list[int] = []
    for index in sampled:
        entry = entries[index]
        prompt = _render(
            settings,
            "verify",
            _VERIFY_PROMPT,
            title=entry.title,
            page_text=page_texts[entry.page_index],
        )
        try:
            answer = extract_json(llm_complete(prompt, settings, doc_id=doc_id))
            verdict = str(answer.get("answer", "no")).lower()
        except (ValueError, AttributeError, TypeError):
            verdict = "no"
        if verdict != "yes":
            bad.append(index)
    return bad


def _fix_window(
    entries: list[TocEntry],
    bad_indices: list[int],
    window_end_hint: int | None = None,
) -> dict[int, tuple[int, int]]:
    """For each bad entry, the (start, end) 0-based page window to search.

    Bounded by the nearest neighbours that hold a mapped page — the same
    neighbourhood rule PageIndex's ``fix_incorrect_toc`` uses.
    """
    windows: dict[int, tuple[int, int]] = {}
    for index in bad_indices:
        prev_page = 0
        for other in range(index - 1, -1, -1):
            if entries[other].page_index is not None:
                prev_page = entries[other].page_index
                break
        next_page = -1
        for other in range(index + 1, len(entries)):
            if entries[other].page_index is not None:
                next_page = entries[other].page_index
                break
        # No later mapped neighbour (trailing sections, appendices) and no
        # earlier one either: extend to the full remaining range, like
        # PageIndex's fix_incorrect_toc does with end_index — a one-page
        # window makes trailing sections systematically unfixable.
        if next_page >= 0:
            end = next_page
        elif window_end_hint is not None:
            end = window_end_hint
        else:
            end = prev_page
        windows[index] = (prev_page, max(prev_page, end))
    return windows


def fix_entries_llm(
    entries: list[TocEntry],
    page_texts: list[str],
    bad_indices: list[int],
    settings: Settings,
    *,
    max_attempts: int | None = None,
    doc_id: str = "",
) -> list[TocEntry]:
    """Return a new list with bad entries re-located, or left unplaced.

    Ported from PageIndex ``single_toc_item_index_fixer``: the entry title
    plus the window pages go to the LLM, which answers with a
    ``<physical_index_X>`` marker. An answer outside the window, or any
    failure after ``max_attempts``, leaves the entry unplaced
    (``page_index=None``) — never guessed. ``max_attempts`` defaults to the
    纲目 config, then to ``_FIX_MAX_ATTEMPTS``.
    """
    effective_attempts = (
        max_attempts
        if max_attempts is not None
        else int(gangmu_config(settings).get("fix_attempts") or _FIX_MAX_ATTEMPTS)
    )
    fix_prompt = _prompt(settings, "fix", _FIX_PROMPT)
    windows = _fix_window(entries, bad_indices, window_end_hint=len(page_texts) - 1)
    fixed = list(entries)
    for index in bad_indices:
        start, end = windows[index]
        if end < start:
            end = start
        window_texts = [
            text for text in page_texts[start : end + 1]
        ]
        if not window_texts:
            fixed[index] = _unplace(entries[index])
            continue
        prompt = fix_prompt + "\nSection Title:\n" + entries[index].title
        prompt += "\nDocument pages:\n" + "\n".join(window_texts)
        located: int | None = None
        for _ in range(max(1, effective_attempts)):
            try:
                answer = extract_json(llm_complete(prompt, settings, doc_id=doc_id))
                raw = str(answer.get("physical_index") or "")
                match = _PHYSICAL_INDEX_RE.match(raw.strip())
            except Exception:  # LLMError, ValueError, shape errors: one failed try
                continue
            if not match:
                continue
            physical = int(match.group(1)) - 1  # 1-based marker → 0-based
            if start <= physical <= end:
                located = physical
                break
        if located is None:
            fixed[index] = _unplace(entries[index])
        else:
            replacement = entries[index]
            fixed[index] = TocEntry(
                title=replacement.title,
                heading_num=replacement.heading_num,
                level=replacement.level,
                printed_page=replacement.printed_page,
                page_index=located,
                confidence=_FIXED_CONFIDENCE,
                x0=replacement.x0,
                is_master=replacement.is_master,
                order=replacement.order,
                evidence=replacement.evidence,
            )
    return fixed


def _unplace(entry: TocEntry) -> TocEntry:
    return TocEntry(
        title=entry.title,
        heading_num=entry.heading_num,
        level=entry.level,
        printed_page=entry.printed_page,
        page_index=None,
        confidence=min(entry.confidence, 0.5),
        x0=entry.x0,
        is_master=entry.is_master,
        order=entry.order,
        evidence=entry.evidence,
    )


# --- Proposal assembly (consumed by app.ingest.toc.build_toc) ---

def detect_printed_toc_llm(
    parsed: ParsedDocument, settings: Settings, doc_id: str = "",
    *, log_doc_id: str = "",
) -> "PrintedToc | None":
    """LLM printed-TOC proposal as a PrintedToc, or None on any failure.

    TOC page indices come from the rule detector first (free); only when it
    finds nothing and the LLM is available does the LLM detector run. Entries
    are merged master-first (the earliest page mentioning 2+ chapters owns
    level-1 roots), page-mapped via the shared mapping context, verified and
    repaired by the LLM loop, then deduped — the same post-processing the
    rule path applies to its own entries.

    Results are cached under ``data/llm/<doc_id>/toc.json``; with ``doc_id``
    given, a cache hit short-circuits before any LLM call and a fresh
    result is written back. ``log_doc_id`` only tags the call log — the
    bench path passes it while keeping ``doc_id=""`` so it never touches
    the cache but the usage view still shows which document paid.
    """
    from ..llm.client import LLMError
    from .printed_toc import (
        PrintedToc,
        _chapter_keys,
        _collect_toc_pages,
        _dedupe_entries,
        _map_entries,
        _normalize_title,
        heading_sort_key,
        toc_page_indices as rule_toc_page_indices,
    )

    if not gangmu_available(settings):
        return None
    log_doc_id = log_doc_id or doc_id
    if doc_id:
        cached = load_llm_toc(
            settings,
            doc_id,
            model=settings.tocmemex_llm_model,
            page_count=parsed.page_count,
        )
        if cached is not None:
            return cached
    try:
        toc_pages = _collect_toc_pages(parsed.pages)
        toc_indices = {page_index for page_index, _ in toc_pages}
        if not toc_indices:
            page_texts = page_texts_for_llm(parsed)
            toc_indices = set(
                detect_toc_pages_llm(page_texts, settings, doc_id=log_doc_id)
            )
            if toc_indices:
                toc_pages = [
                    (page_index, [])
                    for page_index in sorted(toc_indices)
                ]
        if not toc_indices:
            return None

        # Text of the TOC pages for the LLM extraction (OCR blocks, cleaned).
        toc_texts = []
        for page_index, _ in toc_pages:
            if 0 <= page_index < len(parsed.pages):
                toc_texts.append(_page_body(parsed.pages[page_index]))
        if not toc_texts:
            return None

        entries = extract_toc_entries_llm(
            toc_texts, settings, page_count=parsed.page_count, doc_id=log_doc_id
        )
        if len(entries) < 2:
            return None

        # Every chapter-keyed entry is a master: the extraction is one call
        # over the whole printed TOC, so entries carry no source-page
        # attribution and the rule path's "earliest multi-chapter page owns
        # roots" heuristic cannot be applied per entry. The rule path keeps
        # its own heuristic; arbitration compares the two resulting trees.
        master_entries = [entry for entry in entries if entry.chapter is not None]
        detail_entries = [entry for entry in entries if entry.chapter is None]
        for entry in master_entries:
            entry.is_master = True

        single_chapter_pages = [
            (page_index, next(iter(_chapter_keys(page_entries))))
            for page_index, page_entries in toc_pages
            if len(_chapter_keys(page_entries)) == 1 and page_entries
        ]
        single_chapter_pages.sort()
        context = build_mapping_context(
            parsed.pages, toc_indices, single_chapter_pages
        )

        mapped = _map_entries(
            master_entries + detail_entries,
            parsed.pages,
            context.page_map,
            context.body_headings,
            chapter_toc_ranges=context.chapter_toc_ranges,
        )

        # LLM verify+fix replaces the rule path's _repair_heading_numbers.
        page_texts = page_texts_for_llm(parsed)
        bad = verify_entries_llm(mapped, page_texts, settings, doc_id=log_doc_id)
        if bad:
            mapped = fix_entries_llm(mapped, page_texts, bad, settings, doc_id=log_doc_id)

        mapped = [
            entry for entry in mapped if entry.page_index is not None or entry.is_master
        ]
        if len(mapped) < 2:
            return None

        seen: set[tuple[str, int]] = set()
        unique: list[TocEntry] = []
        for entry in sorted(
            mapped,
            key=lambda e: (
                e.page_index or 0,
                heading_sort_key(e.heading_num),
                0 if e.is_master else 1,
                e.order,
            ),
        ):
            key = (_normalize_title(entry.title), entry.page_index or 0)
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        unique = _dedupe_entries(unique)
        if len(unique) < 2:
            return None
        result = PrintedToc(
            entries=unique,
            source_page=toc_pages[0][0] if toc_pages else 0,
            confidence=0.8,
        )
        if doc_id:
            try:
                save_llm_toc(settings, doc_id, result, page_count=parsed.page_count)
            except OSError:
                pass  # cache write failure never fails ingestion
        return result
    except LLMError:
        return None
    except Exception:
        # Any unexpected shape/network error must not break ingestion; the
        # caller falls back to the rule proposal.
        return None


# --- Result cache (reindex must not re-pay for LLM calls) ---

def _llm_toc_cache_path(settings: Settings, doc_id: str) -> Path:
    return settings.ocr_dir.parent / "llm" / doc_id / "toc.json"


def _prompt_signature(settings: Settings) -> str:
    """Short fingerprint of the four effective prompts.

    The cached TOC was produced by these prompts; an edited prompt must
    invalidate the cache, otherwise "改了提示词 → 重新入库" silently reuses
    the old proposal. Shipping new defaults invalidates too, which is
    intended.
    """
    import hashlib

    effective = {
        "detect": _prompt(settings, "detect", _TOC_DETECT_PROMPT),
        "transform": _prompt(settings, "transform", _TOC_TRANSFORM_PROMPT),
        "verify": _prompt(settings, "verify", _VERIFY_PROMPT),
        "fix": _prompt(settings, "fix", _FIX_PROMPT),
    }
    payload = json.dumps(effective, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def save_llm_toc(
    settings: Settings, doc_id: str, toc: "PrintedToc", *, page_count: int
) -> Path:
    """Persist one LLM TOC proposal under ``data/llm/<doc_id>/toc.json``."""
    from dataclasses import asdict

    path = _llm_toc_cache_path(settings, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "model": settings.tocmemex_llm_model,
            "page_count": page_count,
            "prompt_sig": _prompt_signature(settings),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        },
        "toc": asdict(toc),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_llm_toc(
    settings: Settings, doc_id: str, *, model: str, page_count: int
) -> "PrintedToc | None":
    """Load a cached LLM TOC; None on miss, mismatch, or corrupt file.

    A cache produced by a different model, page count or prompt text is
    stale (page mapping depends on the document's page count; the proposal
    depends on the prompts) and is ignored.
    """
    from dataclasses import dataclass, field
    from .printed_toc import PrintedToc, TocEntry

    path = _llm_toc_cache_path(settings, doc_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("_meta") or {}
        if meta.get("model") != model or meta.get("page_count") != page_count:
            return None
        if meta.get("prompt_sig") != _prompt_signature(settings):
            return None
        toc_data = payload.get("toc") or {}
        entries = [
            TocEntry(**entry_data) for entry_data in toc_data.get("entries", [])
        ]
        return PrintedToc(
            entries=entries,
            source_page=toc_data.get("source_page", 0),
            confidence=toc_data.get("confidence", 0.8),
        )
    except (ValueError, KeyError, TypeError, OSError):
        return None


def build_llm_toc_proposal(
    doc_id: str, parsed: ParsedDocument, settings: Settings
) -> list:
    """LLM TOC proposal as node rows, bypassing the disk cache, raising on failure.

    Bench-only entry point: unlike ``build_toc`` (which swallows every LLM
    failure and arbitrates silently) this one never consults or writes
    ``data/llm`` — each bench run must actually exercise its endpoint — and
    lets LLM errors propagate so the caller can record them. Node-shaping
    mirrors ``toc._from_printed_toc_llm``; imported lazily to avoid a cycle.
    """
    from .toc import _fix_page_ranges, _nodes_from_printed_entries

    printed = detect_printed_toc_llm(
        parsed, settings, doc_id="", log_doc_id=doc_id
    )
    if printed is None or len(printed.entries) < 2:
        return []
    nodes = _nodes_from_printed_entries(
        doc_id, printed.entries, parsed.page_count, printed.confidence
    )
    for node in nodes:
        node.source = "printed_toc_llm"
    return _fix_page_ranges(nodes, parsed.page_count)
