"""Backward-compatible aliases for the original Paddle-only client.

The implementation moved to ``app.ocr.providers``; this module keeps
``PaddleOcrClient`` / ``PaddleOcrError`` importable so existing callers and
tests do not need to change. New code should use
``app.ocr.providers.build_ocr_provider``.
"""

from __future__ import annotations

from .providers.paddle import PaddleOcrError, PaddleOcrProvider

PaddleOcrClient = PaddleOcrProvider

__all__ = ["PaddleOcrClient", "PaddleOcrError"]
