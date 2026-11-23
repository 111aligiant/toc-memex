from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.ingest import pipeline
from app.ingest.pipeline import (
    _local_image_source,
    _image_headers,
    ingest_path,
    parse_markdown,
    save_resources,
    set_engine,
)
from app.models import Document, Namespace
from app.ocr.models import OcrBlock, OcrPage, ParsedDocument
from app.ocr.netguard import OutboundUrlError


class FakeOcr:
    async def parse_pdf(self, pdf_path, out_dir):
        return ParsedDocument(
            pages=[
                OcrPage(
                    0,
                    100,
                    200,
                    "",
                    [
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 100, 10), 1),
                        OcrBlock("text", "本文件规定了温度试验的要求。", (0, 20, 100, 30), 2),
                    ],
                    {},
                    {},
                )
            ],
            bookmarks=[],
            metadata={},
        )


class FakeNestedOcr:
    async def parse_pdf(self, pdf_path, out_dir):
        return ParsedDocument(
            pages=[
                OcrPage(
                    0,
                    100,
                    200,
                    "",
                    [
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 100, 10), 1),
                        OcrBlock("text", "范围正文", (0, 20, 100, 30), 2),
                        OcrBlock("paragraph_title", "1.1 目的", (0, 40, 100, 50), 3),
                        OcrBlock("text", "目的正文", (0, 60, 100, 70), 4),
                    ],
                    {},
                    {},
                )
            ],
            bookmarks=[],
            metadata={},
        )


def settings_for(tmp_path) -> Settings:
    return Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )


def test_image_headers_are_scoped_to_paddle_hosts():
    assert _image_headers(
        "https://pplines-online.bj.bcebos.com/img.jpg", "token"
    ) == {"Authorization": "bearer token"}
    assert _image_headers("https://example.test/img.jpg", "token") == {}


def test_image_headers_skip_presigned_bos_urls():
    """Paddle's images are presigned: a bearer header breaks the signature.

    BOS answers 400 MissingDateHeader when both an ``?authorization=...``
    query and an Authorization header are present, which made every figure
    download fail.
    """
    presigned = (
        "https://pplines-online.bj.bcebos.com/img.jpg"
        "?authorization=bce-auth-v1%2FALTAK%2F2026-11-12T13%3A57%3A16Z%2F-1%2F%2Fabc"
    )
    assert _image_headers(presigned, "token") == {}


@pytest.mark.asyncio
async def test_parse_pdf_uses_text_layer_without_ocr_client(tmp_path, monkeypatch):
    pdf = tmp_path / "text.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    expected = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [OcrBlock("text", "1 范围", (0, 0, 100, 10), 1)],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={"format": "pdf-text"},
    )
    monkeypatch.setattr(pipeline, "parse_pdf_text", lambda path: expected)
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: [])

    parsed = await pipeline._parse(pdf, "d1", settings_for(tmp_path), None)

    assert parsed is expected


@pytest.mark.asyncio
async def test_parse_pdf_falls_back_to_ocr_when_text_layer_unusable(
    tmp_path, monkeypatch
):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(pipeline, "parse_pdf_text", lambda path: None)
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: [])
    monkeypatch.setattr(pipeline, "build_ocr_provider", lambda settings: FakeOcr())

    parsed = await pipeline._parse(pdf, "d1", settings_for(tmp_path), None)

    assert parsed.pages[0].blocks[0].content == "1 范围"


@pytest.mark.asyncio
async def test_parse_pdf_force_ocr_skips_text_layer(tmp_path, monkeypatch):
    pdf = tmp_path / "text.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def _unreachable(path):  # noqa: ANN001
        raise AssertionError("force_ocr must not read the text layer")

    monkeypatch.setattr(pipeline, "parse_pdf_text", _unreachable)
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: [])
    monkeypatch.setattr(pipeline, "build_ocr_provider", lambda settings: FakeOcr())

    parsed = await pipeline._parse(
        pdf, "d1", settings_for(tmp_path), None, force_ocr=True
    )

    assert parsed.pages[0].blocks[0].content == "1 范围"


@pytest.mark.asyncio
async def test_open_ingest_row_force_reparses_a_ready_document(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    content_hash = pipeline._sha256(pdf)
    args = (engine, pdf, "shared", None, None, content_hash, "文本直读")

    doc_id, should_parse = await pipeline._open_ingest_row(*args)
    assert should_parse
    async with session_factory(engine)() as db:
        doc = await db.get(Document, doc_id)
        doc.status = "ready"
        await db.commit()

    same_id, should_parse = await pipeline._open_ingest_row(*args)
    assert (same_id, should_parse) == (doc_id, False)

    forced_id, should_parse = await pipeline._open_ingest_row(*args, force=True)
    assert (forced_id, should_parse) == (doc_id, True)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, doc_id)
        assert doc.status == "parsing"
        assert doc.parse_plan == "文本直读"
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_resources_sends_paddle_token_for_images(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b"jpeg"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, resource):
            self.added.append(resource)

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", FakeClient)

    async def allow(url, *, allow_private=False):
        return None

    # This test is about header scoping, not URL safety. Without the stub it
    # also depends on live DNS: a TUN-mode proxy resolves to its fake-IP
    # range (198.18.0.0/15), which the guard correctly rejects.
    monkeypatch.setattr(pipeline, "assert_public_url", allow)

    page = OcrPage(
        0,
        100,
        200,
        "",
        [OcrBlock("image", "", (10, 20, 50, 60), 1)],
        {
            "imgs/img_in_image_box_10_20_50_60.jpg": (
                "https://pplines-online.bj.bcebos.com/img.jpg"
            )
        },
        {},
    )
    parsed = ParsedDocument(pages=[page], bookmarks=[])
    settings = Settings(
        tocmemex_data_dir=tmp_path / "data",
        paddleocr_api_token="token",
    )
    db = FakeDb()
    await save_resources(db, SimpleNamespace(id="doc1"), parsed, settings)

    assert captured["headers"] == {"Authorization": "bearer token"}
    assert captured["url"] == "https://pplines-online.bj.bcebos.com/img.jpg"
    assert len(db.added) == 1
    assert db.added[0].image_path is not None
    assert Path(db.added[0].image_path).read_bytes() == b"jpeg"


@pytest.mark.asyncio
async def test_save_resources_decodes_data_url_images(tmp_path):
    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, resource):
            self.added.append(resource)

    payload = base64.b64encode(b"jpeg").decode("ascii")
    page = OcrPage(
        0,
        100,
        200,
        "",
        [OcrBlock("image", "", (10, 20, 50, 60), 1)],
        {
            "imgs/img_in_image_box_10_20_50_60.jpg": (
                f"data:image/jpeg;base64,{payload}"
            )
        },
        {},
    )
    parsed = ParsedDocument(pages=[page], bookmarks=[])
    settings = Settings(tocmemex_data_dir=tmp_path / "data")
    db = FakeDb()
    await save_resources(db, SimpleNamespace(id="doc2"), parsed, settings)

    assert len(db.added) == 1
    assert db.added[0].image_path is not None
    assert Path(db.added[0].image_path).read_bytes() == b"jpeg"


@pytest.mark.asyncio
async def test_save_resources_copies_file_url_images(tmp_path):
    """MinerU writes images next to the cache; save_resources copies them in.

    Also covers the Windows path bug: urlparse("file:///D:/x.jpg").path is
    "/D:/x.jpg", which Path() would read as the relative "D:x.jpg".
    """

    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, resource):
            self.added.append(resource)

    ocr_dir = tmp_path / "data" / "ocr" / "doc3"
    images = ocr_dir / "images"
    images.mkdir(parents=True)
    source = images / "a.jpg"
    source.write_bytes(b"jpeg")

    page = OcrPage(
        0,
        100,
        200,
        "",
        [OcrBlock("figure", "图1", (10, 20, 50, 60), 1)],
        {"imgs/img_in_image_box_10_20_50_60.jpg": source.as_uri()},
        {},
    )
    parsed = ParsedDocument(pages=[page], bookmarks=[])
    settings = Settings(tocmemex_data_dir=tmp_path / "data")
    db = FakeDb()
    await save_resources(db, SimpleNamespace(id="doc3"), parsed, settings)

    assert len(db.added) == 1
    assert db.added[0].image_path is not None
    assert Path(db.added[0].image_path).read_bytes() == b"jpeg"


def test_local_image_source_rejects_paths_outside_ocr_dir(tmp_path):
    root = tmp_path / "ocr" / "doc4"
    root.mkdir(parents=True)
    inside = root / "images" / "a.jpg"
    inside.parent.mkdir()
    inside.write_bytes(b"jpeg")
    assert _local_image_source(inside.as_uri(), root) == inside.resolve()

    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"secret")
    with pytest.raises(ValueError):
        _local_image_source(secret.as_uri(), root)
    with pytest.raises(ValueError):
        _local_image_source("file://evil.example/x.jpg", root)


@pytest.mark.asyncio
async def test_save_resources_skips_private_image_urls(tmp_path, monkeypatch):
    """Vendor-supplied image URLs at internal targets must not be fetched.

    Covers both a literal loopback/link-local address and a hostname that
    resolves to one (the DNS-free pre-filter alone would miss the latter).
    """

    class FakeClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            FakeClient.calls += 1
            raise AssertionError("private URL must not be requested")

    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, resource):
            self.added.append(resource)

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", FakeClient)

    async def fake_assert(url, *, allow_private=False):
        if "169.254." in url or "internal.attacker" in url:
            raise OutboundUrlError(f"refusing non-public target: {url}")

    monkeypatch.setattr(pipeline, "assert_public_url", fake_assert)

    for i, image_url in enumerate(
        [
            "http://169.254.169.254/x.jpg",
            "http://internal.attacker/x.jpg",
        ]
    ):
        page = OcrPage(
            0,
            100,
            200,
            "",
            [OcrBlock("figure", "", (10, 20, 50, 60), 1)],
            {"imgs/img_in_image_box_10_20_50_60.jpg": image_url},
            {},
        )
        settings = Settings(tocmemex_data_dir=tmp_path / "data")
        db = FakeDb()
        await save_resources(
            db, SimpleNamespace(id=f"doc5{i}"), ParsedDocument([page], []), settings
        )
        assert FakeClient.calls == 0
        assert db.added[0].image_path is None


@pytest.mark.asyncio
async def test_ingest_pdf_creates_doc_toc_chunks_and_fts(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc_id = await ingest_path(
        pdf, ocr_client=FakeOcr(), settings=settings_for(tmp_path)
    )

    async with session_factory(engine)() as db:
        assert (await db.execute(text("select count(*) from documents"))).scalar() == 1
        assert (await db.execute(text("select count(*) from toc_nodes"))).scalar() >= 1
        assert (await db.execute(text("select count(*) from chunks"))).scalar() >= 1
        assert (await db.execute(text("select count(*) from chunk_fts"))).scalar() >= 1
        chunk = (
            await db.execute(text("select content, token_count from chunks"))
        ).mappings().first()
        assert chunk is not None
        assert 0 < chunk["token_count"] < len(chunk["content"])
        node = (
            await db.execute(text("select chunk_start, chunk_end from toc_nodes"))
        ).mappings().first()
        assert node is not None
        assert node["chunk_start"] is not None
        assert node["chunk_end"] is not None
        assert doc_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_persists_subtree_chunk_ranges(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    pdf = tmp_path / "nested.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    await ingest_path(
        pdf, ocr_client=FakeNestedOcr(), settings=settings_for(tmp_path)
    )

    async with session_factory(engine)() as db:
        rows = (
            await db.execute(
                text(
                    "select title, chunk_start, chunk_end from toc_nodes "
                    "order by chunk_start"
                )
            )
        ).mappings().all()
        by_title = {row["title"]: row for row in rows}
        parent = by_title["1 范围"]
        child = by_title["1.1 目的"]
        assert parent["chunk_start"] == 0
        assert parent["chunk_end"] == child["chunk_end"]
        assert child["chunk_start"] >= parent["chunk_start"]
    await engine.dispose()


def test_markdown_bom_does_not_hide_the_first_heading(tmp_path):
    """A UTF-8 BOM must not swallow the first H1 (it did: "﻿# 1 Scope")."""
    md = tmp_path / "bom.md"
    md.write_text("﻿# 1 Scope\n\n# 8 Test methods\n", encoding="utf-8")

    parsed = parse_markdown(md)

    assert [bookmark.title for bookmark in parsed.bookmarks] == [
        "1 Scope",
        "8 Test methods",
    ]


@pytest.mark.asyncio
async def test_ingest_markdown_direct(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    md = tmp_path / "note.md"
    md.write_text("# 周报\n\n本周完成温度试验。", encoding="utf-8")
    doc_id = await ingest_path(
        md, doc_type="weekly", settings=settings_for(tmp_path)
    )

    async with session_factory(engine)() as db:
        assert (await db.execute(text("select count(*) from documents"))).scalar() == 1
        assert (await db.execute(text("select count(*) from chunks"))).scalar() >= 1
        assert doc_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_populates_keywords_and_node_summaries_but_not_doc_summary(tmp_path):
    """Document summary is deliberately not generated (ADR-0002 §7.2 revised).

    Keywords stay: they are a real document-filter signal. Node summaries
    stay: they preview a section inside ``get_toc``. The document-level
    summary is left empty until an LLM-written or hand-written one exists.
    """
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    pdf = tmp_path / "summary.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc_id = await ingest_path(
        pdf, ocr_client=FakeOcr(), settings=settings_for(tmp_path)
    )

    async with session_factory(engine)() as db:
        doc = (
            await db.execute(
                text(
                    "select summary, keywords from documents where id=:id"
                ),
                {"id": doc_id},
            )
        ).mappings().one()
        assert doc["summary"] is None
        assert doc["keywords"]

        node = (
            await db.execute(
                text(
                    "select summary from toc_nodes where doc_id=:id limit 1"
                ),
                {"id": doc_id},
            )
        ).mappings().first()
        assert node is not None
        assert node["summary"]

    await engine.dispose()

def test_parse_xlsx_gives_each_sheet_a_node_and_rows_their_column_names(tmp_path):
    """A workbook must not collapse into one node with one giant slice.

    The old parser joined every sheet into a single text block, so a table's
    values had no column names around them and BM25 ranked the document's
    one huge chunk far below prose sections.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "高温试验"
    sheet.append(["试验项目", "条件", "备注"])
    sheet.append(["高温试验", "85℃", "2 小时"])
    second = workbook.create_sheet("低温试验")
    second.append(["试验项目", "条件"])
    second.append(["低温试验", "-40℃"])
    path = tmp_path / "标准清单.xlsx"
    workbook.save(path)

    parsed = pipeline.parse_xlsx(path)

    # One bookmark per sheet -> one TOC node per sheet.
    assert [bookmark.title for bookmark in parsed.bookmarks] == ["高温试验", "低温试验"]
    assert [bookmark.page_index for bookmark in parsed.bookmarks] == [0, 1]
    text = "\n".join(
        block.content for page in parsed.pages for block in page.blocks
    )
    # Column names ride with every row, so "高温试验" is searchable as a value.
    assert "试验项目: 高温试验" in text
    assert "条件: 85℃" in text
    assert "试验项目: 低温试验" in text
    assert parsed.metadata["format"] == "xlsx"


def test_parse_csv_sniffs_gb18030_and_keeps_header_context(tmp_path):
    path = tmp_path / "bills.csv"
    path.write_bytes(
        "交易类型,金额,备注\n高温试验费,1200,七月\n低温试验费,800,八月\n".encode("gb18030")
    )

    parsed = pipeline.parse_csv(path)

    assert [bookmark.title for bookmark in parsed.bookmarks] == ["bills"]
    text = "\n".join(
        block.content for page in parsed.pages for block in page.blocks
    )
    assert "交易类型: 高温试验费" in text
    assert "金额: 1200" in text
    assert parsed.metadata["format"] == "csv"


def test_infer_doc_type_covers_entry_kinds():
    assert pipeline.infer_doc_type(Path("a.pdf")) == "pdf"
    assert pipeline.infer_doc_type(Path("a.CSV")) == "csv"
    assert pipeline.infer_doc_type(Path("a.tsv")) == "csv"
    assert pipeline.infer_doc_type(Path("a.xlsx")) == "xlsx"
    assert pipeline.infer_doc_type(Path("a.png")) == "image"


@pytest.mark.asyncio
async def test_parse_image_wraps_it_as_pdf_for_the_ocr_provider(tmp_path):
    """The image entry has no text layer; it must reach the OCR provider."""
    from PIL import Image

    image_path = tmp_path / "receipt.png"
    Image.new("RGB", (24, 24), "white").save(image_path)
    seen = {}

    class FakeImageOcr:
        async def parse_pdf(self, pdf_path, out_dir):
            seen["pdf"] = Path(pdf_path)
            return ParsedDocument(
                pages=[
                    OcrPage(
                        0, 24, 24, "",
                        [OcrBlock("text", "合计 120 元", (0, 0, 100, 10), 0)],
                        {}, {},
                    )
                ],
                bookmarks=[],
                metadata={},
            )

    parsed = await pipeline._parse(
        image_path, "d-img", settings_for(tmp_path), FakeImageOcr()
    )

    assert seen["pdf"].suffix == ".pdf"
    assert seen["pdf"].is_file()
    assert parsed.pages[0].blocks[0].content == "合计 120 元"
    assert parsed.metadata["format"] == "image"


class FakeTableFigureOcr:
    async def parse_pdf(self, pdf_path, out_dir):
        payload = base64.b64encode(b"jpeg").decode("ascii")
        return ParsedDocument(
            pages=[
                OcrPage(
                    0,
                    100,
                    200,
                    "",
                    [
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 100, 10), 0),
                        OcrBlock("text", "范围正文", (0, 20, 100, 30), 1),
                        OcrBlock(
                            "table",
                            "<table><tr><td>温度</td><td>85℃</td></tr></table>",
                            (0, 40, 100, 60),
                            2,
                        ),
                        OcrBlock("figure_title", "图1 布置", (0, 70, 100, 75), 3),
                        OcrBlock("image", "", (10, 80, 50, 120), 4),
                    ],
                    {
                        "imgs/img_in_image_box_10_80_50_120.jpg": (
                            f"data:image/jpeg;base64,{payload}"
                        )
                    },
                    {},
                )
            ],
            bookmarks=[],
            metadata={},
        )


@pytest.mark.asyncio
async def test_ingest_links_resources_to_section_nodes(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    pdf = tmp_path / "table.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    await ingest_path(
        pdf,
        ocr_client=FakeTableFigureOcr(),
        settings=settings_for(tmp_path),
    )

    async with session_factory(engine)() as db:
        rows = (
            await db.execute(text("select resource_id, node_id from resources"))
        ).mappings().all()
        chunk_nodes = {
            row[0]
            for row in (
                await db.execute(text("select distinct node_id from chunks"))
            ).fetchall()
        }
    assert rows
    assert all(row["node_id"] is not None for row in rows)
    assert {row["node_id"] for row in rows} <= chunk_nodes
    await engine.dispose()


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (120, 60), "white").save(path)
    return path


@pytest.mark.asyncio
async def test_ingest_image_plain_text_mode_has_no_toc_or_resources(tmp_path):
    """The 纯文本 image mode keeps one flat searchable text document."""
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    image = _png(tmp_path / "shot.png")
    doc_id = await ingest_path(
        image,
        ocr_client=FakeOcr(),
        settings=settings_for(tmp_path),
        image_mode="text",
    )

    async with session_factory(engine)() as db:
        nodes = (
            await db.execute(
                text("select title, page_start, page_end from toc_nodes where doc_id = :id"),
                {"id": doc_id},
            )
        ).mappings().all()
        chunks = (
            await db.execute(
                text("select content from chunks where doc_id = :id"), {"id": doc_id}
            )
        ).scalars().all()
        resources = (
            await db.execute(
                text("select count(*) from resources where doc_id = :id"), {"id": doc_id}
            )
        ).scalar()
        doc = await db.get(Document, doc_id)

    assert [node["title"] for node in nodes] == ["全文"]
    assert resources == 0
    assert chunks and "温度试验" in chunks[0]
    assert doc.status == "ready"
    assert doc.stage == "完成"
    assert doc.progress == 100
    assert doc.parse_plan == "OCR 纯文本"
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_image_structured_mode_keeps_headings(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    image = _png(tmp_path / "shot.png")
    doc_id = await ingest_path(
        image, ocr_client=FakeOcr(), settings=settings_for(tmp_path)
    )

    async with session_factory(engine)() as db:
        titles = (
            await db.execute(
                text("select title from toc_nodes where doc_id = :id"), {"id": doc_id}
            )
        ).scalars().all()
        doc = await db.get(Document, doc_id)

    assert "1 范围" in titles
    assert doc.parse_plan == "OCR 结构化"
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_reuses_failed_row_instead_of_reporting_duplicate(tmp_path):
    """A failed parse must be retryable by re-uploading the same bytes."""
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    set_engine(engine)

    md = tmp_path / "note.md"
    md.write_text("# 周报\n\n本周完成温度试验。", encoding="utf-8")
    digest = hashlib.sha256(md.read_bytes()).hexdigest()

    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="failed-doc",
                namespace_id="shared",
                title="note",
                filename="note.md",
                file_path=str(md),
                content_hash=digest,
                status="failed",
                error="OCR 任务超时",
            )
        )
        await db.commit()

    doc_id = await ingest_path(md, settings=settings_for(tmp_path))

    async with session_factory(engine)() as db:
        doc = await db.get(Document, "failed-doc")
        chunks = (
            await db.execute(text("select count(*) from chunks where doc_id = 'failed-doc'"))
        ).scalar()

    assert doc_id == "failed-doc"
    assert doc.status == "ready"
    assert doc.error is None
    assert chunks >= 1
    await engine.dispose()


# ── authored PDF outline preferred over text-line heuristics ───────────


def test_prefer_outline_bookmarks_trusts_a_real_tree(monkeypatch):
    from app.ingest import pipeline
    from app.ocr.models import Bookmark

    outline = [
        Bookmark(1, "Nitridation reaction", 0, from_outline=True),
        Bookmark(2, "Introduction", 0, from_outline=True),
        Bookmark(3, "Reactor design", 2, from_outline=True),
    ]
    synth = [
        Bookmark(1, "3 H (2) The free-energy change of reaction", 1),
        Bookmark(1, "2016 Y. Qiu, L. Gao / Journal", 1),
    ]
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: outline)
    assert (
        pipeline._prefer_outline_bookmarks(synth, Path("a.pdf")) is outline
    )


def test_prefer_outline_keeps_heuristics_for_a_junky_outline(monkeypatch):
    from app.ingest import pipeline
    from app.ocr.models import Bookmark

    outline = [
        Bookmark(1, "Contents", 0, from_outline=True),
        Bookmark(1, "Chapter 1", 1, from_outline=True),
    ]
    synth = [
        Bookmark(1, "1 范围", 1),
        Bookmark(1, "2 要求", 2),
        Bookmark(1, "3 方法", 3),
    ]
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: outline)
    assert (
        pipeline._prefer_outline_bookmarks(synth, Path("a.pdf")) is synth
    )
