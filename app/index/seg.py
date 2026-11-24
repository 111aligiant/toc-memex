from __future__ import annotations

import re

# CJK Unified Ideographs (plus common supplementary ranges). The default
# unicode61 FTS5 tokenizer treats every CJK character as its own token
# boundary only when separated — a contiguous CJK run has no separators, so
# FTS5 stores the whole run as ONE token and subword matching fails. jieba
# recovered terms by word-splitting the run; this module supplies the
# character unigram + bigram view instead ("数据" -> 数 / 数据 / 据).
_CJK_SPAN_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]+")
# Word tokens for non-CJK runs: Latin words, numbers, keep digits intact so
# "2026" stays a token rather than four digits. A single non-alphanumeric
# symbol is a token too.
_WORD_OR_SYMBOL_RE = re.compile(r"[A-Za-z0-9]+|[^\w\s]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_SPAN_RE.fullmatch(ch))


def cjk_n_grams(value: str, max_n: int = 2) -> list[str]:
    """Character n-grams (unigrams + bigrams) of CJK spans, word tokens else.

    Chinese text between two Latin/space runs is contiguous with no
    separators, so this splits each CJK span into single characters and
    adjacent bigrams. Non-CJK runs (English words, numbers, symbols) are
    taken whole so "reactor" keeps matching "reactor design" and "2026"
    stays searchable as one token.
    """
    tokens: list[str] = []
    pos = 0
    for match in _CJK_SPAN_RE.finditer(value or ""):
        if match.start() > pos:
            tokens.extend(_non_cjk_tokens(value[pos : match.start()]))
        run = match.group(0)
        tokens.extend(run)  # unigrams
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # bigrams
        pos = match.end()
    if pos < len(value or ""):
        tokens.extend(_non_cjk_tokens(value[pos:]))
    return tokens


def _non_cjk_tokens(text: str) -> list[str]:
    return [tok for tok in _WORD_OR_SYMBOL_RE.findall(text) if not tok.isspace()]


def char_chunks(value: str) -> list[str]:
    """Single-CJK-character view (for short-query LIKE fallbacks)."""
    out: list[str] = []
    for match in _CJK_SPAN_RE.finditer(value or ""):
        out.extend(match.group(0))
    return out


def contains_cjk(text: str) -> bool:
    return bool(_CJK_SPAN_RE.search(text or ""))


def is_cjk(ch: str) -> bool:
    """True for a single CJK ideograph (Han block)."""
    return len(ch) == 1 and _CJK_SPAN_RE.fullmatch(ch) is not None