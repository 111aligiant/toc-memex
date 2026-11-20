"""MinerU (OpenDataLab) batch API.

Flow: request pre-signed upload URLs → PUT the PDF → poll the batch →
download the result zip → read ``content_list.json`` (plus ``middle.json``
for page sizes and the image files inside the zip).

``content_list.json`` items:

    {"type": "text", "text": "1 范围", "text_level": 1,
     "page_idx": 0, "bbox": [x0, y0, x1, y1]}      # bbox is 0-1000 normalized
    {"type": "table", "table_body": "<html>…", "page_idx": 0, "bbox": [...]}
    {"type": "image", "img_path": "images/a.jpg", "image_caption": [...],
     "page_idx": 0, "bbox": [...]}
    {"type": "equation", "text": "$$…$$", "page_idx": 0, "bbox": [...]}

Docs: https://mineru.net/apiManage/docs
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import httpx

from ..models import OcrBlock, OcrPage, ParsedDocument
from ..normalize import polygon_to_bbox
from .base import BaseOcrProvider, OcrProviderError

_IMAGE_TYPES = {"image", "figure"}
_TABLE_TYPES = {"table"}
_TEXT_TYPES = {"text", "list", "title", "paragraph"}


class MineruOcrError(OcrProviderError):
    pass


class MineruOcrProvider(BaseOcrProvider):
    name = "mineru"

    def check_config(self) -> None:
        if not self.settings.mineru_api_token:
            raise MineruOcrError("MINERU_API_TOKEN is not configured")

    def _headers(self) -> dict[str, str]:
        self.check_config()
        return {
            "Authorization": f"Bearer {self.settings.mineru_api_token}",
            "Content-Type": "application/json",
        }

    def _probe_target(self) -> tuple[str, dict[str, str]]:
        # Batch listing proves reachability; an empty list is a valid answer.
        base = self.settings.mineru_api_url.rstrip("/")
        return f"{base}/extract-results/batch/probe", self._headers()

    async def _run(
        self, client: httpx.AsyncClient, pdf_path: Path, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        base = self.settings.mineru_api_url.rstrip("/")
        # 1. Request an upload URL for this file.
        await self._guard_url(f"{base}/file-urls/batch")
        body = {
            "enable_formula": True,
            "enable_table": True,
            "language": "ch",
            "files": [{"name": pdf_path.name, "is_ocr": True}],
        }
        try:
            response = await client.post(
                f"{base}/file-urls/batch", headers=self._headers(), json=body
            )
        except httpx.HTTPError as exc:
            raise MineruOcrError(f"upload-url request failed: {exc}") from exc
        if response.status_code != 200:
            raise MineruOcrError(
                f"upload-url failed HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        data = response.json().get("data") or {}
        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not batch_id or not urls:
            raise MineruOcrError("upload-url response missing batch_id/file_urls")

        # 2. PUT the raw bytes (pre-signed URL: no Authorization header).
        # The URL comes from the vendor response, so validate before use.
        await self._guard_url(urls[0])
        try:
            upload = await client.put(urls[0], content=pdf_path.read_bytes())
        except httpx.HTTPError as exc:
            raise MineruOcrError(f"file upload failed: {exc}") from exc
        if upload.status_code not in (200, 201, 204):
            raise MineruOcrError(f"file upload failed HTTP {upload.status_code}")

        # 3. Poll the batch until this file is done.
        zip_url = await self._poll(client, f"{base}/extract-results/batch/{batch_id}")

        # 4. Download and unpack the result zip.
        await self._guard_url(zip_url)
        try:
            archive = await client.get(zip_url)
        except httpx.HTTPError as exc:
            raise MineruOcrError(f"result download failed: {exc}") from exc
        if archive.status_code != 200:
            raise MineruOcrError(
                f"result download failed HTTP {archive.status_code}"
            )
        raw_zip = archive.content
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_zip))
        except zipfile.BadZipFile as exc:
            raise MineruOcrError(f"result is not a zip: {exc}") from exc
        return self._parse_zip(zf, out_dir)

    async def _poll(self, client: httpx.AsyncClient, url: str) -> str:
        await self._guard_url(url)
        deadline = time.monotonic() + self.poll_timeout
        while True:
            try:
                response = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                if time.monotonic() >= deadline:
                    raise MineruOcrError(f"poll failed: {exc}") from exc
                await self._sleep(self.poll_interval)
                continue
            if response.status_code >= 500:
                if time.monotonic() >= deadline:
                    raise MineruOcrError(f"poll failed HTTP {response.status_code}")
                await self._sleep(self.poll_interval)
                continue
            if response.status_code != 200:
                raise MineruOcrError(f"poll failed HTTP {response.status_code}")
            results = (response.json().get("data") or {}).get("extract_result") or []
            item = results[0] if results else {}
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise MineruOcrError("job done but missing full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise MineruOcrError(f"job failed: {item.get('err_msg') or 'unknown'}")
            if time.monotonic() >= deadline:
                raise MineruOcrError("poll timeout")
            await self._sleep(self.poll_interval)

    def _parse_zip(
        self, zf: zipfile.ZipFile, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        names = set(zf.namelist())

        def read(name: str) -> dict | list | None:
            if name not in names:
                return None
            try:
                return json.loads(zf.read(name).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None

        content = read("content_list.json") or []
        middle = read("middle.json") or {}

        # Page sizes from middle.json (points); content_list bboxes are 0-1000.
        sizes: dict[int, tuple[float, float]] = {}
        for page in (middle.get("pdf_info") or []) if isinstance(middle, dict) else []:
            index = page.get("page_idx")
            size = page.get("page_size") or []
            if index is not None and len(size) >= 2:
                sizes[int(index)] = (float(size[0]), float(size[1]))
        page_count = max(sizes) + 1 if sizes else 0
        if not page_count:
            page_count = max(
                (int(item.get("page_idx") or 0) for item in content), default=0
            ) + 1

        buckets: dict[int, list[OcrBlock]] = {index: [] for index in range(page_count)}
        images: dict[int, dict[str, str]] = {index: {} for index in range(page_count)}
        image_dir = Path(out_dir) / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        for item in content:
            if not isinstance(item, dict):
                continue
            page_index = int(item.get("page_idx") or 0)
            if page_index < 0:
                continue
            while page_index >= len(buckets):
                buckets[len(buckets)] = []
                images[len(images)] = {}
            width, height = sizes.get(page_index, (0.0, 0.0))
            bbox = polygon_to_bbox(
                item.get("bbox"),
                scale_x=width / 1000 if width else 1.0,
                scale_y=height / 1000 if height else 1.0,
            )
            kind = str(item.get("type") or "")
            blocks = buckets[page_index]

            if kind in _TABLE_TYPES:
                html = item.get("table_body") or item.get("text") or ""
                if html:
                    blocks.append(OcrBlock("table", str(html), bbox, len(blocks) + 1))
            elif kind in _IMAGE_TYPES:
                caption = item.get("image_caption") or []
                title = str(caption[0]) if caption else ""
                path = str(item.get("img_path") or "")
                url = ""
                if path and path in names:
                    target = image_dir / Path(path).name
                    try:
                        target.write_bytes(zf.read(path))
                        url = target.as_uri()
                    except OSError:
                        url = ""
                if url:
                    images[page_index][f"{Path(path).name}"] = url
                blocks.append(
                    OcrBlock("figure", title or path, bbox, len(blocks) + 1)
                )
            elif kind in {"equation", "interline_equation"}:
                text = str(item.get("text") or "")
                if text:
                    blocks.append(
                        OcrBlock("display_formula", text, bbox, len(blocks) + 1)
                    )
            elif kind in _TEXT_TYPES or kind == "":
                text = str(item.get("text") or "")
                if not text.strip():
                    continue
                level = item.get("text_level")
                label = "paragraph_title" if level not in (None, 0) else "text"
                blocks.append(OcrBlock(label, text, bbox, len(blocks) + 1))

        pages = [
            OcrPage(
                page_index=index,
                width=sizes.get(index, (0.0, 0.0))[0],
                height=sizes.get(index, (0.0, 0.0))[1],
                markdown="",
                blocks=sorted(buckets[index], key=lambda b: (b.order or 0)),
                images=images[index],
                raw={},
            )
            for index in range(len(buckets))
        ]
        if not pages:
            raise MineruOcrError("result zip has no pages")
        return (
            ParsedDocument(
                pages=pages,
                bookmarks=[],
                metadata={
                    "ocr_provider": self.name,
                    "ocr_model": self.settings.mineru_model_version,
                },
            ),
            json.dumps(content, ensure_ascii=False),
        )
