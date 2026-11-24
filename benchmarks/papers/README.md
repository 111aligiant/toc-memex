# Benchmark: papers

Retrieval + page-citation benchmark for Toc Memex, round A (`scripts/run_eval.py
retrieval`). Deterministic, no LLM. The public repo contains **questions and
expectations only** — never the PDFs.

This directory is not gitignored, unlike `data/` and `eval/`. Local databases,
corpora and generated reports stay under `data/eval/` and are never committed.

## Layout

```
benchmarks/papers/
  README.md                # this file
  golden.jsonl             # main questions + expectations (16 scored rows)
  golden-crosslingual.jsonl # cross-lingual subset (kind=crosslingual, 4 rows)
  expected/                # optional: hand-checked page/citation tables
```

## Corpus

The 2026-11-23 spike produced 16 scored rows from five documents. The four
`p-hq-*` placeholders were dropped because 何庆's thesis was not ingested;
add them only after ingesting it and hand-checking `pdf_page_*`.

| # | Title | Authors / venue | Year | Pages | Public URL / DOI | Publicly reproducible |
|---|-------|-----------------|------|-------|------------------|-----------------------|
| 1 | Nitridation reaction of aluminum powder in flowing ammonia | J. Eur. Ceram. Soc. | 2003 | 8 | DOI `10.1016/S0955-2219(03)00014-1` | yes (journal paper) |
| 2 | A Chemical Solution Approach to Epitaxial Metal Nitride Thin Films | *Advanced Materials* 21, 193–197 | 2009 | 5 | journal paper; DOI not recorded | yes (journal paper) |
| 3 | AlN薄膜的制备与性能的研究（牛东伟） | Chinese thesis | — | 78 | not redistributable | no — obtain it yourself |
| 4 | 溶胶—凝胶法制备钛酸铝薄膜的工艺研究（冯果） | Chinese thesis, converted to image-only PDF | — | 73 | not redistributable | no — not in golden; OCR pipeline only |
| 5 | 材料科学基础笔记（手写扫描） | handwritten notes | — | 65 | not redistributable | no — obtain it yourself |

Rules:

- A paper that can be downloaded publicly gets its URL/DOI/arXiv ID and a
  SHA-256 checksum, so anyone can rebuild the same `data/eval/papers.db`.
- A Chinese scan that cannot be redistributed gets "how to obtain it
  yourself" instead of a URL, and the README must state that the item is
  **not publicly reproducible**. Never attach the PDF.
- One row = one paper. Keep the numbering stable after publishing so
  reports remain comparable.

## First real report (2026-11-23)

Snapshot: `report-2026-11-23.json`. Database and corpus stay local
(`data/eval/`), never committed.

| metric | value |
| --- | --- |
| queries | 16 |
| hit_at_k | 68.75% |
| primary_at_1 | 25.00% |
| page_at_1 | 37.50% |
| mrr | 0.5156 |
| citation_page_rate | 100.00% |
| citation_leak_rate | 0.00% |
| low_confidence_rate | 12.50% |
| ask_page_rate | 62.50% |

Known gaps exposed by this run:

- The five misses were **Chinese queries against English-only documents**
  (JECS ×2, AM ×3). There was no cross-lingual retrieval at the time; report
  it, do not paper over it. **Phase 0 unlocks this** via the query-side
  bilingual dictionary (see "Cross-lingual" below).
- AM (5-page journal article) has no bookmarks or printed TOC, so its TOC
  is a single `root`; the `toc` question cannot be answered. Known gap.
- Section numbers collide across documents (`4.2` exists in both 牛东伟 and
  冯果); the top hit can come from the wrong document. Cross-document
  disambiguation is an open item.
- Mixed-language queries (English title + Chinese question) can rank a
  Chinese OCR chunk above the intended English section.

## Cross-lingual (phase 0, 2026-11-24)

Pure Chinese queries against English-only documents do not match the English
index by lexical overlap — they need the query-side bilingual dictionary
(`TOCMEMEX_SEARCH_SYNONYMS`, `{"氮化物": ["nitride"], ...}`). No dictionary is
shipped by default; the mechanism is config by design.

`golden-crosslingual.jsonl` isolates those questions under `kind=crosslingual`
so the ESL direction has its own measurable gate. Same `spike.db`, same top-k.

| config | hit@k | MRR |
| --- | --- | --- |
| no dictionary | 0.00% (0/4) | 0.0000 |
| surgical dictionary | 75.00% (3/4) | 0.4167 |

`report-crosslingual-2026-11-24.json` is the with-dictionary snapshot.
`report-2026-11-24-syn.json` is the main 16-question golden under the same
dictionary (hit@k 68.75% → 87.50%, across stays 100%).

**Lesson:** the dictionary is a scalpel, not a sledgehammer. Broad maps
(`溅射 → sputtering`, `薄膜 → thin films`) make an English full-text root
outrank a Chinese thesis section on `across` questions — including a dictionary
term in a query that *targets Chinese content* pulls the English document up.
Map only the terms unique to the English target language and absent from the
Chinese-target queries.


## Golden format (v2)

One JSON object per line. Headings are compared after
`normalize_section_number` (so `§2.3`, `2.3`, `2.3条` are equal).

```jsonc
{
  "id": "p01",
  "kind": "fact | section | across | crosslingual | identifier | toc | no_answer",
  "query": "氮化铝薄膜的制备方法有哪些",
  "expected": {
    "doc_id": "…",                    // optional: omit to match any document
    "node_id": "…:h:2.3",             // read_section item; enables page + leak tests
    "heading_nums": ["2.3"],          // retrieval match
    "primary_heading_num": "2.3",     // optional; defaults to heading_nums[0]
    "pages": [[12, 14]],              // page ranges (multiple allowed)
    "answer_contains": ["溶胶-凝胶"]   // optional, not graded
  },
  "forbidden": {
    "node_ids": ["…:h:2.4"],          // read_section must not return these chunks
    "heading_nums": ["2.4"]           // or these titles in returned text
  },
  "notes": "…"
}
```

Legacy **v1 flat rows** are still accepted and map automatically:

```
id, query, kind, doc_id, primary_heading_num,
expected_heading_nums, expected_pages
```

`expected.doc_id` wins over the legacy `doc_id`; `kind` defaults to `""`.
A file may mix v1 and v2 lines.

## Metrics

Retrieval (per item, first `--top-k` hits):

- `hit_at_k` — an expected heading appears in the top k (heading match;
  page overlap only for hits without a heading number). Cross-document
  queries require the `doc_id` to match too.
- `primary_at_1` — the first hit's heading equals `primary_heading_num`.
- `page_at_1` — the first hit's page range overlaps `expected.pages`.
- `mrr` — reciprocal rank of the first correct hit.

`read_section` (items with `expected.node_id`):

- `citation_page_rate` — returned `[page_start, page_end]` overlaps
  `expected.pages`.
- `citation_leak_rate` — returned chunks include a `forbidden.node_ids`
  chunk, or the returned text contains a forbidden heading's title
  (titles shorter than 4 chars are ignored).
- `low_confidence_rate` — share of reads whose `citation_confidence` is
  `"low"` (reserved for round 03; reported as `—` until it exists).

`ask` (items with expected pages): `ask_page_rate` — a returned citation's
page range overlaps the expectation. The answer text is not graded.

`citation_leak_rate` and `low_confidence_rate` are better when lower.

## Running

```powershell
$PY = D:\Desktop\Toc目录树\toc-memex\.venv\Scripts\python.exe
& $PY scripts/run_eval.py retrieval `
  --db data/eval/papers.db `
  --golden benchmarks/papers/golden.jsonl `
  --top-k 5 `
  --out data/eval/report-papers.json
```

`--json` prints the JSON report instead of the markdown table.
`--fail-under hit_at_k=0.8` (repeatable, comma-separated) makes the process
exit 1 when a metric is below target. Exit code 2 means the golden file or
database could not be read.

`scripts/eval_retrieval.py` is kept as a thin wrapper over the same command
for older docs (`--queries` instead of `--golden`).
