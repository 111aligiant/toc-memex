"""LLM call log and token accounting (ADR-0002 §10).

Tokens are read from the endpoint's own ``usage`` field — exact, free, and
no extra dependency. An endpoint that omits ``usage`` is estimated and the
record says ``source: "estimated"`` so the two are never mixed silently.

The log is JSONL, appended with one small ``O_APPEND`` write per call.
``llm_complete`` is synchronous and can run inside an ingest transaction;
opening a second SQLite connection there would fight the write lock, while
an append-only file never does.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

_PREVIEW_CHARS = 800

# Call ids are generated (float timestamps today, uuid4 possible later). The
# id is interpolated into a filesystem path and arrives over HTTP, so it is
# allowed-listed before touching the filesystem — defence in depth beyond
# whatever the route layer does with encoded separators.
_CALL_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    """Rough token count when the endpoint returns no ``usage``.

    Chinese runs ~1.5 chars/token, Latin ~4 chars/token. Only a fallback;
    every record using it is marked ``estimated``.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    other = len(text) - cjk
    return max(1, round(cjk / 1.5 + other / 4))


def usage_from_response(payload: dict, prompt: str, content: str) -> dict:
    """Read the endpoint's ``usage``; estimate only when it is absent."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict) and (
        usage.get("prompt_tokens") is not None
        or usage.get("completion_tokens") is not None
    ):
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        return {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
            "source": "api_usage",
        }
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(content)
    return {
        "prompt": prompt_tokens,
        "completion": completion_tokens,
        "total": prompt_tokens + completion_tokens,
        "source": "estimated",
    }


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "llm" / "calls.jsonl"


def detail_path(data_dir: Path, call_id: str) -> Path:
    """Full prompt + response for one call, written when log_mode is ``full``.

    ``calls.jsonl`` keeps only 800-char previews so the dashboard stays
    cheap; the complete input and output live here, one JSON per call, so a
    specific call can be exported verbatim afterwards. Records logged before
    this file existed have no detail and the export endpoint says so.
    """
    if not _CALL_ID_RE.fullmatch(call_id or ""):
        raise ValueError(f"invalid call id: {call_id!r}")
    return Path(data_dir) / "llm" / "calls" / f"{call_id}.json"


def read_call_detail(data_dir: Path, call_id: str) -> dict | None:
    """The full record for one call, or None when it was not captured.

    Falls back to the JSONL line (previews only) when no detail file exists,
    so even a pre-detail call exports something rather than 404-ing blindly.
    An id that fails validation returns None — same response as "not found",
    never a filesystem error.
    """
    try:
        detail = detail_path(data_dir, call_id)
    except ValueError:
        return None
    try:
        if detail.exists():
            return json.loads(detail.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for record in _read_calls_matching(data_dir):
        if record.get("id") == call_id:
            return record
    return None


def _preview(value: str | None, mode: str) -> str | None:
    if mode == "off":
        return None
    if not value:
        return ""
    if mode == "meta":
        return f"<{len(value)} chars>"
    return value[:_PREVIEW_CHARS]


def append_call(
    data_dir: Path,
    *,
    call_site: str,
    endpoint: str,
    model: str,
    prompt: str,
    content: str | None,
    tokens: dict,
    elapsed_ms: int,
    status: str = "ok",
    error: str | None = None,
    retries: int = 0,
    run_id: str | None = None,
    doc_id: str | None = None,
    tools: list[str] | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str | None = None,
    log_mode: str = "full",
) -> None:
    """Append one call record. Never raises: logging must not break a call."""
    if log_mode == "off":
        return
    record = {
        "id": f"{datetime.now(timezone.utc).timestamp():.6f}",
        "run_id": run_id,
        "doc_id": doc_id,
        "ts": _now(),
        "call_site": call_site,
        "endpoint": endpoint,
        "model": model,
        "request": {
            "prompt_chars": len(prompt or ""),
            "prompt_preview": _preview(prompt, log_mode),
            "tools": tools or [],
        },
        "response": {
            "content_chars": len(content or ""),
            "content_preview": _preview(content, log_mode),
            "tool_calls": tool_calls or [],
            "finish_reason": finish_reason,
        },
        "tokens": tokens,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "error": error,
        "retries": retries,
    }
    try:
        path = log_path(data_dir)
        # The log can hold up to 800 characters of document text per call, so
        # it is owner-only: directory 0700, file 0600. On Windows these mode
        # bits are advisory; the file inherits the profile directory's ACL.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        _tighten(path)
        if log_mode == "full":
            _write_detail(data_dir, record, prompt, content)
    except OSError:
        return


def _write_detail(data_dir: Path, record: dict, prompt: str, content: str | None) -> None:
    """Full verbatim input/output for one call; never breaks the main log.

    Includes the exact wire prompt (joined message contents) and response,
    plus the metadata line so the file is self-describing. Best effort:
    a failed detail write still leaves the JSONL record intact.
    """
    detail = {
        "id": record["id"],
        "run_id": record.get("run_id"),
        "doc_id": record.get("doc_id"),
        "ts": record.get("ts"),
        "call_site": record.get("call_site"),
        "endpoint": record.get("endpoint"),
        "model": record.get("model"),
        "tokens": record.get("tokens"),
        "elapsed_ms": record.get("elapsed_ms"),
        "status": record.get("status"),
        "error": record.get("error"),
        "retries": record.get("retries"),
        "request": {"prompt": prompt or "", "tools": record["request"]["tools"]},
        "response": {
            "content": content,
            "tool_calls": record["response"]["tool_calls"],
        },
    }
    path = detail_path(data_dir, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(detail, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)


def _tighten(path: Path) -> None:
    """Best effort: a file created by older code may still be 0644."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_calls_matching(
    data_dir: Path,
    *,
    doc_id: str | None = None,
    run_id: str | None = None,
    call_site: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """All records passing the filters, oldest first (shared filter logic)."""
    path = log_path(data_dir)
    if not path.exists():
        return []
    since_dt = _parse_since(since)
    matched: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if doc_id and record.get("doc_id") != doc_id:
                continue
            if run_id and record.get("run_id") != run_id:
                continue
            if call_site and record.get("call_site") != call_site:
                continue
            if status and record.get("status") != status:
                continue
            if since_dt is not None and not _at_or_after(record.get("ts"), since_dt):
                continue
            matched.append(record)
    return matched


def read_calls(
    data_dir: Path,
    *,
    doc_id: str | None = None,
    run_id: str | None = None,
    call_site: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Newest-first call records, filtered in memory.

    The file is small (one line per call) and append-only, so filtering by
    reading it backwards is simpler than an index and cannot drift.
    ``since`` is an ISO-8601 timestamp compared as a string: every record is
    written with a UTC ``+00:00`` offset, so lexicographic order is time order.
    """
    matched = _read_calls_matching(
        data_dir,
        doc_id=doc_id,
        run_id=run_id,
        call_site=call_site,
        status=status,
        since=since,
    )
    matched.reverse()
    return matched[offset : offset + limit]


def count_calls(
    data_dir: Path,
    *,
    doc_id: str | None = None,
    run_id: str | None = None,
    call_site: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> int:
    """How many records the same filters would return (for paging UIs)."""
    return len(_read_calls_matching(
        data_dir,
        doc_id=doc_id,
        run_id=run_id,
        call_site=call_site,
        status=status,
        since=since,
    ))


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp is read as UTC: every record is written in UTC, so
    # "2026-11-17T07:00:00" should mean 07:00 UTC, not the server's zone.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _at_or_after(ts: str | None, since: datetime) -> bool:
    """Compare a record timestamp to ``since`` across timezone offsets."""
    if not ts:
        return False
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= since


def usage_summary(data_dir: Path) -> dict:
    """Totals, per-day, per-run and per-endpoint aggregates over logged calls."""
    path = log_path(data_dir)
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}
    by_day: dict[str, dict] = {}
    by_run: dict[str, dict] = {}
    by_endpoint: dict[str, dict] = {}
    if not path.exists():
        return {"total": totals, "by_day": [], "by_run": [], "by_endpoint": []}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            tokens = record.get("tokens") or {}
            prompt_tokens = int(tokens.get("prompt") or 0)
            completion_tokens = int(tokens.get("completion") or 0)
            is_error = record.get("status") != "ok"
            totals["calls"] += 1
            totals["tokens_in"] += prompt_tokens
            totals["tokens_out"] += completion_tokens
            totals["errors"] += 1 if is_error else 0
            day = (record.get("ts") or "")[:10]
            bucket = by_day.setdefault(day, {"day": day, "calls": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += prompt_tokens + completion_tokens
            run_id = record.get("run_id") or "unassigned"
            run = by_run.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "doc_id": record.get("doc_id"),
                    "calls": 0,
                    "tokens": 0,
                    "errors": 0,
                },
            )
            run["calls"] += 1
            run["tokens"] += prompt_tokens + completion_tokens
            run["errors"] += 1 if is_error else 0
            endpoint = record.get("endpoint") or "unknown"
            ep = by_endpoint.setdefault(
                endpoint,
                {"endpoint": endpoint, "model": record.get("model") or "",
                 "calls": 0, "tokens": 0, "errors": 0},
            )
            ep["calls"] += 1
            ep["tokens"] += prompt_tokens + completion_tokens
            ep["errors"] += 1 if is_error else 0
    return {
        "total": totals,
        "by_day": sorted(by_day.values(), key=lambda item: item["day"]),
        "by_run": sorted(by_run.values(), key=lambda item: -item["tokens"]),
        "by_endpoint": sorted(by_endpoint.values(), key=lambda item: -item["tokens"]),
    }


def export_calls(
    data_dir: Path,
    *,
    doc_id: str | None = None,
    run_id: str | None = None,
    call_site: str | None = None,
    status: str | None = None,
    since: str | None = None,
    format: str = "jsonl",
) -> tuple[str, str, str]:
    """Whole (filtered) log as a downloadable export; returns (body, media, filename).

    Same filters as ``read_calls`` but uncapped — an export that silently
    stops at a page size is not an export. ``jsonl`` streams the records
    verbatim; ``csv`` flattens to one row per call for spreadsheets.
    """
    path = log_path(data_dir)
    if not path.exists():
        return "", "text/plain", "llm-calls-empty.txt"
    records = _read_calls_matching(
        data_dir,
        doc_id=doc_id,
        run_id=run_id,
        call_site=call_site,
        status=status,
        since=since,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        import csv as _csv
        import io as _io

        buffer = _io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow([
            "ts", "call_site", "endpoint", "model", "run_id", "doc_id",
            "status", "retries", "elapsed_ms", "prompt_tokens",
            "completion_tokens", "total_tokens", "token_source",
            "prompt_chars", "content_chars", "error",
        ])
        for record in records:
            tokens = record.get("tokens") or {}
            request = record.get("request") or {}
            response = record.get("response") or {}
            writer.writerow([
                record.get("ts"), record.get("call_site"), record.get("endpoint"),
                record.get("model"), record.get("run_id"), record.get("doc_id"),
                record.get("status"), record.get("retries"), record.get("elapsed_ms"),
                tokens.get("prompt"), tokens.get("completion"), tokens.get("total"),
                tokens.get("source"), request.get("prompt_chars"),
                response.get("content_chars"), record.get("error") or "",
            ])
        return (
            buffer.getvalue(),
            "text/csv",
            f"llm-calls-{stamp}.csv",
        )
    body = "".join(
        json.dumps(_with_detail(data_dir, record), ensure_ascii=False) + "\n"
        for record in records
    )
    return body, "application/x-ndjson", f"llm-calls-{stamp}.jsonl"


def _with_detail(data_dir: Path, record: dict) -> dict:
    """Replace a JSONL record with its full detail when one exists.

    An export that still carries 800-char previews for calls whose full
    input/output is on disk is not the log the user asked for. Calls
    without a detail file (meta mode, or logged before this feature)
    keep their preview record — the record shows which kind it is.
    """
    try:
        detail = read_call_detail(data_dir, record.get("id") or "")
    except (OSError, ValueError):
        return record
    if detail is not None and isinstance(detail.get("request"), dict) and "prompt" in detail["request"]:
        return detail
    return record


def export_call_detail(
    data_dir: Path, call_id: str
) -> tuple[dict | None, str, str]:
    """One call's complete input and output; returns (record, media, filename).

    The detail file holds the verbatim prompt and response (no 800-char
    preview). A call logged in ``meta`` mode or before this feature existed
    exports its JSONL record instead — previews, marked as such by the
    presence of ``request.prompt_preview`` without ``request.prompt``.
    """
    record = read_call_detail(data_dir, call_id)
    if record is None:
        return None, "", ""
    return (
        record,
        "application/json",
        f"llm-call-{call_id}.json",
    )
