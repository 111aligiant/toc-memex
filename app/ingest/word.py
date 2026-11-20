from __future__ import annotations

import ctypes
import os
import re
import shutil
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from ..ocr.models import Bookmark

# Word COM constants.
WD_ACTIVE_END_PAGE_NUMBER = 3
WD_EXPORT_FORMAT_PDF = 17
WD_EXPORT_CREATE_HEADING_BOOKMARKS = 1
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3


class _Win32FindStreamData(ctypes.Structure):
    _fields_ = [
        ("StreamSize", ctypes.c_longlong),
        ("StreamName", ctypes.c_wchar * 296),
    ]


def has_zone_identifier(path: Path) -> bool:
    """True when Windows marked the file as downloaded from the internet.

    Word opens those in Protected View. Protected View blocks COM automation
    and can leave a non-interactive caller waiting until Word is killed.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        find_first = kernel32.FindFirstStreamW
        find_first.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(_Win32FindStreamData),
            wintypes.DWORD,
        ]
        find_first.restype = wintypes.HANDLE
        find_next = kernel32.FindNextStreamW
        find_next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Win32FindStreamData),
        ]
        find_next.restype = wintypes.BOOL
        find_close = kernel32.FindClose
        find_close.argtypes = [wintypes.HANDLE]

        data = _Win32FindStreamData()
        handle = find_first(str(path), 0, ctypes.byref(data), 0)
        if handle == wintypes.HANDLE(-1).value:
            return False
        try:
            while True:
                if "Zone.Identifier" in data.StreamName:
                    return True
                if not find_next(handle, ctypes.byref(data)):
                    return False
        finally:
            find_close(handle)
    except (AttributeError, OSError):
        return False


def open_word_document(src: Path):
    """Open a document with Word COM: no Protected View, macros disabled.

    Returns ``(app, doc, temp_copy)``; pass all three to
    :func:`close_word_document`.
    """
    import pythoncom
    import win32com.client

    src = Path(src).resolve()
    temp_copy: Path | None = None
    if has_zone_identifier(src):
        fd, temp_name = tempfile.mkstemp(prefix="toc-memex-", suffix=src.suffix)
        os.close(fd)
        temp_copy = Path(temp_name)
        shutil.copyfile(src, temp_copy)
        src = temp_copy

    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("Word.Application")
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        try:
            app.Options.UpdateLinksAtOpen = False
        except Exception:
            pass
        doc = app.Documents.Open(
            str(src),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            OpenAndRepair=False,
            NoEncodingDialog=True,
        )
    except Exception:
        app.Quit()
        pythoncom.CoUninitialize()
        if temp_copy is not None:
            temp_copy.unlink(missing_ok=True)
        raise
    return app, doc, temp_copy


def close_word_document(app, doc, temp_copy: Path | None) -> None:
    import pythoncom

    try:
        if doc is not None:
            doc.Close(False)
    finally:
        if app is not None:
            app.Quit()
        pythoncom.CoUninitialize()
        if temp_copy is not None:
            temp_copy.unlink(missing_ok=True)


@dataclass
class WordConversion:
    pdf_path: Path
    bookmarks: list[Bookmark]


def _clean_title(text: str) -> str:
    return text.replace("\r", "").replace("\x07", "").strip()


def _heading_num(title: str) -> str | None:
    match = re.match(r"^(\d+(?:\.\d+)*)", title)
    if match:
        return match.group(1)
    match = re.match(r"^附录\s*([A-Z])", title)
    if match:
        return match.group(1)
    return None


def extract_headings(doc) -> list[Bookmark]:
    """Read Word paragraphs whose OutlineLevel is a heading (1-9).

    ``wdActiveEndPageNumber`` is 1-based; stored Bookmark.page_index is 0-based.
    """
    bookmarks: list[Bookmark] = []
    for para in doc.Paragraphs:
        level = int(para.OutlineLevel)
        if not 1 <= level <= 9:
            continue
        title = _clean_title(para.Range.Text)
        if not title:
            continue
        page = int(para.Range.Information(WD_ACTIVE_END_PAGE_NUMBER)) - 1
        bookmarks.append(
            Bookmark(
                level=level,
                title=title,
                page_index=max(page, 0),
                heading_num=_heading_num(title),
            )
        )
    return bookmarks


def convert_docx(src: Path, dst: Path) -> WordConversion:
    """Convert a DOCX/DOC to PDF with Word COM and capture heading bookmarks."""
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    app, doc, temp_copy = open_word_document(src)
    try:
        bookmarks = extract_headings(doc)
        doc.ExportAsFixedFormat(
            str(dst),
            WD_EXPORT_FORMAT_PDF,
            CreateBookmarks=WD_EXPORT_CREATE_HEADING_BOOKMARKS,
        )
        return WordConversion(pdf_path=dst, bookmarks=bookmarks)
    finally:
        close_word_document(app, doc, temp_copy)