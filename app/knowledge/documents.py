from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class LocatorKind(StrEnum):
    LINE_RANGE = "line_range"
    PAGE = "page"
    ANCHOR = "anchor"


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """Provenance supplied by the importer for one stable document identity."""

    source_id: str
    source_uri: str
    source_type: str
    corpus_id: str
    pack_id: str | None = None
    pack_version: str | None = None
    game_version: str | None = None
    source_revision: str | None = None
    license_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("source_id", "source_uri", "source_type", "corpus_id"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    """Exact metadata constraints shared by FTS and vector retrieval."""

    document_id: str | None = None
    source_id: str | None = None
    source_type: str | None = None
    corpus_id: str | None = None
    pack_id: str | None = None
    pack_version: str | None = None
    game_version: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "document_id",
            "source_id",
            "source_type",
            "corpus_id",
            "pack_id",
            "pack_version",
            "game_version",
            "source_revision",
        ):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must be None or non-empty")

    @property
    def version_constrained(self) -> bool:
        return any((self.pack_version, self.game_version, self.source_revision))


@dataclass(frozen=True, slots=True)
class DocumentLocator:
    """A source-native location, independent of retrieval row numbers."""

    kind: LocatorKind
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    anchor: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", LocatorKind(self.kind))
        except ValueError as exc:
            raise ValueError("unsupported document locator kind") from exc
        if self.kind == LocatorKind.LINE_RANGE:
            if self.start_line is None or self.end_line is None:
                raise ValueError("line locator requires start_line and end_line")
            if self.start_line < 1 or self.end_line < self.start_line:
                raise ValueError("line locator range is invalid")
        elif self.kind == LocatorKind.PAGE:
            if self.page is None or self.page < 1:
                raise ValueError("page locator requires a positive page")
        elif self.kind == LocatorKind.ANCHOR:
            if self.anchor is None or not self.anchor.strip():
                raise ValueError("anchor locator requires a non-empty anchor")

    def display(self) -> str:
        if self.kind == LocatorKind.LINE_RANGE:
            assert self.start_line is not None and self.end_line is not None
            return f"L{self.start_line}" if self.start_line == self.end_line else f"L{self.start_line}-L{self.end_line}"
        if self.kind == LocatorKind.PAGE:
            return f"p.{self.page}"
        return f"#{self.anchor}"


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    title: str
    section_path: tuple[str, ...]
    content: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source: DocumentSource
    document_id: str
    title: str
    content: str
    content_hash: str
    parser_version: str
    sections: tuple[MarkdownSection, ...]


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    title: str
    content: str
    section_path: tuple[str, ...]
    locator: DocumentLocator
    generation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class IndexState(StrEnum):
    NOT_INDEXED = "not_indexed"
    EMPTY = "empty"
    INDEXED = "indexed"


@dataclass(frozen=True, slots=True)
class DocumentIndexStatus:
    document_id: str
    state: IndexState
    chunk_count: int = 0
    content_hash: str | None = None
    source: DocumentSource | None = None
    parser_version: str | None = None
    chunker_version: str | None = None
    generation: str | None = None
    vector_revision: str | None = None
