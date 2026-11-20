from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.ingest.word import convert_docx


@pytest.mark.skipif(
    os.environ.get("TOCMEMEX_RUN_WORD") != "1",
    reason="set TOCMEMEX_RUN_WORD=1 and TOCMEMEX_WORD_SAMPLE=<docx> to run Word COM integration",
)
def test_convert_real_docx(tmp_path):
    sample = Path(os.environ["TOCMEMEX_WORD_SAMPLE"])
    result = convert_docx(sample, tmp_path / "out.pdf")
    assert result.pdf_path.exists()
    assert result.pdf_path.stat().st_size > 0
    assert result.bookmarks