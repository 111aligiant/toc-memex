from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from PIL import Image, ImageDraw
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.ingest.pdf_media import attach_pdf_text_media
from app.ingest.pdf_text import parse_pdf_text
from app.ocr.parser import page_text

_BODY = "这是用于验证文本层可用性的正文第{}行，内容是一段足够长的中文说明文字。"


def _make_pdf(path: Path, image_path: Path) -> None:
    image = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 190, 90), outline="black", width=3)
    image.save(image_path)

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    sheet = canvas.Canvas(str(path), pagesize=(595, 842))
    sheet.setFont("STSong-Light", 10)

    for index in range(15):
        sheet.drawString(60, 842 - (60 + index * 14), _BODY.format(index + 1))

    xs = [60, 160, 260, 400, 540]
    ys = [300, 330, 360, 390, 420]
    to_pdf = lambda value: 842 - value  # noqa: E731 - test helper, top-down -> PostScript
    for x in xs:
        sheet.line(x, to_pdf(ys[0]), x, to_pdf(ys[-1]))
    for row, y_value in enumerate(ys):
        sheet.line(xs[0], to_pdf(y_value), xs[-1], to_pdf(y_value))
        if row in {1, 2, 3}:
            # The first column has no internal horizontal rule: one merged cell.
            sheet.line(xs[1], to_pdf(y_value), xs[-1], to_pdf(y_value))

    sheet.drawString(60, to_pdf(ys[0] - 18), "表 1 测试表")
    values = [["类别", "项目1", "项目2"], ["甲", "A1", "A2"], ["乙", "B1", "B2"], ["丙", "C1", "C2"]]
    for row, cells in enumerate(values):
        for column, value in enumerate(cells):
            sheet.drawString(xs[column] + 8, to_pdf(ys[row] + 20), value)

    sheet.drawImage(str(image_path), 80, to_pdf(600), width=240, height=120)
    sheet.drawString(80, to_pdf(630), "图 1 测试图")
    sheet.showPage()
    sheet.save()


def test_text_layer_pdf_gets_tables_figures_and_captions(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, tmp_path / "figure.png")

    parsed = parse_pdf_text(pdf_path)
    assert parsed is not None
    attach_pdf_text_media(pdf_path, parsed, tmp_path / "ocr" / "doc" / "text-layer")
    page = parsed.pages[0]

    tables = [block for block in page.blocks if block.label == "table"]
    figures = [block for block in page.blocks if block.label == "figure"]
    captions = [block for block in page.blocks if block.label == "figure_title"]

    assert len(tables) == 1
    assert "<table>" in tables[0].content
    assert "项目1" in tables[0].content
    assert len(figures) == 1
    assert any("图 1 测试图" in block.content for block in captions)
    assert page.images
    for url in page.images.values():
        assert url.startswith("file:")
        assert Path(url2pathname(urlparse(url).path)).exists()

    text = page_text(page)
    # The table's cell text is not also emitted as loose lines...
    assert "A1 A2" not in text
    # ...and the table itself keeps a rectangular Markdown shape.
    table_lines = [line for line in text.splitlines() if line.startswith("|")]
    assert table_lines
    assert len({line.count("|") for line in table_lines}) == 1


def _dark_pixels(image: Image.Image) -> int:
    return sum(image.convert("L").histogram()[:128])


def _block_pdf(path: Path, block_top: float, block_bottom: float) -> None:
    """One black rectangle spanning the full width at the given top-down y."""
    sheet = canvas.Canvas(str(path), pagesize=(595, 842))
    sheet.setFillColorRGB(0, 0, 0)
    # reportlab draws bottom-up; convert top-down y to PostScript y.
    height = block_bottom - block_top
    sheet.rect(60, 842 - block_bottom, 475, height, fill=1, stroke=0)
    sheet.showPage()
    sheet.save()


def test_render_crop_keeps_top_half_block(tmp_path):
    """Regression for the vertically mirrored crop (B1).

    The crop is passed to pypdfium2 in bottom-up cut-off space; the old code
    used pdfplumber top-down values, which rendered a top-half figure blank
    (0 dark pixels) so ``_has_ink`` dropped it. The correct crop must keep
    the block. This distinguishes the two formulas: the old one produced 0
    dark pixels, the fixed one keeps the whole block.
    """
    import pypdfium2 as pdfium

    from app.ingest.pdf_media import _render

    pdf_path = tmp_path / "top.pdf"
    _block_pdf(pdf_path, block_top=50, block_bottom=150)
    output = tmp_path / "top.jpg"

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page = document[0]
        # pdfplumber top-down rect (x0, top, x1, bottom) around the block.
        kept = _render(page, 595, 842, (60, 50, 535, 150), output)
    finally:
        document.close()

    assert kept is True
    with Image.open(output) as image:
        dark = _dark_pixels(image)
    # The old (mirrored) formula rendered this to ~0 dark pixels; the correct
    # one keeps the full black block. Assert well above the blank threshold.
    assert dark > 5000, f"top-half block was cropped away (dark_px={dark})"


def test_render_crop_keeps_bottom_half_block(tmp_path):
    """The symmetric case: a bottom-half block must also survive the crop."""
    import pypdfium2 as pdfium

    from app.ingest.pdf_media import _render

    pdf_path = tmp_path / "bottom.pdf"
    _block_pdf(pdf_path, block_top=692, block_bottom=792)
    output = tmp_path / "bottom.jpg"

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page = document[0]
        kept = _render(page, 595, 842, (60, 692, 535, 792), output)
    finally:
        document.close()

    assert kept is True
    with Image.open(output) as image:
        dark = _dark_pixels(image)
    assert dark > 5000, f"bottom-half block was cropped away (dark_px={dark})"