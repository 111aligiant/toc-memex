"""Resize the transparent-corner favicon for embedding (64x64)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image


def resize_favicon(src: Path, dst: Path, size: int = 64) -> None:
    im = Image.open(src).convert("RGBA")
    im = im.resize((size, size), Image.LANCZOS)
    im.save(dst, "PNG")
    print(f"saved {dst}  size={im.size}")


if __name__ == "__main__":
    resize_favicon(Path("assets/icon-favicon.png"), Path("assets/icon-64.png"), 64)