from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import url2pathname

import httpx
import openpyxl
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from vendor.pageindex.flash.embedded_toc import read_bookmarks as read_pdf_bookmarks_impl

from ..config import Settings, get_settings
from ..db import session_factory
from ..index.fts import count_tokens, index_document
from ..index.resources import link_resources
from ..models import (
    Chunk,
    Document,
    Namespace,
    Resource,
    TocNode,
    new_uuid,
    utcnow_iso,
)
from ..ocr.cache import load_pages, load_cached_pages
from ..ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument
from ..ocr.netguard import (
    OutboundUrlError,
    assert_public_url,
    is_obviously_private_url,
)
from ..ocr.parser import page_text
from ..ocr.providers import BaseOcrProvider, build_ocr_provider
from .chunker import chunk_document, chunk_ranges_by_node
from .docx_direct import parse_docx_direct
from .pdf_text import parse_pdf_text
from .provenance import build_run, record_run
from .summarize import extractive_summary, extract_keywords
from .toc import TocNodeData, anchor_nodes, build_toc
from .word import convert_docx

logger = logging.getLogger(__name__)

_ENGINE: AsyncEngine | None = None

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_IMG_KEY_RE = re.compile(r"_(\d+)_(\d+)_(\d+)_(\d+)\.")
BOOKMARKS_FILE = "bookmarks.json"


def set_engine(engine: AsyncEngine) -> None:
    global _ENGINE
    _ENGINE = engine


def get_engine() -> AsyncEngine:
    if _ENGINE is None:
        raise RuntimeError("engine is not initialized; call set_engine() first")
    return _ENGINE


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _ensure_namespace(db: AsyncSession, namespace: str) -> None:
    if await db.get(Namespace, namespace) is None:
        db.add(
            Namespace(
                id=namespace,
                kind="shared" if namespace == "shared" else "agent",
                description=None,
            )
        )
        await db.flush()


def save_bookmarks(settings: Settings, doc_id: str, bookmarks: list[Bookmark]) -> None:
    if not bookmarks:
        return
    path = settings.ocr_dir / doc_id / BOOKMARKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "level": bookmark.level,
            "title": bookmark.title,
            "page_index": bookmark.page_index,
            "heading_num": bookmark.heading_num,
            "from_outline": bookmark.from_outline,
        }
        for bookmark in bookmarks
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_bookmarks(settings: Settings, doc_id: str) -> list[Bookmark]:
    path = settings.ocr_dir / doc_id / BOOKMARKS_FILE
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    bookmarks: list[Bookmark] = []
    for item in payload:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        bookmarks.append(
            Bookmark(
                level=int(item.get("level") or 1),
                title=title,
                page_index=max(0, int(item.get("page_index") or 0)),
                heading_num=item.get("heading_num"),
                from_outline=bool(item.get("from_outline")),
            )
        )
    return bookmarks


def read_pdf_bookmarks(path: Path) -> list[Bookmark]:
    try:
        entries = read_pdf_bookmarks_impl(path)
    except Exception:
        return []
    return [
        Bookmark(
            level=int(entry.get("level") or 1),
            title=str(entry["title"]),
            page_index=max(0, int(entry.get("page") or 1) - 1),
            from_outline=True,
        )
        for entry in entries
        if entry.get("title")
    ]


def _prefer_outline_bookmarks(synth: list[Bookmark], path: Path) -> list[Bookmark]:
    """The authored PDF outline beats text-line heading heuristics.

    Heading heuristics over the text layer misread equation lines and page
    furniture as headings; the outline is metadata the authoring tool wrote.
    Trust it whenever it survives junk filtering as a real tree (enough
    entries, and either nested levels or a solid flat list). A PDF whose
    outline is junk or missing keeps the heuristic bookmarks — this is a
    per-document decision, not a format rule.
    """
    outline = read_pdf_bookmarks(path)
    if len(outline) < 2:
        return synth
    from .bookmarks import filter_bookmarks

    kept = filter_bookmarks(outline)
    levels = {bookmark.level for bookmark in kept}
    if len(kept) >= 3 and (len(levels) >= 2 or len(kept) >= 5):
        return outline
    return synth


def parse_markdown(path: Path) -> ParsedDocument:
    """Markdown/TXT: each heading starts a synthetic page; bookmarks carry levels."""
    # ``utf-8-sig`` strips a BOM: a leading U+FEFF otherwise hides the first
    # heading ("<BOM># 1 Scope") from ``_HEADING_RE`` and silently drops it.
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    lines = text.splitlines()

    sections: list[tuple[int, str, list[str]]] = []
    current: tuple[int, str, list[str]] | None = None
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match:
            if current is not None:
                sections.append(current)
            current = (len(match.group(1)), match.group(2).strip(), [])
        elif current is not None:
            current[2].append(line)

    if current is not None:
        sections.append(current)

    if not sections:
        page = OcrPage(
            0,
            0,
            0,
            "",
            [OcrBlock("text", text.strip(), (0, 0, 100, 10), 0)],
            {},
            {},
        )
        return ParsedDocument(pages=[page], bookmarks=[], metadata={"format": "text"})

    pages: list[OcrPage] = []
    bookmarks: list[Bookmark] = []
    for index, (level, title, body) in enumerate(sections):
        blocks = [OcrBlock("paragraph_title", title, (0, 0, 100, 10), 0)]
        body_text = "\n".join(body).strip()
        if body_text:
            blocks.append(OcrBlock("text", body_text, (0, 20, 100, 30), 1))
        pages.append(OcrPage(index, 0, 0, "", blocks, {}, {}))
        bookmarks.append(Bookmark(level=level, title=title, page_index=index))
    return ParsedDocument(pages=pages, bookmarks=bookmarks, metadata={"format": "markdown"})


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\t", " ").replace("\n", " ").strip()


def _table_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]], int]:
    """Split the first row off as a header when it looks like one.

    A header is a row of plain labels: no cell carries a digit. Anything
    else (``85℃``, ``-40``, ``第3批``) is data, so the row is kept as data
    and the columns get positional names. This matters for headerless
    exports: treating their first data row as a header would silently drop
    it from the index.
    """
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return [], [], 0
    first = rows[0]
    has_digits = any(
        cell and any(char.isdigit() for char in cell) for cell in first
    )
    width = max(len(row) for row in rows)
    if not has_digits:
        header = [first[i] if i < len(first) and first[i] else f"列{i + 1}"
                  for i in range(width)]
        return header, rows[1:], 2
    return [f"列{i + 1}" for i in range(width)], rows, 1


def _render_row(header: list[str], row: list[str], row_number: int) -> str:
    """One row as ``列名: 值 | 列名: 值``.

    A cell's column name is repeated in every row on purpose: without it a
    bare value has no topic word around it, and BM25 can only match the
    number. ``第N行`` keeps the row identifiable when citing the slice.
    """
    cells = [
        f"{header[i] if i < len(header) else f'列{i + 1}'}: {value}"
        for i, value in enumerate(row)
        if value
    ]
    if not cells:
        return ""
    return f"第{row_number}行 | " + " | ".join(cells)


def _table_document(
    sheets: list[tuple[str, list[list[str]]]], fmt: str
) -> ParsedDocument:
    """A workbook / CSV as one page per sheet, one TOC node per sheet.

    The old xlsx parser put a whole sheet into a single text block, so the
    chunker produced one huge node and one slice — a structured table was
    effectively unsearchable. Here rows are rendered with their column names
    and packed into ``_TABLE_ROWS_PER_BLOCK``-row blocks, which the normal
    chunker splits at ``max_chars``; each sheet becomes a bookmark and
    therefore a TOC node.
    """
    pages: list[OcrPage] = []
    bookmarks: list[Bookmark] = []
    for index, (name, rows) in enumerate(sheets):
        title = (name or f"表{index + 1}").strip()[:200] or f"表{index + 1}"
        header, body, first_row_number = _table_header(rows)
        blocks = [OcrBlock("paragraph_title", title, (0, 0, 100, 10), 0)]
        for start in range(0, len(body), _TABLE_ROWS_PER_BLOCK):
            lines = []
            for offset, row in enumerate(body[start : start + _TABLE_ROWS_PER_BLOCK]):
                line = _render_row(header, row, first_row_number + start + offset)
                if line:
                    lines.append(line)
            if lines:
                blocks.append(
                    OcrBlock(
                        "text",
                        "\n".join(lines),
                        (0, 20, 100, 30),
                        len(blocks),
                    )
                )
        pages.append(OcrPage(index, 0, 0, "", blocks, {}, {}))
        bookmarks.append(Bookmark(level=1, title=title, page_index=index))
    return ParsedDocument(pages=pages, bookmarks=bookmarks, metadata={"format": fmt})


_TABLE_ROWS_PER_BLOCK = 40


def parse_xlsx(path: Path) -> ParsedDocument:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets: list[tuple[str, list[list[str]]]] = []
    try:
        for sheet in workbook.worksheets:
            rows = [
                [_cell_text(value) for value in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            sheets.append((sheet.title, rows))
    finally:
        workbook.close()
    return _table_document(sheets, "xlsx")


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(path: Path) -> ParsedDocument:
    """CSV/TSV as a one-sheet table with header-aware, searchable rows.

    Delimiter and encoding are sniffed: bill exports are commonly UTF-8 BOM
    or GB18030, and `.tsv`/semicolon dumps are just CSV with another
    separator. A bad guess still yields rows, never an exception.
    """
    text = _read_text_file(path)
    sample = text[:64_000]
    delimiter = "\t" if path.suffix.lower() == ".tsv" else None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = delimiter or ","
    rows = [
        [_cell_text(value) for value in row]
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]
    return _table_document([(path.stem, rows)], "csv")


IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
}
TABLE_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv"}

# What the documents list can filter by. Kept coarse on purpose: the open
# Web/MCP filter is for "show me the tables", not for MIME taxonomy.
_DOC_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    **{suffix: "image" for suffix in IMAGE_SUFFIXES},
}


def infer_doc_type(path: Path) -> str | None:
    """Derive ``documents.doc_type`` from the file when the caller omits it.

    The column used to stay NULL for every ingested document (the CLI's
    ``--type`` was the only writer), which made ``list_documents(doc_type=)``
    and the overview's type distribution useless.
    """
    return _DOC_TYPE_BY_SUFFIX.get(Path(path).suffix.lower())


def image_to_pdf(path: Path, target: Path) -> Path:
    """Wrap an image as a one-page PDF so every OCR provider can read it.

    Providers speak ``parse_pdf`` only; converting with Pillow (already a
    dependency) gives the image entry point the whole existing pipeline —
    OCR, blocks, resources, chunks — without a provider-specific branch.
    EXIF orientation is applied first so phone photos are not sideways.
    """
    from PIL import Image, ImageOps

    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as opened:
        frames = []
        try:
            while True:
                frame = ImageOps.exif_transpose(opened.copy())
                if frame.mode not in ("RGB", "L"):
                    if frame.mode in ("RGBA", "LA", "P"):
                        background = Image.new("RGB", frame.size, (255, 255, 255))
                        rgba = frame.convert("RGBA")
                        background.paste(rgba, mask=rgba.split()[-1])
                        frame = background
                    else:
                        frame = frame.convert("RGB")
                frames.append(frame)
                opened.seek(opened.tell() + 1)
        except EOFError:
            pass
        if not frames:
            raise ValueError(f"image has no readable frames: {path}")
        frames[0].save(
            target,
            "PDF",
            save_all=len(frames) > 1,
            append_images=frames[1:],
            resolution=150.0,
        )
    return target


def _docx_direct_has_text(parsed: ParsedDocument) -> bool:
    """A page-image DOCX parses to pages but almost no text; send it to OCR."""
    chars = sum(
        len(block.content)
        for page in parsed.pages
        for block in page.blocks
        if block.content
    )
    return chars >= max(20, parsed.page_count * 20)


async def _parse(
    path: Path,
    doc_id: str,
    settings: Settings,
    ocr_client: BaseOcrProvider | None,
    ocr_settings: Settings | None = None,
    provenance: dict | None = None,
    image_mode: str = "structured",
    force_ocr: bool = False,
) -> ParsedDocument:
    """Parse one file into pages.

    ``ocr_settings`` is the resolved OCR configuration (DB default endpoint
    or .env fallback, see ``app.ocr.endpoints``); when omitted the caller's
    settings are used, which keeps every existing caller working.

    ``provenance`` (optional dict) records how the pages were obtained:
    ``parse_channel`` (pdf-text / docx-direct / ocr / ocr-text / markdown /
    xlsx), ``ocr_used`` and the provider's ``ocr_provider`` /
    ``ocr_model`` / ``ocr_job_id``.

    ``image_mode`` applies to standalone images only: ``structured`` (the
    default, current behaviour) or ``text`` (one plain-text document, see
    ``persist_plain_text``). It never changes how PDFs/DOCX are parsed.

    ``force_ocr`` skips the direct text layer and goes straight to OCR for
    PDF/DOCX. It is how a corpus that was ingested before OCR existed can be
    rebuilt on OCR pages even when the file's text layer is readable.
    """
    ocr_settings = ocr_settings or settings
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if ocr_client is None and not force_ocr:
            try:
                parsed = parse_pdf_text(path)
            except Exception:
                logger.warning(
                    "PDF text-layer parse failed; falling back to OCR",
                    exc_info=True,
                )
                parsed = None
            if parsed is not None:
                parsed.bookmarks = _prefer_outline_bookmarks(parsed.bookmarks, path)
                from .pdf_media import attach_pdf_text_media

                attach_pdf_text_media(path, parsed, settings.ocr_dir / doc_id)
                _record_parse(provenance, parsed, "pdf-text")
                return parsed
            logger.info("PDF text layer unusable; falling back to OCR")
        client = ocr_client or build_ocr_provider(ocr_settings)
        parsed = await client.parse_pdf(path, settings.ocr_dir / doc_id)
        parsed.bookmarks = read_pdf_bookmarks(path)
        _record_parse(provenance, parsed, "ocr", ocr_settings)
        return parsed
    if suffix in {".docx", ".doc"}:
        if ocr_client is None and not force_ocr:
            try:
                parsed = parse_docx_direct(path)
                if parsed.pages and _docx_direct_has_text(parsed):
                    _record_parse(provenance, parsed, "docx-direct")
                    return parsed
                logger.info(
                    "DOCX direct parse found little text; falling back to Word->PDF->OCR"
                )
            except Exception:
                logger.warning(
                    "DOCX direct parse failed; falling back to Word->PDF->OCR",
                    exc_info=True,
                )

        conversion = convert_docx(
            path, settings.tocmemex_data_dir / "converted" / f"{doc_id}.pdf"
        )
        client = ocr_client or build_ocr_provider(ocr_settings)
        parsed = await client.parse_pdf(conversion.pdf_path, settings.ocr_dir / doc_id)
        parsed.bookmarks = conversion.bookmarks or read_pdf_bookmarks(conversion.pdf_path)
        _record_parse(provenance, parsed, "ocr", ocr_settings)
        return parsed
    if suffix in {".md", ".markdown", ".txt"}:
        parsed = parse_markdown(path)
        _record_parse(provenance, parsed, "markdown")
        return parsed
    if suffix in TABLE_SUFFIXES:
        # shortcut: 表格类入库在 Web 与 CLI 均已禁用（2026-11-24 产品决策，
        # 等待无目录文档适配方案再放开）。解析函数保留供 reindex/export 使用。
        raise ValueError(
            f"{suffix} (spreadsheet) is not supported for ingest yet; "
            "convert to PDF/Word/Markdown and ingest that"
        )
    if suffix in IMAGE_SUFFIXES:
        # No text layer exists for a standalone image, so this always OCRs.
        pdf_path = image_to_pdf(
            path, settings.tocmemex_data_dir / "converted" / f"{doc_id}.pdf"
        )
        client = ocr_client or build_ocr_provider(ocr_settings or settings)
        parsed = await client.parse_pdf(pdf_path, settings.ocr_dir / doc_id)
        format_ = "image-text" if image_mode == "text" else "image"
        parsed.metadata = {**(parsed.metadata or {}), "format": format_}
        _record_parse(
            provenance, parsed, "ocr-text" if image_mode == "text" else "ocr", ocr_settings
        )
        return parsed
    raise ValueError(f"unsupported file type: {suffix}")


def _record_parse(
    provenance: dict | None,
    parsed: ParsedDocument,
    channel: str,
    ocr_settings: Settings | None = None,
) -> None:
    """Copy how these pages were produced into ``provenance``."""
    if provenance is None:
        return
    meta = parsed.metadata or {}
    ocr_used = channel in {"ocr", "ocr-text"}
    provenance.update(
        {
            "parse_channel": channel,
            "ocr_used": ocr_used,
            "ocr_provider": (meta.get("ocr_provider") if ocr_used else None)
            or (ocr_settings.tocmemex_ocr_provider if ocr_used and ocr_settings else None),
            "ocr_model": (meta.get("ocr_model") if ocr_used else None),
            "ocr_job_id": meta.get("job_id") if ocr_used else None,
        }
    )


def _nearest_title(block: OcrBlock, titles: list[OcrBlock]) -> str | None:
    if not titles:
        return None
    y = block.bbox[1]
    nearest = min(titles, key=lambda item: abs(item.bbox[1] - y))
    return nearest.content.strip()[:200] or None


def _image_headers(url: str, token: str) -> dict[str, str]:
    """Headers for fetching an OCR image.

    Paddle hands back *presigned* BOS URLs (``?authorization=bce-auth-v1/...``)
    which already carry their own signature. Adding ``Authorization: bearer
    <token>`` to one of those makes BOS answer ``400 MissingDateHeader`` —
    every figure download failed that way, which is why no slice ever had an
    image. Presigned URLs are fetched bare; the token is only attached to
    unsigned Paddle hosts (and never to a third-party host).
    """
    if not token:
        return {}
    host = (urlparse(url).hostname or "").lower()
    if not host.endswith((".bcebos.com", ".baidubce.com", ".aistudio-app.com")):
        return {}
    query = parse_qs(urlparse(url).query)
    if any(key.lower().startswith("authorization") for key in query):
        return {}
    return {"Authorization": f"bearer {token}"}


def _local_image_source(url: str, allowed_root: Path) -> Path:
    """Resolve a ``file:`` image URL to a path inside ``allowed_root``.

    ``url2pathname`` is required: on Windows ``urlparse("file:///D:/a.jpg").path``
    is ``/D:/a.jpg``, and ``Path()`` turns that into the *relative* path
    ``D:a.jpg``. Raises ValueError when the target escapes the OCR output dir
    (``file:///etc/passwd``, ``../`` segments, symlinks out of the tree).
    """
    parsed = urlparse(url)
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError(f"file URL with a host is not allowed: {url}")
    raw = url2pathname(parsed.path)
    if not raw:
        raise ValueError(f"file URL has no path: {url}")
    source = Path(raw).resolve()
    root = Path(allowed_root).resolve()
    if not source.is_relative_to(root):
        raise ValueError(f"file URL escapes the OCR output dir: {url}")
    return source


def _match_image_url(page: OcrPage, block: OcrBlock) -> str | None:
    if not page.images:
        return None
    bx0, by0, bx1, by1 = block.bbox
    best_url: str | None = None
    best_distance = float("inf")
    for key, url in page.images.items():
        match = _IMG_KEY_RE.search(key)
        if not match:
            continue
        x0, y0, x1, y1 = (float(value) for value in match.groups())
        distance = abs(x0 - bx0) + abs(y0 - by0) + abs(x1 - bx1) + abs(y1 - by1)
        if distance < best_distance:
            best_distance = distance
            best_url = url
    if best_url is not None and best_distance <= 40:
        return best_url
    return next(iter(page.images.values()), None)


async def _fetch_image(
    client: httpx.AsyncClient,
    url: str,
    target: Path,
    *,
    token: str,
    allowed_root: Path,
    allow_private: bool,
) -> bool:
    """Write one vendor image to ``target``. Returns True on success."""
    if url.startswith("file:"):
        # Local provider (MinerU zip) already wrote the file under this
        # document's OCR dir. Confine the read to that directory: image URLs
        # ultimately come from a vendor response, so "file:///etc/passwd" or
        # "file:///C:/Users/..." must not be copied.
        try:
            source = _local_image_source(url, allowed_root)
            target.write_bytes(source.read_bytes())
        except (OSError, ValueError) as exc:
            logger.warning("image copy failed: %s (%s)", url, exc)
            return False
        return True
    if url.startswith("data:"):
        try:
            _, _, payload = url.partition(",")
            target.write_bytes(base64.b64decode(payload))
        except (ValueError, binascii.Error) as exc:
            logger.warning("image data URL decode failed: %s", exc)
            return False
        return True
    if is_obviously_private_url(url):
        # Vendor-supplied image URL pointing at a loopback/link-local
        # address: refuse without a request (see app/ocr/netguard.py).
        logger.warning("image URL rejected (non-public host): %s", url)
        return False
    # Full DNS-resolving guard: an internal *hostname* passes the literal-IP
    # check but still must not be fetched. The DNS-free check above only
    # rejects obvious targets without paying a lookup.
    try:
        await assert_public_url(url, allow_private=allow_private)
    except OutboundUrlError as exc:
        logger.warning("image URL rejected: %s (%s)", url, exc)
        return False
    try:
        response = await client.get(url, headers=_image_headers(url, token))
    except httpx.HTTPError as exc:
        logger.warning("image download failed: %s (%s)", url, exc)
        return False
    if response.status_code != 200 or not response.content:
        logger.warning("image download failed HTTP %s: %s", response.status_code, url)
        return False
    target.write_bytes(response.content)
    return True


def _iter_resource_blocks(
    pages: list[OcrPage],
) -> Iterator[tuple[OcrPage, OcrBlock, list[OcrBlock], str, str]]:
    """Yield (page, block, titles, resource_id, type) in ingest order.

    Tables and figures share one per-page bucket counter; resource ids are
    derived from it, so every consumer (save / backfill) must iterate the
    same way or the ids drift.
    """
    for page in pages:
        titles = [b for b in page.blocks if b.label == "figure_title"]
        bucket = 0
        for block in page.blocks:
            if block.label == "table":
                yield page, block, titles, f"tbl_{page.page_index}_{bucket}", "table"
                bucket += 1
            elif block.label in {"figure", "chart", "image"}:
                yield page, block, titles, f"fig_{page.page_index}_{bucket}", "figure"
                bucket += 1


async def save_resources(
    db: AsyncSession,
    doc: Document,
    parsed: ParsedDocument,
    settings: Settings,
    ocr_settings: Settings | None = None,
) -> None:
    """Persist tables/figures.

    ``ocr_settings`` is the resolved OCR configuration; image URLs from
    Paddle need its token, so it is preferred over the plain settings.
    """
    image_token = (ocr_settings or settings).paddleocr_api_token
    resource_dir = settings.resources_dir / doc.id
    resource_dir.mkdir(parents=True, exist_ok=True)

    failed_images = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for page, block, titles, resource_id, kind in _iter_resource_blocks(
            parsed.pages
        ):
            if kind == "table":
                db.add(
                    Resource(
                        doc_id=doc.id,
                        resource_id=resource_id,
                        type="table",
                        title=_nearest_title(block, titles),
                        content=block.content,
                        page_idx=page.page_index,
                    )
                )
                continue

            image_path: str | None = None
            url = _match_image_url(page, block)
            if url:
                target = resource_dir / f"{resource_id}.jpg"
                if target.exists() and target.stat().st_size > 0:
                    # Reindex reuses previously downloaded images.
                    image_path = str(target)
                elif await _fetch_image(
                    client,
                    url,
                    target,
                    token=image_token,
                    allowed_root=settings.ocr_dir / doc.id,
                    allow_private=settings.tocmemex_ocr_allow_private_hosts,
                ):
                    image_path = str(target)
                else:
                    failed_images += 1
            db.add(
                Resource(
                    doc_id=doc.id,
                    resource_id=resource_id,
                    type="figure",
                    title=_nearest_title(block, titles),
                    content=None,
                    image_path=image_path,
                    page_idx=page.page_index,
                )
            )

    if failed_images:
        logger.warning(
            "%s OCR image(s) could not be downloaded for document %s",
            failed_images,
            doc.id,
        )


async def backfill_images(
    db: AsyncSession,
    doc: Document,
    parsed: ParsedDocument,
    settings: Settings,
    ocr_settings: Settings | None = None,
) -> dict:
    """Download figure images that an earlier ingest failed to fetch.

    Only fills ``resources.image_path``; TOC, chunks and FTS are untouched,
    so this is safe to run against a live archive (unlike a full reindex,
    which re-runs TOC arbitration).
    """
    image_token = (ocr_settings or settings).paddleocr_api_token
    resource_dir = settings.resources_dir / doc.id
    resource_dir.mkdir(parents=True, exist_ok=True)

    rows = {
        row.resource_id: row
        for row in (
            await db.execute(
                select(Resource).where(
                    Resource.doc_id == doc.id, Resource.type == "figure"
                )
            )
        ).scalars()
    }

    counts = {"downloaded": 0, "reused": 0, "failed": 0, "missing": 0}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for page, block, _titles, resource_id, kind in _iter_resource_blocks(
            parsed.pages
        ):
            if kind != "figure":
                continue
            row = rows.get(resource_id)
            if row is None:
                counts["missing"] += 1
                continue
            if row.image_path and Path(row.image_path).exists():
                counts["reused"] += 1
                continue
            url = _match_image_url(page, block)
            target = resource_dir / f"{resource_id}.jpg"
            if url and await _fetch_image(
                client,
                url,
                target,
                token=image_token,
                allowed_root=settings.ocr_dir / doc.id,
                allow_private=settings.tocmemex_ocr_allow_private_hosts,
            ):
                row.image_path = str(target)
                counts["downloaded"] += 1
            else:
                counts["failed"] += 1

    return {"doc_id": doc.id, **counts}


async def persist_parsed(
    db: AsyncSession,
    doc: Document,
    parsed: ParsedDocument,
    settings: Settings,
    ocr_settings: Settings | None = None,
    toc_trace: dict | None = None,
) -> list:
    """Build TOC/chunks/resources and refresh FTS for one parsed document.

    Caller owns the transaction and must have removed any previous rows.
    ``toc_trace`` (optional) receives the TOC provenance decisions.
    Returns the TOC node list for the caller's provenance row.
    """
    toc = build_toc(doc.id, parsed, settings, trace=toc_trace)
    chunk_data = chunk_document(
        parsed,
        toc,
        max_chars=settings.tocmemex_max_chunk_chars,
        overlap=settings.tocmemex_chunk_overlap,
    )

    chunk_models = [
        Chunk(
            doc_id=doc.id,
            node_id=chunk.node_id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            heading_path=chunk.heading_path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            token_count=count_tokens(chunk.content),
        )
        for chunk in chunk_data
    ]

    chunk_ranges = chunk_ranges_by_node(toc, chunk_data)

    # Document summaries are deliberately left empty (ADR-0002 §7.2 revised):
    # an extractive "first 300 chars" summary just repeats the document's
    # boilerplate preamble, and a TOC-derived one only restates what
    # ``get_toc`` already returns. Both were measured as noise. The field
    # stays for a future LLM-written or hand-written summary, which matters
    # for documents whose filename does not describe their content.
    # Keywords are still generated: they are a real document-filter signal
    # in ``search_doc_ids``.
    summary_text = " ".join(
        chunk.content for chunk in chunk_data[:3]
    ).strip()
    if summary_text and not doc.keywords:
        doc.keywords = extract_keywords(summary_text, 8)

    chunk_by_index = {chunk.chunk_index: chunk for chunk in chunk_data}
    first_chunk_by_node: dict[str, str] = {}
    for chunk in chunk_data:
        if chunk.node_id and chunk.node_id not in first_chunk_by_node:
            first_chunk_by_node[chunk.node_id] = chunk.content

    def _node_summary(node_id: str) -> str | None:
        content = first_chunk_by_node.get(node_id)
        if content:
            return extractive_summary(content, 200)
        start, end = chunk_ranges.get(node_id, (None, None))
        if start is None or end is None:
            return None
        for index in range(start, end + 1):
            chunk = chunk_by_index.get(index)
            if chunk is not None:
                return extractive_summary(chunk.content, 200)
        return None

    for node in toc:
        chunk_start, chunk_end = chunk_ranges.get(node.node_id, (None, None))
        db.add(
            TocNode(
                doc_id=doc.id,
                node_id=node.node_id,
                parent_id=node.parent_id,
                level=node.level,
                heading_num=node.heading_num,
                title=node.title,
                heading_path=node.heading_path,
                page_start=node.page_start,
                page_end=node.page_end,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                summary=_node_summary(node.node_id),
                source=node.source,
                confidence=node.confidence,
            )
        )

    db.add_all(chunk_models)
    await db.flush()

    await index_document(db, doc, chunk_models)
    await save_resources(db, doc, parsed, settings, ocr_settings=ocr_settings)
    await db.flush()
    await link_resources(db, doc.id)
    return toc


async def _resolve_ocr_settings(settings: Settings) -> Settings:
    """OCR config for this ingest: DB default endpoint, else .env.

    The DB is consulted through the module engine so a runtime endpoint
    switch takes effect on the next ingest without a restart. Any failure
    (engine not initialized, empty table, bad row) degrades to ``settings``
    — the .env path — which is exactly the pre-endpoint behaviour.
    """
    try:
        from ..ocr.endpoints import active_ocr_endpoint

        Session = session_factory(get_engine())
        async with Session() as db:
            resolved = await active_ocr_endpoint(db, settings)
    except Exception:
        logger.warning("OCR endpoint resolution failed; using .env", exc_info=True)
        return settings
    if resolved is None:
        logger.warning(
            "no LLM endpoint configured (DB default + .env both empty); "
            "纲目/编修 stay off and ingest takes the rule-based path"
        )
        return settings
    return resolved["settings"]


async def _resolve_llm_settings(settings: Settings) -> tuple[Settings, str | None]:
    """LLM config for this ingest: 纲目's bound endpoint, DB default, else .env.

    Mirrors ``_resolve_ocr_settings`` so the endpoint configured in the UI
    is the one ingest-time TOC parsing actually calls. The 纲目 config
    (prompts / tuning / enabled switch) rides along on the returned
    Settings, so ``llm_toc`` needs no extra arguments. Returns
    ``(settings, endpoint_name)``; ``endpoint_name`` is None when the
    .env path (or no LLM at all) applies.
    """
    try:
        from ..llm.purpose import resolve_runtime

        Session = session_factory(get_engine())
        async with Session() as db:
            resolved, endpoint_name, _cfg = await resolve_runtime(
                db, "gangmu", settings
            )
    except Exception:
        logger.warning("LLM endpoint resolution failed; using .env", exc_info=True)
        return settings, None
    if resolved is None:
        logger.warning(
            "no LLM endpoint for 纲目 (bound + DB default + .env all empty); "
            "TOC building takes the deterministic path"
        )
        return settings, None
    return resolved, endpoint_name


async def _attach_gangmu_lessons(
    engine, doc_id: str, settings: Settings
) -> Settings:
    """Attach lessons relevant to this document to the 纲目 config (§5.5).

    Retrieval is keyword-based over the document title plus "目录 TOC"
    (``index.activity.lesson_search``); the block rides on
    ``settings.tocmemex_gangmu["lessons"]`` and is appended to the
    transform / verify / fix prompts. Advisory only: any failure — no
    engine, empty table, odd row — returns the settings untouched.
    """
    try:
        from ..index.activity import lesson_search

        Session = session_factory(engine)
        async with Session() as db:
            doc = await db.get(Document, doc_id)
            if doc is None:
                return settings
            lessons = await lesson_search(db, f"{doc.title or ''} 目录 TOC", top_k=4)
        if not lessons:
            return settings
        # Lessons come from any MCP client, so they enter the prompt as
        # untrusted data: one JSON record per line, control characters and
        # delimiter tags stripped, per-field and total length capped. The
        # consumer (llm_toc._lessons_hint) wraps the block in an explicit
        # "data, not instructions" envelope.
        lines: list[str] = []
        for lesson in lessons:
            record = {
                "author": _clip_lesson(lesson.author_agent, 40),
                "trigger": _clip_lesson(lesson.trigger, 200),
                "issue": _clip_lesson(lesson.issue, 300),
                "solution": _clip_lesson(lesson.solution, 300),
            }
            lines.append(json.dumps(record, ensure_ascii=False))
        block = "\n".join(lines)[:2000]
        cfg = dict(settings.tocmemex_gangmu or {})
        cfg["lessons"] = block
        return settings.model_copy(update={"tocmemex_gangmu": cfg})
    except Exception:
        logger.warning("纲目 lesson retrieval failed; continuing without", exc_info=True)
        return settings


def _clip_lesson(value, limit: int) -> str:
    """One lesson field, safe to place inside the data envelope."""
    text = "".join(ch for ch in str(value or "") if ch in "\n\t" or ch.isprintable())
    text = text.replace("<lessons-data", "[lessons-data").replace(
        "</lessons-data", "[/lessons-data"
    )
    return text[:limit]


IMAGE_MODES = ("structured", "text")


def normalize_image_mode(value: str | None) -> str:
    """Only two image modes exist; anything else means the old behaviour."""
    mode = (value or "").strip().lower()
    return mode if mode in IMAGE_MODES else "structured"


def guess_parse_plan(path: Path, image_mode: str = "structured") -> str:
    """The scheme shown while ingestion runs, before the real channel is known.

    ``parse_plan_label`` replaces it once parsing proves which channel ran.
    """
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "OCR 纯文本" if image_mode == "text" else "OCR 结构化"
    if suffix == ".pdf":
        return "文本层优先"
    if suffix in {".docx", ".doc"}:
        return "DOCX 直读"
    if suffix in {".md", ".markdown", ".txt"}:
        return "Markdown 直读"
    return "表格直读"


def parse_stage(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES or suffix in {".pdf", ".docx", ".doc"}:
        return "OCR 识别" if suffix in IMAGE_SUFFIXES else "读取内容"
    return "读取内容"


def parse_plan_label(provenance: dict) -> str:
    """The scheme that actually produced the pages, in the queue's words."""
    channel = provenance.get("parse_channel")
    direct = {
        "pdf-text": "文本层直读",
        "docx-direct": "DOCX 直读",
        "markdown": "Markdown 直读",
        "table": "表格直读",
    }
    if channel in direct:
        return direct[channel]
    if channel in {"ocr", "ocr-text"}:
        kind = "纯文本" if channel == "ocr-text" else "结构化"
        model = provenance.get("ocr_model")
        return f"OCR {kind} · {model}" if model else f"OCR {kind}"
    return "自动"


async def _set_doc_state(engine: AsyncEngine, doc_id: str, **fields) -> None:
    """Write live ingest state in its own committed transaction.

    Ingest runs in a child process; the queue endpoint is read by the web
    process, so a stage update only becomes visible once it is committed.
    """
    async with session_factory(engine)() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        for key, value in fields.items():
            setattr(doc, key, value)
        doc.updated_at = utcnow_iso()
        await db.commit()


async def _mark_ingest_failed(engine: AsyncEngine, doc_id: str, exc: Exception) -> None:
    message = (str(exc) or exc.__class__.__name__)[:1000]
    try:
        await _set_doc_state(
            engine, doc_id, status="failed", stage="解析失败", progress=100, error=message
        )
    except Exception:
        logger.exception("could not mark document %s failed", doc_id)


async def _clear_derived_rows(db: AsyncSession, doc_id: str) -> None:
    for statement in (
        "DELETE FROM chunk_fts WHERE doc_id = :doc_id",
        "DELETE FROM doc_fts WHERE doc_id = :doc_id",
        "DELETE FROM resources WHERE doc_id = :doc_id",
        "DELETE FROM chunks WHERE doc_id = :doc_id",
        "DELETE FROM toc_nodes WHERE doc_id = :doc_id",
    ):
        await db.execute(text(statement), {"doc_id": doc_id})


async def _open_ingest_row(
    engine: AsyncEngine,
    path: Path,
    namespace: str,
    doc_type: str | None,
    doc_date: str | None,
    content_hash: str,
    plan: str,
    *,
    force: bool = False,
) -> tuple[str, bool]:
    """Create the documents row before parsing starts.

    The 解析队列 is derived from document status, so a row only appears
    there once one exists. Creating it up front means a long OCR job is
    visible (and progress-tracked) instead of invisible until it finishes.

    Returns ``(doc_id, should_parse)``. ``False`` means the same bytes are
    already in this namespace (ready or in flight) and the caller must stop.
    A previously *failed* row is reused and re-ingested in place, which is
    what makes re-uploading a file after a failure work instead of being
    reported as a duplicate forever. ``force`` re-uses a ready row the same
    way, for a deliberate corpus rebuild.
    """
    Session = session_factory(engine)
    async with Session() as db:
        await _ensure_namespace(db, namespace)
        existing = (
            await db.execute(
                select(Document).where(
                    Document.namespace_id == namespace,
                    Document.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status != "failed" and not force:
            return existing.id, False
        if existing is not None:
            existing.status = "parsing"
            existing.error = None
            existing.stage = "准备解析"
            existing.progress = 5
            existing.parse_plan = plan
            existing.title = path.stem
            existing.filename = path.name
            existing.file_path = str(path)
            existing.doc_type = doc_type or infer_doc_type(path)
            existing.doc_date = doc_date
            existing.page_count = None
            existing.char_count = None
            existing.summary = None
            existing.keywords = None
            # A retry is a fresh run for the queue's elapsed-time display;
            # keeping the old timestamp would show hours of "已用时".
            existing.created_at = utcnow_iso()
            existing.updated_at = utcnow_iso()
            await _clear_derived_rows(db, existing.id)
            await db.commit()
            return existing.id, True

        doc = Document(
            id=new_uuid(),
            namespace_id=namespace,
            title=path.stem,
            filename=path.name,
            file_path=str(path),
            content_hash=content_hash,
            doc_type=doc_type or infer_doc_type(path),
            doc_date=doc_date,
            source="upload",
            status="parsing",
            stage="准备解析",
            progress=5,
            parse_plan=plan,
        )
        db.add(doc)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent ingest of the same bytes won the insert.
            await db.rollback()
            existing = (
                await db.execute(
                    select(Document).where(
                        Document.namespace_id == namespace,
                        Document.content_hash == content_hash,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            return existing.id, False
        return doc.id, True


def _split_plain_text(text_value: str, max_chars: int, overlap: int) -> list[str]:
    """Size-based split for the image text mode (no headings to split on)."""
    value = re.sub(r"\n{3,}", "\n\n", text_value or "").strip()
    if not value:
        return []
    max_chars = max(1, max_chars)
    overlap = max(0, min(overlap, max_chars - 1))
    pieces: list[str] = []
    start = 0
    while start < len(value):
        end = min(start + max_chars, len(value))
        if end < len(value):
            cut = value.rfind("\n", start + max_chars // 2, end)
            if cut > start:
                end = cut
        piece = value[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(value):
            break
        start = max(end - overlap, start + 1)
    return pieces


async def persist_plain_text(
    db: AsyncSession,
    doc: Document,
    parsed: ParsedDocument,
    settings: Settings,
) -> list[TocNodeData]:
    """Store an image as one flat, searchable text document.

    The 纯文本 upload mode keeps the original image (``doc.file_path``) and
    its OCR text, but builds no TOC tree, no figure/table resources and no
    heading-derived chunks: screenshots and chat logs rarely have a real
    structure, and a fake TOC made them harder to read.
    """
    body = "\n\n".join(
        part for part in (page_text(page) for page in parsed.pages) if part.strip()
    ).strip()
    last_page = max(parsed.page_count - 1, 0)
    node = TocNodeData(
        node_id=f"{doc.id}:root",
        parent_id=None,
        level=1,
        heading_num=None,
        title="全文",
        heading_path="全文",
        page_start=0,
        page_end=last_page,
        source="plain",
        confidence=0.3,
    )
    anchor_nodes(doc.id, [node])

    pieces = _split_plain_text(
        body, settings.tocmemex_max_chunk_chars, settings.tocmemex_chunk_overlap
    )
    chunks = [
        Chunk(
            doc_id=doc.id,
            node_id=node.node_id,
            chunk_index=index,
            content=piece,
            heading_path="全文",
            page_start=0,
            page_end=last_page,
            token_count=count_tokens(piece),
        )
        for index, piece in enumerate(pieces)
    ]
    if body and not doc.keywords:
        doc.keywords = extract_keywords(body[:4000], 8)

    db.add(
        TocNode(
            doc_id=doc.id,
            node_id=node.node_id,
            parent_id=None,
            level=1,
            heading_num=None,
            title=node.title,
            heading_path=node.heading_path,
            page_start=0,
            page_end=last_page,
            chunk_start=0 if chunks else None,
            chunk_end=len(chunks) - 1 if chunks else None,
            summary=extractive_summary(chunks[0].content, 200) if chunks else None,
            source=node.source,
            confidence=node.confidence,
        )
    )
    db.add_all(chunks)
    await db.flush()
    await index_document(db, doc, chunks)
    await db.flush()
    return [node]


async def ingest_path(
    path: Path,
    namespace: str = "shared",
    doc_type: str | None = None,
    doc_date: str | None = None,
    ocr_client: BaseOcrProvider | None = None,
    settings: Settings | None = None,
    image_mode: str = "structured",
    force: bool = False,
    force_ocr: bool = False,
) -> str:
    """Ingest one file. ``force`` re-parses an already-ingested file in
    place; ``force_ocr`` additionally skips direct text extraction."""
    settings = settings or get_settings()
    path = Path(path).resolve()
    suffix = path.suffix.lower()
    image_mode = normalize_image_mode(image_mode) if suffix in IMAGE_SUFFIXES else "structured"
    engine = get_engine()
    content_hash = _sha256(path)

    doc_id, should_parse = await _open_ingest_row(
        engine,
        path,
        namespace,
        doc_type,
        doc_date,
        content_hash,
        guess_parse_plan(path, image_mode),
        force=force,
    )
    if not should_parse:
        return doc_id

    ocr_settings = (
        await _resolve_ocr_settings(settings)
        if ocr_client is None
        and (
            suffix in {".pdf", ".docx", ".doc"}
            or suffix in IMAGE_SUFFIXES
        )
        else settings
    )
    llm_settings, llm_endpoint_name = await _resolve_llm_settings(settings)
    llm_settings = await _attach_gangmu_lessons(engine, doc_id, llm_settings)
    parse_provenance: dict = {}
    try:
        await _set_doc_state(engine, doc_id, stage=parse_stage(path), progress=15)
        ocr_started = time.monotonic()
        parsed = await _parse(
            path,
            doc_id,
            settings,
            ocr_client,
            ocr_settings,
            provenance=parse_provenance,
            image_mode=image_mode,
            force_ocr=force_ocr,
        )
    except Exception as exc:
        logger.exception("ingest parse failed for %s", doc_id)
        await _mark_ingest_failed(engine, doc_id, exc)
        raise
    ocr_elapsed_ms = int((time.monotonic() - ocr_started) * 1000)
    save_bookmarks(settings, doc_id, parsed.bookmarks)
    await _set_doc_state(
        engine,
        doc_id,
        stage="整理结构",
        progress=60,
        parse_plan=parse_plan_label(parse_provenance),
        page_count=parsed.page_count,
        char_count=sum(len(page_text(page)) for page in parsed.pages),
    )

    Session = session_factory(engine)
    try:
        async with Session() as db:
            doc = await db.get(Document, doc_id)
            if doc is None:
                raise RuntimeError(f"ingest row disappeared: {doc_id}")
            doc.status = "indexing"
            doc.stage = "切块索引"
            doc.progress = 85
            doc.error = None

            toc_trace: dict = {}
            llm_started = time.monotonic()
            if image_mode == "text" and suffix in IMAGE_SUFFIXES:
                toc = await persist_plain_text(db, doc, parsed, settings)
            else:
                toc = await persist_parsed(
                    db,
                    doc,
                    parsed,
                    llm_settings,
                    ocr_settings=ocr_settings,
                    toc_trace=toc_trace,
                )
            llm_elapsed_ms = int((time.monotonic() - llm_started) * 1000)
            if toc_trace.get("llm_used"):
                toc_trace["llm_elapsed_ms"] = llm_elapsed_ms
                toc_trace["llm_endpoint_name"] = llm_endpoint_name

            doc.status = "ready"
            doc.stage = "完成"
            doc.progress = 100
            doc.updated_at = utcnow_iso()
            await record_run(
                db,
                build_run(
                    doc,
                    recorded_by="ingest",
                    parse=parse_provenance,
                    toc_trace=toc_trace,
                    nodes=toc,
                    ocr_elapsed_ms=ocr_elapsed_ms,
                    ocr_cache="pages.json"
                    if (settings.ocr_dir / doc_id / "pages.json").exists()
                    else "raw.jsonl",
                ),
            )
            await db.commit()
    except Exception as exc:
        logger.exception("ingest persist failed for %s", doc_id)
        await _mark_ingest_failed(engine, doc_id, exc)
        raise
    return doc_id