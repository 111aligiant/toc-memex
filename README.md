# Toc Memex

> **PageIndex is a smart reader. Toc Memex is a library with a circulation
> desk — every agent's reads, citations, and corrections stay on the page.**

- Multiple agents share one local library instead of each rebuilding its own.
- Reads, citations, notes and corrections are anchored to the page they came from.
- The next agent starts from what the last one left, not from zero.

The library and retrieval run locally. Scanned files can use OCR — a cloud
provider by default, local providers supported.

## See it

A 60-second demo: ingest a paper folder, ask for a comparison, get every
sentence cited to a page, then leave one erratum that a second session picks
up. A demo recording is planned.

## Requirements

- Python 3.12 (3.13 is experimental but installable: `requires-python = ">=3.12,<3.14"`)
- Node.js 20+ only when you rebuild the web UI from source; the packaged bundle needs no npm
- Windows for the DOCX/DOC Word-COM path; other formats work cross-platform
- Optional: an OCR API token and/or an OpenAI-compatible LLM endpoint

## Install

```bash
# Run the MCP server straight away, without installing first
uvx --from git+https://github.com/111aligiant/toc-memex toc-memex-mcp

# Or install CLI + MCP + Web locally
pipx install git+https://github.com/111aligiant/toc-memex
toc-memex init
toc-memex ingest path/to/document.pdf
toc-memex-web            # optional web UI, prebuilt; no npm needed
```

The package installs three commands: `toc-memex` (CLI), `toc-memex-mcp` (MCP
server) and `toc-memex-web` (API + prebuilt web UI).

If `pip` connects but stalls with no progress (some IPv6 networks), retry with
a mirror, e.g. `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`. The
default commands above do not use a mirror.

MCP client config:

```json
{
  "mcpServers": {
    "toc-memex": {
      "command": "toc-memex-mcp",
      "env": { "TOCMEMEX_DB_PATH": "C:/path/to/data/tocmemex.db" }
    }
  }
}
```

## What it does

- **Ingest** — PDF text layer first, OCR fallback (PaddleOCR-VL, Mistral OCR,
  MinerU, Azure Document Intelligence); DOCX/DOC through Word COM; Markdown and TXT;
  XLSX / CSV / TSV as one TOC node per sheet; images via OCR.
- **Structure** — rebuilds a TOC from printed contents, PDF bookmarks, Word heading
  styles or OCR paragraph titles; maps printed page labels to PDF pages; validates the
  tree and flags unplaced or duplicate nodes.
- **Search** — SQLite FTS5 BM25 with jieba segmentation, section-number routing
  (`8.3`), document-number routing (`ACME 100`), a TOC-title channel, title-echo
  promotion, and an optional synonym dictionary. No vector search.
- **Library record** — every read, citation, note, task and correction is written
  against a page; collections, hand-off notes and external-agent sessions are part of
  the same record.
- **Repair** — quality signals become evidence-backed proposals; a curator checks
  them, and changes can be rolled back against the backup.

The core is deliberately generic: no document domain is hard-coded. Section
numbers, document numbers, chapter/appendix structure and print-page mapping
are handled by neutral rules; any domain vocabulary is supplied by you through
configuration.

The web UI adds a library, document workspace, search, quality, jobs, tasks,
notes, agent console, model/OCR endpoints and settings.

## Second screen: Chinese annual reports

The same product, with a different preset, eval set and demo, will cover
Chinese A-share annual reports. Same code, different configuration — not a
second product, and not part of the first release story.

## Benchmark

Retrieval and page-citation benchmark on five documents, 16 hand-checked
questions, deterministic and LLM-free.

| metric | value |
| --- | --- |
| hit@k | 68.75% |
| page@1 | 50.00% |
| citation_page_rate | 100.00% |
| citation_leak_rate | 0.00% |
| MRR | 0.5781 |

Method, failure cases and limitations are collected in the project wiki.
Reproduce (needs a local corpus):

```bash
python scripts/run_eval.py retrieval --db <db> --golden <golden.jsonl> --top-k 5
```

## Docs

Design and spike notes live in a private `docs/` folder kept out of this
repository. OCR providers are documented in [`.env.example`](.env.example).


## Quick start (development)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"           # or: uv sync --extra dev

copy .env.example .env            # fill OCR token / LLM endpoint if you have them

python -m app.cli init
python -m app.cli ingest path\to\document.pdf
python -m app.cli stats
python -m app.cli search "分析方法" --top 5
python -m app.cli search "8.3" --top 5
python -m app.cli search "ACME 100" --top 5
```

Rebuild the web bundle and start the server from source:

```bash
cd web
npm ci
npm run build
cd ..
python -m app.web_api --db data/tocmemex.db --port 8791 --web-dir web/dist
```

Or use the Windows launcher, which initialises the database, backs it up and rebuilds a
stale frontend automatically:

```bat
start-web.bat
start-web.bat 8793 --no-open
```

The read-only inspector is available through `start-inspector.bat` or:

```bash
python -m app.inspector --db data/tocmemex.db --port 8765
```

## MCP server

```bash
toc-memex-mcp
toc-memex-mcp --transport streamable-http --host 127.0.0.1 --port 8790
# development checkout:
python -m app.mcp_server
```

Example stdio client configuration:

```json
{
  "mcpServers": {
    "toc-memex": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "TOCMEMEX_DB_PATH": "D:/path/to/toc-memex/data/tocmemex.db"
      }
    }
  }
}
```

Archive tools: `memex_overview` / `overview` / `list_collections` / `list_documents` /
`get_document` / `get_toc` / `read_section` / `list_resources` / `read_resource` /
`search` / `ask` / `mark_used`. Activity tools: `task_create` / `task_update` /
`task_list` / `activity_write` / `activity_list` / `run_start` / `run_end` /
`task_timeline` / `add_note` / `list_notes` / `delete_note`. Memory/scoring:
`lesson_write` / `lesson_search` / `score_write` / `score_list`. Curator tools are
described in the MCP instructions.

## Configuration

All runtime settings are environment variables (or `.env`). The full list with comments
is in `.env.example`; the main groups are:

| Variable | Meaning |
|---|---|
| `TOCMEMEX_DB_PATH` | SQLite database, default `data/tocmemex.db` |
| `TOCMEMEX_DATA_DIR` | OCR cache, uploads and resources, default `data` |
| `TOCMEMEX_OCR_PROVIDER` | `paddle` (default), `mistral`, `mineru`, `azure` |
| `TOCMEMEX_OCR_ALLOW_PRIVATE_HOSTS` | Allow OCR endpoints on loopback/LAN |
| `TOCMEMEX_MAX_CHUNK_CHARS` / `TOCMEMEX_CHUNK_OVERLAP` | Chunk sizing |
| `TOCMEMEX_SEARCH_SYNONYMS` | Optional JSON map `{"term": ["alternative", ...]}` |
| `TOCMEMEX_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | LLM for TOC parsing and agents |
| `TOCMEMEX_LLM_LOG` | `full` (800-char previews), `meta`, `off` |
| `TOCMEMEX_ALLOW_NONLOCAL` | Allow binding the web/API service to a non-loopback host |

Provider-specific keys: `PADDLEOCR_API_TOKEN`, `MISTRAL_API_KEY`, `MINERU_API_TOKEN`,
`AZURE_DI_ENDPOINT` / `AZURE_DI_KEY`. All providers are configured via
[`.env.example`](.env.example).

The web UI can also store model and OCR endpoints in the database; the settings page
manages them at runtime without editing `.env`.

### Document numbers and synonyms

A document-number query is routed to matching titles before BM25 runs. The pattern is
generic: a short Latin designation plus a number (`ACME 100`, `RPT-17`,
`ACME/TS 100`). Common words such as `Chapter 12` or `Table 3` are not treated as
identifiers.

Search knows no domain vocabulary by default. To bridge aliases, set a JSON map:

```bash
TOCMEMEX_SEARCH_SYNONYMS={"alpha":["beta","gamma"],"湿度":["humidity"]}
```

It is used by both body search and TOC-title matching.

## Repository layout

```
app/                 # ingest, OCR, index, curation, agent, MCP, Web API, CLI, inspector
web/                 # Vue 3 + Vite frontend (build output: web/dist)
scripts/             # backup, smoke test, TOC probe, stats, curator task installer
tests/               # pytest
vendor/pageindex/    # MIT-licensed PageIndex code (see LICENSE / NOTICE)
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Some OCR/probe tests skip unless you place local fixtures under `tests/fixtures/` (see
`tests/fixtures/README.md`). No network, OCR token or LLM endpoint is needed for the
default suite.

## Limits and notes

- No vector search. Retrieval is FTS5 BM25 plus deterministic section/document routing.
- Excel / CSV uploads are CLI-only; the web upload dialog covers PDF, Word, Markdown,
  text and images.
- DOCX/DOC direct reading uses Word COM and is Windows-only; without Word, convert to
  PDF first.
- Upload progress is kept in memory, so a server restart loses in-flight upload state.
- The service has no login. It binds to loopback by default; exposing it to a LAN or the
  internet requires `TOCMEMEX_ALLOW_NONLOCAL=1` and a trusted network. Model and OCR
  endpoint settings can redirect stored keys, so do not expose the port to untrusted
  users.
- OCR cache and downloaded resources live under `TOCMEMEX_DATA_DIR`; back up the SQLite
  file and that directory together. `scripts/backup.py` does this transactionally.

## License

Toc Memex is released under the **MIT License**; see [`LICENSE`](LICENSE).

Third-party code keeps its own license — in particular `vendor/pageindex/` is
MIT-licensed (Vectify AI) and is not relicensed.
