from __future__ import annotations

import re

import jieba.analyse


def extractive_summary(text: str, max_chars: int = 300) -> str:
    """Zero-cost summary: first sentence(s) up to ``max_chars``."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    for mark in ("。", "；", "！", "？", ".", ";", "，", ","):
        cut = window.rfind(mark)
        if cut >= max_chars // 3:
            return window[: cut + 1].strip()
    return window.strip()


def extract_keywords(text: str, top_k: int = 8) -> str:
    """Cheap TF-IDF keywords via jieba; no LLM or embedding dependency."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        tags = jieba.analyse.extract_tags(text, topK=top_k)
    except Exception:
        return ""
    return ", ".join(tag for tag in tags if tag)