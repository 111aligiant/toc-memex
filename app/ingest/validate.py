"""File validation and error translation for ingest, shared by every entry point.

Web upload, the CLI and the MCP ``ingest_path`` tool all accept a file the
user pointed at, so they must agree on what a valid ingest file is and on
how a failed parse is described. This module holds those two pieces without
importing FastAPI: callers wrap ``ValueError`` into whatever their surface
needs (an ``HTTPException`` for the web layer, a ``ToolError`` for MCP).

Keeping the checks here (rather than in ``web_api``) also avoids
``mcp_server`` importing the web layer, which would invert the dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

from .pipeline import infer_doc_type

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Extensions we refuse even though they are otherwise readable: the ingest
# pipeline has no spreadsheet renderer, so accepting them would only produce
# a confusing late failure.
_REJECTED_SUFFIXES = {".xls", ".xlsx", ".csv", ".tsv"}

_IMAGE_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)


def validate_ingest_file(
    path: Path,
    name: str | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> None:
    """Raise ``ValueError`` when *path* is not something ingest should accept.

    Catches the common "renamed / truncated / empty file" case before a
    background ingest spends minutes on it. Deeper corruption that still has
    a valid header surfaces later as a friendly parse error, not here.
    """
    path = Path(path)
    label = name or path.name
    suffix = Path(label).suffix.lower()

    if not path.is_file():
        raise ValueError(f"文件不存在：{label}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"文件为空：{label}")
    if size > max_bytes:
        raise ValueError(
            f"文件超过 {max_bytes // (1024 * 1024)} MB 上限：{label}"
        )
    if suffix in _REJECTED_SUFFIXES:
        raise ValueError(
            "Excel / CSV 上传暂未开放，请先导出为 PDF 或文本后再上传"
        )
    if infer_doc_type(path) is None:
        raise ValueError(f"不支持的文件类型：{suffix or '（无扩展名）'}")

    with path.open("rb") as handle:
        head = handle.read(1024)
    if suffix == ".pdf":
        if b"%PDF-" not in head:
            raise ValueError("文件不是有效的 PDF：缺少 %PDF 文件头")
    elif suffix == ".docx":
        if not head.startswith(b"PK\x03\x04"):
            raise ValueError("文件不是有效的 DOCX：不是 ZIP 容器")
    elif suffix == ".doc":
        if not head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ValueError("文件不是有效的 DOC：不是 Word 复合文档")
    elif suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}:
        if not any(head.startswith(prefix) for prefix in _IMAGE_PREFIXES):
            raise ValueError("文件不是有效的图片：文件头无法识别")
    elif suffix == ".webp":
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise ValueError("文件不是有效的 WEBP：文件头无法识别")


_INGEST_ERROR_HINTS = (
    ("PdfReadError", "PDF 文件损坏或无法读取"),
    ("PdfStreamError", "PDF 文件损坏或无法读取"),
    ("EOFError", "文件内容不完整或已损坏"),
    ("BadZipFile", "DOCX 文件损坏（ZIP 容器不完整）"),
    ("UnidentifiedImageError", "图片文件损坏或格式不受支持"),
    ("ModuleNotFoundError", "解析依赖缺失，请检查安装"),
    ("OCR", "OCR 解析失败；到设置页点「测试」检查端点，或点「重新上传」重试"),
)


def friendly_ingest_error(detail: str) -> str:
    """Turn a child-process traceback into something a user can act on.

    The text must answer two questions the UI cannot: what actually failed
    (local file, credentials, rate limit, or the vendor's own 5xx), and what
    to do next. The retry path is the document row's 「重新上传」 button.
    """
    text = detail or ""

    # A vendor async job that the OCR service itself failed (state=failed).
    # The *last* occurrence is the raised exception; earlier ones are
    # traceback source lines that contain the literal "job failed:".
    if "job failed" in text:
        tail = text.rsplit("job failed", 1)[1]
        reason = tail.splitlines()[0].strip() if tail else ""
        reason = re.sub(r"^\s*\(job [^)]+\):\s*", "", reason).strip(" :\"'")
        reason = reason[:200] or "未知原因"
        lowered = reason.lower()
        if "500" in reason:
            return (
                f"解析失败：OCR 服务端执行任务时返回 500（{reason}）。"
                "文件本身没问题，这是 OCR 服务的临时故障。"
                "点这条任务的「重新上传」重试，通常第二次就能通过；"
                "若连续失败，到「设置 → OCR 端点」点「测试」确认服务可用，"
                "或换一张图再试。"
            )
        if "401" in reason or "403" in reason or "认证" in reason or "auth" in lowered:
            return (
                f"解析失败：OCR 服务认证失败（{reason}）。"
                "到「设置 → OCR 端点」检查 Token 后点「测试」。"
            )
        if "429" in reason or "限流" in reason or "频繁" in reason or "limit" in lowered:
            return (
                f"解析失败：OCR 服务限流（{reason}）。"
                "等 1 分钟后点「重新上传」再试。"
            )
        return (
            f"解析失败：OCR 服务端任务执行失败（{reason}）。"
            "点「重新上传」重试；若反复失败，到设置页测试 OCR 端点，"
            "或换一张图/把图片缩小后再试。"
        )

    lowered = text.lower()
    if "submit failed http 401" in lowered or "submit failed http 403" in lowered:
        return (
            "解析失败：OCR 服务拒绝认证（HTTP 401/403）。"
            "到「设置 → OCR 端点」重新填写 Token 并点「测试」。"
        )
    if "submit failed http 429" in lowered:
        return "解析失败：OCR 服务限流（HTTP 429）。等 1 分钟后点「重新上传」再试。"
    if "poll timeout" in lowered:
        return (
            "解析失败：OCR 服务超过等待时限仍未完成。"
            "稍后在解析队列点「重新上传」；大文件可先拆分或压缩。"
        )
    if "submit failed after retries" in lowered:
        return (
            "解析失败：连接不上 OCR 服务（已自动重试）。"
            "检查网络和「设置 → OCR 端点」的地址/Token 后点「测试」。"
        )
    for needle, message in _INGEST_ERROR_HINTS:
        if needle in text:
            return f"解析失败：{message}"
    return "解析失败：文件无法解析，请确认格式与完整性"


def display_ingest_error(error: str | None) -> str | None:
    """Hide unreadable errors left by older versions in the queue.

    Before the child ran with PYTHONIOENCODING=utf-8, a GBK traceback was
    decoded as UTF-8 and the vendor's Chinese message became replacement
    characters. The raw text is gone, so say what happened and how to
    recover instead of showing mojibake forever.
    """
    if not error:
        return None
    if "�" in error or "data.get('errorMsg')" in error:
        return (
            "解析失败：OCR 服务端临时故障（旧版本记录，服务端错误详情已丢失）。"
            "点「重新上传」用原文件重试即可。"
        )
    return error
