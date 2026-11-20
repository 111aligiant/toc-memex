"""Mistral OCR (``POST /v1/ocr``).

Mistral returns per-page Markdown plus images with bounding boxes. It has
no per-text-block bbox or header/footer labels, so blocks are reconstructed
from Markdown (headings, tables, figures, formulas) with synthetic bboxes;
see ``app.ocr.normalize.markdown_to_blocks``.

Request (PDF inline, no upload step):

    POST https://api.mistral.ai/v1/ocr
    {"model": "mistral-ocr-latest",
     "document": {"type": "document_url",
                  "document_url": "data:application/pdf;base64,..."},
     "include_image_base64": true,
     "table_format": "html"}

Docs: https://docs.mistral.ai/capabilities/document_ai/basic_ocr/
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..models import OcrPage, ParsedDocument
from ..normalize import markdown_to_blocks
from .base import BaseOcrProvider, OcrProviderError


class MistralOcrError(OcrProviderError):
    pass


class MistralOcrProvider(BaseOcrProvider):
    name = "mistral"

    def check_config(self) -> None:
        if not self.settings.mistral_api_key:
            raise MistralOcrError("MISTRAL_API_KEY is not configured")

    def _headers(self) -> dict[str, str]:
        self.check_config()
        return {
            "Authorization": f"Bearer {self.settings.mistral_api_key}",
            "Content-Type": "application/json",
        }

    def _probe_target(self) -> tuple[str, dict[str, str]]:
        # /v1/models is a real catalog endpoint: 200 proves key + base URL.
        url = self.settings.mistral_api_url.rstrip("/")
        if url.endswith("/ocr"):
            url = url[: -len("/ocr")]
        return f"{url}/models", {"Authorization": f"Bearer {self.settings.mistral_api_key}"}

    async def _run(
        self, client: httpx.AsyncClient, pdf_path: Path, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        encoded = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        body = {
            "model": self.settings.mistral_ocr_model,
            "document": {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{encoded}",
            },
            "include_image_base64": True,
            "table_format": "html",
        }
        last: Exception | None = None
        payload: dict | None = None
        await self._guard_url(self.settings.mistral_api_url)
        for attempt in range(3):
            try:
                response = await client.post(
                    self.settings.mistral_api_url,
                    headers=self._headers(),
                    json=body,
                )
            except httpx.HTTPError as exc:
                last = exc
            else:
                if response.status_code == 200:
                    payload = response.json()
                    break
                message = (
                    f"OCR failed HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
                if response.status_code < 500:
                    raise MistralOcrError(message)
                last = MistralOcrError(message)
            if attempt < 2:
                await self._sleep(2 * (attempt + 1))
        if payload is None:
            raise MistralOcrError(f"OCR failed after retries: {last}")

        pages_data = payload.get("pages")
        if not isinstance(pages_data, list) or not pages_data:
            raise MistralOcrError("OCR response has no pages")

        pages: list[OcrPage] = []
        for offset, item in enumerate(pages_data):
            dimensions = item.get("dimensions") or {}
            width = float(dimensions.get("width") or 0)
            height = float(dimensions.get("height") or 0)
            markdown = str(item.get("markdown") or "")
            images: dict[str, str] = {}
            for image in item.get("images") or []:
                url = image.get("image_base64") or image.get("image_url")
                if not url:
                    continue
                x0 = image.get("top_left_x")
                y0 = image.get("top_left_y")
                x1 = image.get("bottom_right_x")
                y1 = image.get("bottom_right_y")
                # The key encodes the bbox so save_resources can match the
                # figure block to its image (same convention as Paddle).
                key = (
                    f"img_{x0}_{y0}_{x1}_{y1}.jpg"
                    if None not in (x0, y0, x1, y1)
                    else f"{image.get('id') or 'img'}.jpg"
                )
                images[key] = str(url)
            pages.append(
                OcrPage(
                    page_index=int(item.get("index") if item.get("index") is not None else offset),
                    width=width,
                    height=height,
                    markdown=markdown,
                    blocks=markdown_to_blocks(markdown, width, height),
                    images=images,
                    raw=item,
                )
            )
        pages.sort(key=lambda page: page.page_index)
        for index, page in enumerate(pages):
            page.page_index = index

        import json

        return (
            ParsedDocument(
                pages=pages,
                bookmarks=[],
                metadata={
                    "ocr_provider": self.name,
                    "ocr_model": str(payload.get("model") or self.settings.mistral_ocr_model),
                },
            ),
            json.dumps(payload, ensure_ascii=False),
        )
