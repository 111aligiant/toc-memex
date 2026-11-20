from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ocr.models import OcrBlock, OcrPage
from app.ocr.parser import OcrParseError, page_text, parse_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "paddleocr_sample.jsonl"
pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="OCR fixtures are not tracked; see tests/fixtures/README.md",
)


def test_parse_jsonl_returns_four_pages():
    pages = parse_jsonl(FIXTURE.read_text(encoding="utf-8"))
    assert len(pages) == 4
    assert [p.page_index for p in pages] == [0, 1, 2, 3]
    assert pages[0].width == 1191
    assert pages[0].height == 1685


def test_parse_jsonl_block_labels_and_table():
    pages = parse_jsonl(FIXTURE.read_text(encoding="utf-8"))
    labels = {b.label for p in pages for b in p.blocks}
    assert {"paragraph_title", "text", "table", "figure_title"} <= labels
    table = next(b for p in pages for b in p.blocks if b.label == "table")
    assert table.content.startswith("<table>")
    assert "筛选参数名称及要求" in table.content


def test_parse_jsonl_images():
    pages = parse_jsonl(FIXTURE.read_text(encoding="utf-8"))
    assert any(p.images for p in pages)
    url = next(iter(next(p.images.values() for p in pages if p.images)))
    assert url.startswith("http")


def test_page_text_skips_header_footer_and_keeps_body():
    pages = parse_jsonl(FIXTURE.read_text(encoding="utf-8"))
    text = page_text(pages[1])
    assert "范围" in text
    assert "筛选" in text or "选择" in text


def test_page_text_skips_header_and_footer_images():
    page = OcrPage(
        0,
        100,
        200,
        "",
        [
            OcrBlock("header_image", "LOGO", (0, 0, 10, 10), 1),
            OcrBlock("text", "正文", (0, 20, 10, 30), 2),
            OcrBlock("footer_image", "FOOTER", (0, 40, 10, 50), 3),
        ],
        {},
        {},
    )
    assert page_text(page) == "正文"


def test_parse_jsonl_rejects_error_payload():
    raw = json.dumps(
        {
            "logId": "log-1",
            "errorCode": 403,
            "errorMsg": "Access Denied",
            "result": {},
        }
    )
    with pytest.raises(OcrParseError, match="403.*Access Denied.*log-1"):
        parse_jsonl(raw)


def test_parse_jsonl_rejects_error_message_with_zero_code():
    raw = json.dumps(
        {
            "logId": "log-3",
            "errorCode": 0,
            "errorMsg": "Access Denied",
            "result": {},
        }
    )
    with pytest.raises(OcrParseError, match="Access Denied.*log-3"):
        parse_jsonl(raw)


def test_parse_jsonl_rejects_page_count_mismatch():
    raw = json.dumps(
        {
            "logId": "log-2",
            "errorCode": 0,
            "errorMsg": "Success",
            "result": {
                "dataInfo": {"numPages": 2},
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "width": 100,
                            "height": 200,
                            "parsing_res_list": [],
                        },
                        "markdown": {"text": "", "images": {}},
                    }
                ],
            },
        }
    )
    with pytest.raises(OcrParseError, match="page count mismatch"):
        parse_jsonl(raw)


def test_parse_jsonl_uses_data_info_page_dimensions_as_fallback():
    raw = json.dumps(
        {
            "logId": "log-4",
            "errorCode": 0,
            "errorMsg": "Success",
            "result": {
                "dataInfo": {
                    "numPages": 1,
                    "pages": [{"width": 800, "height": 600}],
                },
                "layoutParsingResults": [
                    {
                        "prunedResult": {"parsing_res_list": []},
                        "markdown": {"text": "", "images": {}},
                    }
                ],
            },
        }
    )
    pages = parse_jsonl(raw)
    assert pages[0].width == 800
    assert pages[0].height == 600


def test_parse_jsonl_rejects_empty_response():
    with pytest.raises(OcrParseError, match="empty"):
        parse_jsonl("")


def test_parse_jsonl_rejects_invalid_json():
    with pytest.raises(OcrParseError, match="line 1"):
        parse_jsonl("{not json")