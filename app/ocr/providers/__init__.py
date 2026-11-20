"""Provider registry: pick the OCR backend from settings.

``build_ocr_provider(settings)`` returns the provider selected by
``TOCMEMEX_OCR_PROVIDER`` (default ``paddle``). Unknown names raise a
ValueError listing the supported ones instead of silently falling back, so
a typo in ``.env`` surfaces on the first ingest rather than as wrong data.

Adding a provider: subclass ``BaseOcrProvider``, implement ``_run`` and
``check_config``, and register it in ``_PROVIDERS``.
"""

from __future__ import annotations

from ...config import Settings
from .azure import AzureOcrProvider
from .base import BaseOcrProvider, OcrProviderError
from .mineru import MineruOcrProvider
from .mistral import MistralOcrProvider
from .paddle import PaddleOcrError, PaddleOcrProvider

_PROVIDERS: dict[str, type[BaseOcrProvider]] = {
    "paddle": PaddleOcrProvider,
    "mistral": MistralOcrProvider,
    "mineru": MineruOcrProvider,
    "azure": AzureOcrProvider,
}

SUPPORTED_PROVIDERS = tuple(_PROVIDERS)


def build_ocr_provider(
    settings: Settings, transport=None
) -> BaseOcrProvider:
    name = (settings.tocmemex_ocr_provider or "paddle").strip().lower()
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise ValueError(
            f"unknown OCR provider {name!r}; supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}"
        )
    return provider(settings, transport=transport)


__all__ = [
    "AzureOcrProvider",
    "BaseOcrProvider",
    "MineruOcrProvider",
    "MistralOcrProvider",
    "OcrProviderError",
    "PaddleOcrError",
    "PaddleOcrProvider",
    "SUPPORTED_PROVIDERS",
    "build_ocr_provider",
]
