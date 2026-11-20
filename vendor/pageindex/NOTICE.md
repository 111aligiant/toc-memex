# Third-party notice: PageIndex

- Source: https://github.com/VectifyAI/PageIndex
- Commit: `2ac8694bf8fe89a3840dda9bdb88db5119b77fc4`
- License: MIT (see `LICENSE` in this directory)
- Copyright: Vectify AI

Vendored files:

- `utils.py`
- `page_index_md.py`
- `naming.py`
- `flash/embedded_toc.py`

Modifications:

- Copied into the `vendor/pageindex` package.
- No logic changes. Import paths are unchanged; the package is used as `vendor.pageindex.*`.

If you update PageIndex upstream, re-copy these files and update the commit hash above.

## Ported code (not vendored verbatim)

`app/ingest/llm_toc.py` and `app/llm/client.py` port the prompt text and
control flow of the following PageIndex Classic functions
(`pageindex/page_index_classic.py`, `pageindex/utils.py`), adapted to this
codebase's `Settings`/`TocEntry`/httpx stack:

- `toc_detector_single_page` → `detect_toc_pages_llm` (per-page TOC detection)
- `find_toc_pages` → the run/stop logic inside `detect_toc_pages_llm`
- `toc_transformer` → `extract_toc_entries_llm` (printed TOC → JSON entries)
- `check_title_appearance` → `verify_entries_llm` (title-on-page verification)
- `single_toc_item_index_fixer` → `fix_entries_llm` (re-locate wrong pages)
- `extract_json` → `app/llm/client.py::extract_json`

Source: VectifyAI/PageIndex `main` branch, retrieved 2026-11-13. MIT, same
license as the vendored files above. Not covered by the commit hash above
(ported from `main`, not pinned).