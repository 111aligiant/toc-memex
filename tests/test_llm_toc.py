from app.config import Settings
from app.ingest.llm_toc import detect_toc_pages_llm, page_texts_for_llm
from app.ocr.models import OcrBlock, OcrPage, ParsedDocument


def _settings() -> Settings:
    return Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
    )


def _parsed_with_pages(texts: list[str]) -> ParsedDocument:
    """One text block per page."""
    pages = [
        OcrPage(
            page_index=i,
            width=595.0,
            height=842.0,
            markdown="",
            blocks=[OcrBlock("text", text, (0.0, 0.0, 595.0, 100.0), 0)],
        )
        for i, text in enumerate(texts)
    ]
    return ParsedDocument(pages=pages)


def test_page_texts_are_tagged_and_normalized():
    parsed = _parsed_with_pages(["前言 $ ^{*} $", "1.0 范围……3"])
    texts = page_texts_for_llm(parsed)
    assert "<physical_index_1>" in texts[0]
    assert texts[0].count("<physical_index_1>") == 2  # 前后都包标记
    assert "physical_index_2" in texts[1]
    assert "$" not in texts[0]  # normalize_text 已清洗 LaTeX 噪声


def test_detect_toc_pages_stops_at_first_no(monkeypatch):
    # 前 3 页答 yes，第 4 页答 no → 只返回 [0,1,2]
    def fake(prompt, settings=None, **kw):
        return '{"toc_detected": "no"}' if "PAGE4MARK" in prompt else '{"toc_detected": "yes"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    texts = [f"page{i} PAGE{i+1}MARK" for i in range(5)]
    assert detect_toc_pages_llm(texts, _settings()) == [0, 1, 2]


def test_detect_toc_pages_respects_check_limit(monkeypatch):
    monkeypatch.setattr(
        "app.ingest.llm_toc.llm_complete",
        lambda *a, **kw: '{"toc_detected": "yes"}',
    )
    assert detect_toc_pages_llm(["x"] * 50, _settings()) == list(range(30))


def test_extract_entries_parses_structure_title_page(monkeypatch):
    from app.ingest.llm_toc import extract_toc_entries_llm

    fake = (
        '[{"structure": "8.3", "title": "振动试验要求", "page": 47},'
        '{"structure": null, "title": "前言", "page": null}]'
    )
    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", lambda *a, **kw: fake)
    entries = extract_toc_entries_llm(["8.3 振动试验要求……47"], _settings())
    assert entries[0].heading_num == "8.3" and entries[0].level == 2
    assert entries[0].printed_page == "47" and entries[0].evidence == "llm"
    assert entries[1].heading_num is None and entries[1].level == 1


def test_extract_entries_page_out_of_range_becomes_none(monkeypatch):
    from app.ingest.llm_toc import extract_toc_entries_llm

    monkeypatch.setattr(
        "app.ingest.llm_toc.llm_complete",
        lambda *a, **kw: '[{"structure": "1", "title": "范围", "page": 9999}]',
    )
    entries = extract_toc_entries_llm(["1 范围……9999"], _settings(), page_count=100)
    assert entries[0].printed_page is None


def test_extract_entries_dot_leaders_normalized_in_prompt(monkeypatch):
    from app.ingest.llm_toc import extract_toc_entries_llm

    seen = {}

    def fake(prompt, *a, **kw):
        seen["p"] = prompt
        return "[]"

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    extract_toc_entries_llm(["4.5 温度试验．．．．．．．15"], _settings(), page_count=100)
    assert "．．．．．．" not in seen["p"]  # prompt 里点线已被归一


def test_build_mapping_context_exposes_page_map_and_ranges():
    from app.ingest.llm_toc import build_mapping_context

    def _page(page_index: int, blocks: list[OcrBlock]) -> OcrPage:
        return OcrPage(
            page_index=page_index, width=595.0, height=842.0, markdown="", blocks=blocks
        )

    # 页 0 是第 4 章的详细目录页；页 1-2 是带页脚页码（number label）的正文。
    toc_page = _page(0, [OcrBlock("text", "目 录\n4.1 试验条件……5\n4.2 试验程序……6", (0.0, 0.0, 595.0, 300.0), 0)])
    body_1 = _page(
        1,
        [
            OcrBlock("paragraph_title", "4.1 试验条件", (0.0, 0.0, 595.0, 100.0), 0),
            OcrBlock("text", "正文内容一", (0.0, 110.0, 595.0, 210.0), 1),
            OcrBlock("number", "5", (0.0, 800.0, 595.0, 810.0), 2),
        ],
    )
    body_2 = _page(
        2,
        [
            OcrBlock("paragraph_title", "4.2 试验程序", (0.0, 0.0, 595.0, 100.0), 0),
            OcrBlock("text", "正文内容二", (0.0, 110.0, 595.0, 210.0), 1),
            OcrBlock("number", "6", (0.0, 800.0, 595.0, 810.0), 2),
        ],
    )
    parsed = ParsedDocument(pages=[toc_page, body_1, body_2])

    ctx = build_mapping_context(parsed.pages, {0}, [(0, "4")])
    assert "n:5" in ctx.page_map and "n:6" in ctx.page_map
    assert "4" in ctx.chapter_toc_ranges
    assert ctx.body_headings  # 4.1 / 4.2 被收为正文标题


def _entry(title: str, page_index: int | None) -> "object":
    from app.ingest.printed_toc import TocEntry

    return TocEntry(
        title=title,
        heading_num=None,
        level=1,
        printed_page=None,
        page_index=page_index,
        confidence=0.85,
        evidence="llm",
    )


def _page_texts(count: int = 30) -> list[str]:
    texts = []
    for n in range(1, count + 1):
        texts.append(f"<physical_index_{n}>\npage {n} content\n<physical_index_{n}>")
    return texts


def test_verify_flags_entry_whose_title_is_absent(monkeypatch):
    from app.ingest.llm_toc import verify_entries_llm

    entries = [_entry("4.5 温度试验", 14), _entry("9.9 不存在的节", 20)]

    def fake(prompt, *a, **kw):
        return '{"answer": "no"}' if "9.9" in prompt else '{"answer": "yes"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    bad = verify_entries_llm(entries, _page_texts(), _settings())
    assert bad == [1]


def test_verify_skips_unplaced_entries(monkeypatch):
    from app.ingest.llm_toc import verify_entries_llm

    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        return '{"answer": "yes"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    bad = verify_entries_llm([_entry("无页码节", None)], _page_texts(), _settings())
    assert bad == [] and calls["n"] == 0


def test_fix_repairs_wrong_page_within_window(monkeypatch):
    from app.ingest.llm_toc import fix_entries_llm

    entries = [_entry("4.4 前一节", 10), _entry("4.5 温度试验", 99), _entry("4.6 后一节", 16)]

    def fake(prompt, *a, **kw):
        return '{"physical_index": "<physical_index_15>"}'  # 1-based 15 → 0-based 14

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    fixed = fix_entries_llm(entries, _page_texts(), [1], _settings())
    assert fixed[1].page_index == 14
    assert entries[1].page_index == 99  # 原列表不被修改


def test_fix_gives_up_to_unplaced_not_guessed(monkeypatch):
    from app.ingest.llm_toc import fix_entries_llm
    from app.llm.client import LLMError

    entries = [_entry("4.5 温度试验", 99)]
    calls = {"n": 0}

    def fake(prompt, *a, **kw):
        calls["n"] += 1
        raise LLMError("bad answer")

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    fixed = fix_entries_llm(entries, _page_texts(), [0], _settings(), max_attempts=2)
    assert fixed[0].page_index is None
    assert calls["n"] == 2


def test_verify_sample_capped(monkeypatch):
    from app.ingest.llm_toc import verify_entries_llm

    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        return '{"answer": "yes"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    verify_entries_llm(
        [_entry(f"t{i}", i) for i in range(40)], _page_texts(40), _settings()
    )
    assert calls["n"] == 10


def test_llm_toc_cache_roundtrip(tmp_path):
    from app.ingest.llm_toc import load_llm_toc, save_llm_toc
    from app.ingest.printed_toc import PrintedToc, TocEntry

    settings = Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
        tocmemex_data_dir=tmp_path,
    )
    toc = PrintedToc(
        entries=[
            TocEntry(
                title="8.3 振动试验要求",
                heading_num="8.3",
                level=2,
                printed_page="47",
                page_index=46,
                confidence=0.85,
                evidence="llm",
            )
        ],
        source_page=0,
        confidence=0.8,
    )
    save_llm_toc(settings, "doc1", toc, page_count=10)
    loaded = load_llm_toc(settings, "doc1", model="m", page_count=10)
    assert loaded is not None
    assert loaded.entries[0].title == "8.3 振动试验要求"
    assert loaded.entries[0].page_index == 46
    assert loaded.entries[0].evidence == "llm"


def test_llm_toc_cache_model_mismatch_misses(tmp_path):
    from app.ingest.llm_toc import load_llm_toc, save_llm_toc
    from app.ingest.printed_toc import PrintedToc

    settings = Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
        tocmemex_data_dir=tmp_path,
    )
    save_llm_toc(settings, "doc1", PrintedToc(entries=[], source_page=0, confidence=0.8), page_count=10)
    assert load_llm_toc(settings, "doc1", model="other", page_count=10) is None
    assert load_llm_toc(settings, "doc1", model="m", page_count=999) is None


def test_detect_llm_uses_cache_without_llm_calls(tmp_path, monkeypatch):
    from app.ingest.llm_toc import detect_printed_toc_llm, save_llm_toc
    from app.ingest.printed_toc import PrintedToc, TocEntry

    settings = Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
        tocmemex_data_dir=tmp_path,
    )
    save_llm_toc(
        settings,
        "doc1",
        PrintedToc(
            entries=[
                TocEntry(
                    title="1.0 章节",
                    heading_num="1.0",
                    level=1,
                    printed_page="1",
                    page_index=0,
                    confidence=0.85,
                    evidence="llm",
                )
            ],
            source_page=0,
            confidence=0.8,
        ),
        page_count=0,
    )
    monkeypatch.setattr(
        "app.ingest.llm_toc.page_texts_for_llm",
        lambda parsed: (_ for _ in ()).throw(AssertionError("cache hit must not build texts")),
    )
    parsed = ParsedDocument(pages=[])
    result = detect_printed_toc_llm(parsed, settings, doc_id="doc1")
    assert result is not None and result.entries[0].title == "1.0 章节"


# --- Final-review fix pass (RED first) ---

def test_detect_toc_pages_survives_non_dict_answer(monkeypatch):
    """I2: array/bare-string LLM answers count as "no", never raise."""
    from app.ingest.llm_toc import detect_toc_pages_llm

    monkeypatch.setattr(
        "app.ingest.llm_toc.llm_complete", lambda *a, **kw: "[1, 2, 3]"
    )
    assert detect_toc_pages_llm(["x", "y"], _settings()) == []


def test_fix_window_extends_to_last_page_for_trailing_entry(monkeypatch):
    """I3: a trailing bad entry's window reaches the last document page."""
    from app.ingest.llm_toc import fix_entries_llm

    entries = [_entry("4.4 前一节", 10), _entry("附录B 尾节", 3)]  # index 1 bad
    texts = _page_texts(25)

    seen_pages = []

    def fake(prompt, *a, **kw):
        seen_pages.append(prompt)
        return '{"physical_index": "<physical_index_20>"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake)
    fixed = fix_entries_llm(entries, texts, [1], _settings())
    assert fixed[1].page_index == 19
    # The prompt must contain pages well beyond the previous mapped page 11.
    assert "<physical_index_20>" in seen_pages[0]


def test_extract_entries_title_keeps_number_prefix(monkeypatch):
    """M1: LLM-path titles carry the heading number like the rule path."""
    from app.ingest.llm_toc import extract_toc_entries_llm

    monkeypatch.setattr(
        "app.ingest.llm_toc.llm_complete",
        lambda *a, **kw: '[{"structure": "8.3", "title": "振动试验要求", "page": 47}]',
    )
    entries = extract_toc_entries_llm(["8.3 振动试验要求……47"], _settings())
    assert entries[0].title == "8.3 振动试验要求"
