from __future__ import annotations

import re

_NUM_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")
# Number butted directly against a CJK/Latin title ("4.0温度－高度"). The
# first title character must not be a digit, dot or dash, so dates and
# hyphenated identifiers are not mistaken for headings.
_NUM_HEADING_TIGHT = re.compile(
    r"^(\d+(?:\.\d+)*)([^\W\d_.\-–—\s].*)$", re.UNICODE
)
_CN_CHAPTER = re.compile(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*章\s*(.*)$")
_APPENDIX = re.compile(
    r"^(附录|Annex|Appendix)\s*([A-Za-z])\s*(.*)$", re.IGNORECASE
)
_LETTER_HEADING = re.compile(r"^([A-Z](?:\.\d+)*)\s+(.+)$")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_LEADING_NUMBER = re.compile(r"^[０-９0-9](?:[０-９0-9\s．.]*[０-９0-9])?\s*")


def _normalize_leading_number(text: str) -> str:
    """Normalize ``5 . 1`` / ``４．１`` headings to ``5.1`` / ``4.1``.

    Word and PDF outlines often insert spaces around every dot, and some
    text layers use full-width digits. Only the leading number is touched;
    the title keeps its original spacing.
    """
    match = _LEADING_NUMBER.match(text)
    if not match:
        return text
    rest = text[match.end() :].strip()
    if not rest or not rest[0].isalnum():
        # Dates (2026-11-11), list markers (1)), and similar are not headings.
        return text
    number = match.group(0).translate(_FULLWIDTH_DIGITS).replace("．", ".")
    number = re.sub(r"\s*\.\s*", ".", number).strip()
    if not number or "." not in number and not number.isdigit():
        return text
    return f"{number} {rest}"


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def cn_number_to_int(text: str) -> int | None:
    """Convert ``十四``/``二十六``/``一百零五`` to an integer."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def parse_heading(text: str) -> tuple[str | None, str, int] | None:
    """Return (heading_num, title, level) for a heading-like line."""
    text = " ".join(text.split())
    if not text:
        return None
    text = _normalize_leading_number(text)
    match = _NUM_HEADING.match(text) or _NUM_HEADING_TIGHT.match(text)
    if match:
        num, title = match.group(1), match.group(2).strip()
        return num, title, num.count(".") + 1
    match = _CN_CHAPTER.match(text)
    if match:
        num = f"第{match.group(1)}章"
        title = match.group(2).strip() or text
        return num, title, 1
    match = _APPENDIX.match(text)
    if match:
        prefix = match.group(1)
        letter = match.group(2).upper()
        if prefix.lower() in ("annex", "appendix"):
            num = f"{prefix.capitalize()} {letter}"
        else:
            num = f"附录{letter}"
        title = match.group(3).strip() or text
        return num, title, 1
    match = _LETTER_HEADING.match(text)
    if match:
        num, title = match.group(1), match.group(2).strip()
        return num, title, num.count(".") + 1
    if 2 <= len(text) <= 100:
        return None, text, 1
    return None


def chapter_key(heading_num: str | None) -> str | None:
    """Top-level chapter key for ``1.2.3`` → ``1``, ``附录A`` → ``A``."""
    if not heading_num:
        return None
    match = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*章", heading_num)
    if match:
        value = cn_number_to_int(match.group(1))
        return str(value) if value is not None else match.group(1)
    match = re.match(
        r"^(?:附录|Annex|Appendix)\s*([A-Za-z])", heading_num, re.IGNORECASE
    )
    if match:
        return match.group(1).upper()
    match = re.match(r"^([A-Z])(?:\.\d+)*$", heading_num)
    if match:
        return match.group(1)
    match = re.match(r"^(\d+)", heading_num)
    if match:
        return match.group(1)
    return None