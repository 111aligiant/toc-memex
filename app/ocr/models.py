from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OcrBlock:
    label: str
    content: str
    bbox: tuple[float, float, float, float]
    order: int | None

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


@dataclass
class OcrPage:
    page_index: int
    width: float
    height: float
    markdown: str
    blocks: list[OcrBlock]
    images: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class Bookmark:
    level: int
    title: str
    page_index: int  # 0-based
    heading_num: str | None = None
    # True when this entry comes from the PDF's authored outline rather than
    # heading heuristics over the text layer. Outline titles are metadata the
    # authoring tool wrote, so unnumbered titles ("Introduction") are kept
    # with their authored level instead of being dropped as noise.
    from_outline: bool = False


@dataclass
class ParsedDocument:
    pages: list[OcrPage]
    bookmarks: list[Bookmark] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)