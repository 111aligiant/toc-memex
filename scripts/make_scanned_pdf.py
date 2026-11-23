"""Render a text PDF into an image-only PDF (a reproducible "clean scan").

Used by the 2026-11-23 spike to exercise the OCR path without a real scanner.
Each page is rasterised with pypdfium2, stored as JPEG with Pillow, and the
JPEGs are assembled into a new PDF that carries no text layer.

``--degrade`` adds the usual scanner artefacts (small rotation, grain,
grayscale). It is off by default so the clean control stays reproducible.

Usage:
    python scripts/make_scanned_pdf.py --in input.pdf --out output.pdf
        [--dpi 200] [--jpeg-quality 85] [--degrade]
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render_pages(
    source: Path, target: Path, dpi: int, quality: int, degrade: bool, out_dir: Path
) -> int:
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    doc = pdfium.PdfDocument(str(source))
    try:
        scale = dpi / 72
        frames: list[Path] = []
        rng = random.Random(1123)  # deterministic "scan" noise when degraded
        for index in range(len(doc)):
            page = doc[index]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            if degrade:
                angle = rng.uniform(-0.5, 0.5)
                image = image.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
                image = image.convert("L")
                grain = Image.effect_noise(image.size, 6).convert("L")
                image = Image.blend(image, grain, 0.06).filter(ImageFilter.SMOOTH)
            frame = out_dir / f"page-{index + 1:04d}.jpg"
            image.save(frame, "JPEG", quality=quality, optimize=True)
            frames.append(frame)
        if not frames:
            raise ValueError(f"PDF has no pages: {source}")

        images = [Image.open(frame).convert("RGB") for frame in frames]
        first, rest = images[0], images[1:]
        first.save(
            target,
            "PDF",
            resolution=dpi,
            save_all=True,
            append_images=rest,
            quality=quality,
        )
        for image in images:
            image.close()
        return len(frames)
    finally:
        doc.close()


def _text_chars(path: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return sum(len(doc[i].get_textpage().get_text_range()) for i in range(len(doc)))
    finally:
        doc.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="source", type=Path, required=True)
    parser.add_argument("--out", dest="target", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument(
        "--degrade",
        action="store_true",
        help="add small rotation, grain and grayscale (simulated scanner)",
    )
    args = parser.parse_args(argv)

    source = args.source.resolve()
    target = args.target.resolve()
    if not source.is_file():
        print(f"input not found: {source}", file=sys.stderr)
        return 2
    if target.exists():
        print(f"refusing to overwrite: {target}", file=sys.stderr)
        return 2
    if not 72 <= args.dpi <= 600:
        print("--dpi must be between 72 and 600", file=sys.stderr)
        return 2
    if not 30 <= args.jpeg_quality <= 100:
        print("--jpeg-quality must be between 30 and 100", file=sys.stderr)
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tocmemex-scan-") as tmp:
        pages = _render_pages(
            source, target, args.dpi, args.jpeg_quality, args.degrade, Path(tmp)
        )
        print(f"wrote {target} pages={pages} dpi={args.dpi} degrade={args.degrade}")

    chars = _text_chars(target)
    print(f"text layer chars={chars} pages={pages}")
    if chars > pages * 20:
        print(
            "warning: output still carries a text layer; scan conversion failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
