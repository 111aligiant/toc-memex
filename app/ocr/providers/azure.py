"""Azure AI Document Intelligence ``prebuilt-layout``.

Flow: POST the PDF bytes to ``:analyze`` → 202 + ``Operation-Location`` →
poll that URL until ``status=succeeded`` → read ``analyzeResult``.

What we map:

- ``paragraphs[].role`` → block label: ``title``/``sectionHeading`` →
  ``paragraph_title``, ``pageHeader``/``pageFooter``/``pageNumber`` →
  ``header``/``footer``/``number``, ``footnote`` → ``footnote``, else ``text``.
- ``tables[]`` → ``table`` blocks (cells rebuilt as HTML so the existing
  ``_html_to_text`` path keeps working).
- ``figures[]`` → ``figure`` blocks (bbox + caption; the image itself needs
  a separate figures endpoint and is not downloaded).

Docs: https://learn.microsoft.com/azure/ai-services/document-intelligence/prebuilt/layout
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

import httpx

from ..models import OcrBlock, OcrPage, ParsedDocument
from ..normalize import polygon_to_bbox
from .base import BaseOcrProvider, OcrProviderError

_ROLE_LABELS = {
    "title": "paragraph_title",
    "sectionHeading": "paragraph_title",
    "pageHeader": "header",
    "pageFooter": "footer",
    "pageNumber": "number",
    "footnote": "footnote",
}


class AzureOcrError(OcrProviderError):
    pass


class AzureOcrProvider(BaseOcrProvider):
    name = "azure"

    def check_config(self) -> None:
        if not self.settings.azure_di_endpoint or not self.settings.azure_di_key:
            raise AzureOcrError("AZURE_DI_ENDPOINT / AZURE_DI_KEY are not configured")

    def _headers(self) -> dict[str, str]:
        self.check_config()
        return {"Ocp-Apim-Subscription-Key": self.settings.azure_di_key}

    def _analyze_url(self) -> str:
        endpoint = self.settings.azure_di_endpoint.rstrip("/")
        return (
            f"{endpoint}/documentintelligence/documentModels/"
            f"{self.settings.azure_di_model}:analyze"
            f"?api-version={self.settings.azure_di_api_version}"
        )

    def _probe_target(self) -> tuple[str, dict[str, str]]:
        # Model catalog: 200 proves endpoint + key without analyzing a file.
        endpoint = self.settings.azure_di_endpoint.rstrip("/")
        return (
            f"{endpoint}/documentintelligence/documentModels"
            f"?api-version={self.settings.azure_di_api_version}",
            self._headers(),
        )

    async def _run(
        self, client: httpx.AsyncClient, pdf_path: Path, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        analyze_url = self._analyze_url()
        await self._guard_url(analyze_url)
        try:
            response = await client.post(
                analyze_url,
                headers={
                    **self._headers(),
                    "Content-Type": "application/pdf",
                },
                content=pdf_path.read_bytes(),
            )
        except httpx.HTTPError as exc:
            raise AzureOcrError(f"analyze request failed: {exc}") from exc
        if response.status_code not in (200, 202):
            raise AzureOcrError(
                f"analyze failed HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        operation = response.headers.get("Operation-Location")
        if not operation:
            raise AzureOcrError("analyze response missing Operation-Location")

        result = await self._poll(client, operation)
        raw = json.dumps(result, ensure_ascii=False)
        return self._to_document(result), raw

    async def _poll(self, client: httpx.AsyncClient, url: str) -> dict:
        # The URL is the vendor's Operation-Location header: guard it too.
        await self._guard_url(url)
        deadline = time.monotonic() + self.poll_timeout
        while True:
            try:
                response = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                if time.monotonic() >= deadline:
                    raise AzureOcrError(f"poll failed: {exc}") from exc
                await self._sleep(self.poll_interval)
                continue
            if response.status_code >= 500:
                if time.monotonic() >= deadline:
                    raise AzureOcrError(f"poll failed HTTP {response.status_code}")
                await self._sleep(self.poll_interval)
                continue
            if response.status_code != 200:
                raise AzureOcrError(f"poll failed HTTP {response.status_code}")
            payload = response.json()
            status = payload.get("status")
            if status == "succeeded":
                return payload.get("analyzeResult") or {}
            if status == "failed":
                raise AzureOcrError(f"analyze failed: {payload.get('error') or 'unknown'}")
            if time.monotonic() >= deadline:
                raise AzureOcrError("poll timeout")
            await self._sleep(self.poll_interval)

    def _to_document(self, result: dict) -> ParsedDocument:
        raw_pages = result.get("pages") or []
        page_count = len(raw_pages)
        buckets: list[list[OcrBlock]] = [[] for _ in range(page_count)]
        sizes: list[tuple[float, float]] = [
            (float(page.get("width") or 0), float(page.get("height") or 0))
            for page in raw_pages
        ]

        def page_of(regions: list[dict]) -> int | None:
            for region in regions or []:
                number = region.get("pageNumber")
                if number:
                    return int(number) - 1
            return None

        # Paragraphs carry the heading semantics we care about.
        for paragraph in result.get("paragraphs") or []:
            index = page_of(paragraph.get("boundingRegions") or [])
            if index is None or not (0 <= index < page_count):
                continue
            content = str(paragraph.get("content") or "").strip()
            if not content:
                continue
            region = (paragraph.get("boundingRegions") or [{}])[0]
            bbox = polygon_to_bbox(region.get("polygon"))
            role = str(paragraph.get("role") or "")
            label = _ROLE_LABELS.get(role, "text")
            bucket = buckets[index]
            bucket.append(OcrBlock(label, content, bbox, len(bucket) + 1))

        # Tables: rebuild HTML so _html_to_text still yields tab-separated text.
        for table in result.get("tables") or []:
            index = page_of(table.get("boundingRegions") or [])
            if index is None or not (0 <= index < page_count):
                continue
            rows: dict[int, dict[int, str]] = {}
            for cell in table.get("cells") or []:
                row = int(cell.get("rowIndex") or 0)
                column = int(cell.get("columnIndex") or 0)
                rows.setdefault(row, {})[column] = html.escape(
                    str(cell.get("content") or "")
                )
            html_rows = []
            for row_index in sorted(rows):
                cells = "".join(
                    f"<td>{rows[row_index][column]}</td>"
                    for column in sorted(rows[row_index])
                )
                html_rows.append(f"<tr>{cells}</tr>")
            if not html_rows:
                continue
            bbox = polygon_to_bbox(
                (table.get("boundingRegions") or [{}])[0].get("polygon")
            )
            bucket = buckets[index]
            bucket.append(
                OcrBlock("table", f"<table>{''.join(html_rows)}</table>", bbox, len(bucket) + 1)
            )

        # Figures: title + bbox only; the pixels need the figures endpoint.
        for figure in result.get("figures") or []:
            index = page_of(figure.get("boundingRegions") or [])
            if index is None or not (0 <= index < page_count):
                continue
            caption = str(figure.get("caption", {}).get("content") or "")
            bbox = polygon_to_bbox(
                (figure.get("boundingRegions") or [{}])[0].get("polygon")
            )
            bucket = buckets[index]
            bucket.append(OcrBlock("figure", caption, bbox, len(bucket) + 1))

        pages = [
            OcrPage(
                page_index=index,
                width=sizes[index][0],
                height=sizes[index][1],
                markdown="",
                blocks=sorted(
                    buckets[index], key=lambda block: (block.bbox[1], block.bbox[0])
                ),
                images={},
                raw={},
            )
            for index in range(page_count)
        ]
        if not pages:
            raise AzureOcrError("analyzeResult has no pages")
        for page in pages:
            page.blocks = [
                OcrBlock(block.label, block.content, block.bbox, position)
                for position, block in enumerate(page.blocks, 1)
            ]
        return ParsedDocument(
            pages=pages,
            bookmarks=[],
            metadata={
                "ocr_provider": self.name,
                "ocr_model": self.settings.azure_di_model,
            },
        )
