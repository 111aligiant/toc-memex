"""Provider adapters and the provider-neutral parse cache."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.ocr.cache import load_pages, save_pages
from app.ocr.models import OcrBlock, OcrPage, ParsedDocument
from app.ocr.normalize import markdown_to_blocks, polygon_to_bbox
from app.ocr.providers import (
    SUPPORTED_PROVIDERS,
    AzureOcrProvider,
    MineruOcrProvider,
    MistralOcrProvider,
    PaddleOcrProvider,
    build_ocr_provider,
)


def _settings(**overrides) -> Settings:
    base = dict(
        tocmemex_ocr_poll_timeout=5,
        tocmemex_ocr_poll_interval=0,
    )
    base.update(overrides)
    return Settings(**base)


def test_registry_picks_provider_and_rejects_unknown():
    assert isinstance(build_ocr_provider(_settings()), PaddleOcrProvider)
    assert isinstance(
        build_ocr_provider(_settings(tocmemex_ocr_provider="mistral")),
        MistralOcrProvider,
    )
    assert isinstance(
        build_ocr_provider(_settings(tocmemex_ocr_provider="mineru")),
        MineruOcrProvider,
    )
    assert isinstance(
        build_ocr_provider(_settings(tocmemex_ocr_provider="azure")),
        AzureOcrProvider,
    )
    with pytest.raises(ValueError) as exc:
        build_ocr_provider(_settings(tocmemex_ocr_provider="nope"))
    assert "mistral" in str(exc.value)
    assert set(SUPPORTED_PROVIDERS) == {"paddle", "mistral", "mineru", "azure"}


# ── cache round-trip ────────────────────────────────────────────────────


def test_pages_cache_roundtrip(tmp_path):
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                page_index=0,
                width=100,
                height=200,
                markdown="# 标题",
                blocks=[OcrBlock("paragraph_title", "1 范围", (0, 0, 100, 10), 1)],
                images={"a.jpg": "https://example.test/a.jpg"},
            )
        ],
        bookmarks=[],
        metadata={"ocr_provider": "mistral", "ocr_model": "m"},
    )
    save_pages(tmp_path, parsed)

    loaded = load_pages(tmp_path)
    assert loaded is not None
    assert loaded.metadata["ocr_provider"] == "mistral"
    page = loaded.pages[0]
    assert (page.width, page.height) == (100, 200)
    assert page.blocks[0].label == "paragraph_title"
    assert page.blocks[0].bbox == (0, 0, 100, 10)
    assert page.images == {"a.jpg": "https://example.test/a.jpg"}


def test_pages_cache_missing_returns_none(tmp_path):
    assert load_pages(tmp_path) is None


# ── markdown normalization (Mistral-style output) ──────────────────────


def test_markdown_to_blocks_labels_and_page_number():
    markdown = "\n".join(
        [
            "# 第1章 范围",
            "正文第一段。",
            "<table><tr><td>a</td></tr></table>",
            "![图1](img-0.jpeg)",
            "$$ E = mc^2 $$",
            "- 12 -",
        ]
    )
    blocks = markdown_to_blocks(markdown, width=1000, height=1400)
    labels = [block.label for block in blocks]
    assert labels[0] == "paragraph_title"
    assert "text" in labels
    assert "table" in labels
    assert "figure" in labels
    assert "display_formula" in labels
    assert labels[-1] == "number"
    # bboxes keep vertical order and stay inside the page.
    assert all(0 <= block.bbox[1] <= 1400 for block in blocks)
    ys = [block.bbox[1] for block in blocks]
    assert ys == sorted(ys)


def test_polygon_to_bbox_scales():
    assert polygon_to_bbox([10, 20, 30, 20, 30, 40, 10, 40]) == (10, 20, 30, 40)
    assert polygon_to_bbox([0, 0, 100, 0, 100, 50, 0, 50], scale_x=2, scale_y=2) == (
        0,
        0,
        200,
        100,
    )
    assert polygon_to_bbox(None) == (0.0, 0.0, 0.0, 0.0)


# ── Mistral ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mistral_parse_pdf(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/ocr")
        body = json.loads(request.content)
        assert body["document"]["type"] == "document_url"
        assert body["document"]["document_url"].startswith(
            "data:application/pdf;base64,"
        )
        return httpx.Response(
            200,
            json={
                "model": "mistral-ocr-latest",
                "pages": [
                    {
                        "index": 0,
                        "markdown": "# 1 范围\n\n正文。",
                        "dimensions": {"width": 1000, "height": 1400},
                        "images": [
                            {
                                "id": "img-0.jpeg",
                                "top_left_x": 0,
                                "top_left_y": 100,
                                "bottom_right_x": 200,
                                "bottom_right_y": 300,
                                "image_base64": "data:image/jpeg;base64,AAAA",
                            }
                        ],
                    }
                ],
            },
        )

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    provider = MistralOcrProvider(
        _settings(mistral_api_key="k"), transport=httpx.MockTransport(handler)
    )
    out = tmp_path / "ocr"
    parsed = await provider.parse_pdf(pdf, out)

    assert parsed.metadata["ocr_provider"] == "mistral"
    page = parsed.pages[0]
    assert page.blocks[0].label == "paragraph_title"
    assert page.blocks[0].content == "1 范围"
    assert page.images  # bbox-encoded key
    assert (out / "raw.jsonl").exists()
    assert load_pages(out) is not None


@pytest.mark.asyncio
async def test_mistral_requires_key(tmp_path):
    provider = MistralOcrProvider(_settings())
    with pytest.raises(Exception):
        await provider.parse_pdf(tmp_path / "a.pdf", tmp_path / "ocr")


# ── Azure Document Intelligence ────────────────────────────────────────


@pytest.mark.asyncio
async def test_azure_parse_pdf_maps_roles_and_tables(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "POST":
            assert request.headers["Ocp-Apim-Subscription-Key"] == "key"
            return httpx.Response(
                202,
                headers={"Operation-Location": "https://azure.test/op/1"},
            )
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "analyzeResult": {
                    "pages": [
                        {"pageNumber": 1, "width": 612, "height": 792},
                    ],
                    "paragraphs": [
                        {
                            "content": "1 范围",
                            "role": "sectionHeading",
                            "boundingRegions": [
                                {"pageNumber": 1, "polygon": [72, 72, 300, 72, 300, 90, 72, 90]}
                            ],
                        },
                        {
                            "content": "正文。",
                            "boundingRegions": [
                                {"pageNumber": 1, "polygon": [72, 100, 300, 100, 300, 120, 72, 120]}
                            ],
                        },
                        {
                            "content": "- 1 -",
                            "role": "pageNumber",
                            "boundingRegions": [
                                {"pageNumber": 1, "polygon": [300, 760, 320, 760, 320, 780, 300, 780]}
                            ],
                        },
                    ],
                    "tables": [
                        {
                            "boundingRegions": [
                                {"pageNumber": 1, "polygon": [72, 200, 400, 200, 400, 300, 72, 300]}
                            ],
                            "cells": [
                                {"rowIndex": 0, "columnIndex": 0, "content": "项目"},
                                {"rowIndex": 0, "columnIndex": 1, "content": "值"},
                            ],
                        }
                    ],
                },
            },
        )

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    provider = AzureOcrProvider(
        _settings(azure_di_endpoint="https://azure.test", azure_di_key="key"),
        transport=httpx.MockTransport(handler),
    )
    parsed = await provider.parse_pdf(pdf, tmp_path / "ocr")

    labels = [block.label for block in parsed.pages[0].blocks]
    assert "paragraph_title" in labels
    assert "table" in labels
    assert "number" in labels
    table = next(b for b in parsed.pages[0].blocks if b.label == "table")
    assert "<td>项目</td>" in table.content
    assert len(calls) == 2


# ── MinerU ─────────────────────────────────────────────────────────────


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "1 范围", "text_level": 1,
                     "page_idx": 0, "bbox": [90, 90, 910, 110]},
                    {"type": "text", "text": "正文。", "page_idx": 0,
                     "bbox": [90, 120, 910, 160]},
                    {"type": "table", "table_body": "<table><tr><td>a</td></tr></table>",
                     "page_idx": 0, "bbox": [90, 200, 910, 400]},
                    {"type": "image", "img_path": "images/abc.jpg",
                     "image_caption": ["图1 示意"], "page_idx": 0,
                     "bbox": [90, 420, 910, 700]},
                ]
            ),
        )
        zf.writestr(
            "middle.json",
            json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [1191, 1685]}]}),
        )
        zf.writestr("images/abc.jpg", b"jpeg")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_mineru_parse_pdf(tmp_path):
    archive = _zip_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/file-urls/batch"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "b1",
                        "file_urls": ["https://upload.test/put"],
                    },
                },
            )
        if request.method == "PUT":
            assert "authorization" not in {k.lower() for k in request.headers}
            return httpx.Response(200)
        if request.url.path.endswith("/extract-results/batch/b1"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://cdn.test/r.zip"}
                        ]
                    }
                },
            )
        if str(request.url) == "https://cdn.test/r.zip":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    provider = MineruOcrProvider(
        _settings(mineru_api_token="t"), transport=httpx.MockTransport(handler)
    )
    out = tmp_path / "ocr"
    parsed = await provider.parse_pdf(pdf, out)

    page = parsed.pages[0]
    assert (page.width, page.height) == (1191, 1685)
    labels = [block.label for block in page.blocks]
    assert labels.count("paragraph_title") == 1
    assert "table" in labels
    assert "figure" in labels
    # 0-1000 bbox scaled to page points.
    title = next(b for b in page.blocks if b.label == "paragraph_title")
    assert title.bbox[0] == pytest.approx(90 * 1191 / 1000)
    # Image written next to the cache and referenced with a file: URL.
    assert page.images
    image_url = next(iter(page.images.values()))
    assert image_url.startswith("file:")
    assert (out / "images" / "abc.jpg").exists()


@pytest.mark.asyncio
async def test_mineru_requires_token(tmp_path):
    provider = MineruOcrProvider(_settings())
    with pytest.raises(Exception):
        await provider.parse_pdf(tmp_path / "a.pdf", tmp_path / "ocr")
