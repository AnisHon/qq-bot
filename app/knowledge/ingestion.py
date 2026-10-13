from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING

from app.knowledge.documents import (
    DocumentChunk,
    DocumentLocator,
    DocumentSource,
    LocatorKind,
    MarkdownSection,
    ParsedDocument,
)

if TYPE_CHECKING:
    from app.rag import RagStore


MARKDOWN_PARSER_VERSION = "markdown-v1"
MARKDOWN_CHUNKER_VERSION = "markdown-lines-v1"
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")


class MarkdownParseError(ValueError):
    """The input cannot be safely decoded as UTF-8 Markdown."""


def parse_markdown_document(
    content: bytes | str,
    source: DocumentSource,
    *,
    document_id: str | None = None,
    title: str | None = None,
) -> ParsedDocument:
    """Parse Markdown headings while preserving the original line locations.

    Front matter is intentionally not interpreted as YAML. Pack and version
    metadata must be supplied explicitly in ``DocumentSource`` until a
    source-specific metadata contract is established.
    """
    if isinstance(content, bytes):
        raw = content
        try:
            decoded = content.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise MarkdownParseError("Markdown input is not valid UTF-8") from exc
    else:
        decoded = content
        try:
            raw = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise MarkdownParseError("Markdown input contains invalid Unicode") from exc

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(raw).hexdigest()
    stable_document_id = (document_id or source.source_id).strip()
    if not stable_document_id:
        raise ValueError("document_id must not be empty")
    display_title = title or PurePosixPath(source.source_uri.split("#", 1)[0]).name or source.source_id
    if display_title.lower().endswith(".md"):
        display_title = display_title[:-3]

    lines = normalized.split("\n") if normalized else []
    sections: list[MarkdownSection] = []
    heading_stack: list[str] = []
    current_title = display_title
    current_path: tuple[str, ...] = ()
    current_start = 1
    current_lines: list[str] = []
    in_fence: tuple[str, int] | None = None

    def flush(end_line: int) -> None:
        text = "\n".join(current_lines).strip("\n")
        if text.strip():
            sections.append(
                MarkdownSection(
                    title=current_title,
                    section_path=current_path,
                    content=text,
                    start_line=current_start,
                    end_line=max(current_start, end_line),
                )
            )

    for line_number, line in enumerate(lines, start=1):
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_match:
            marker_text = fence_match.group(1)
            marker = marker_text[0]
            if in_fence is None:
                in_fence = (marker, len(marker_text))
            elif marker == in_fence[0] and len(marker_text) >= in_fence[1] and not fence_match.group(2).strip():
                in_fence = None

        heading = None if in_fence is not None else _HEADING_RE.match(line)
        if heading is not None:
            flush(line_number - 1)
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(heading_text)
            current_path = tuple(heading_stack)
            current_title = heading_text
            current_start = line_number
            current_lines = [line]
        else:
            if not current_lines:
                current_start = line_number
            current_lines.append(line)
    if lines:
        flush(len(lines))

    return ParsedDocument(
        source=source,
        document_id=stable_document_id,
        title=display_title,
        content=normalized,
        content_hash=digest,
        parser_version=MARKDOWN_PARSER_VERSION,
        sections=tuple(sections),
    )


def chunk_markdown_document(
    document: ParsedDocument,
    *,
    max_chars: int = 900,
    overlap_chars: int = 80,
) -> list[DocumentChunk]:
    """Split sections into stable chunks, retaining heading and line evidence."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be between zero and max_chars")

    chunks: list[DocumentChunk] = []
    for section in document.sections:
        section_text = section.content
        start = 0
        while start < len(section_text):
            end = min(len(section_text), start + max_chars)
            if end < len(section_text):
                boundary = section_text.rfind("\n", start + max(1, max_chars // 2), end)
                if boundary >= 0:
                    end = boundary + 1
            raw_chunk = section_text[start:end]
            left_trim = len(raw_chunk) - len(raw_chunk.lstrip())
            right_trimmed = raw_chunk.rstrip()
            if right_trimmed:
                leading_text = section_text[: start + left_trim]
                last_content_index = start + len(right_trimmed) - 1
                start_line = section.start_line + leading_text.count("\n")
                end_line = section.start_line + section_text[: last_content_index + 1].count("\n")
                text = raw_chunk.strip()
                locator = DocumentLocator(
                    kind=LocatorKind.LINE_RANGE,
                    start_line=start_line,
                    end_line=max(start_line, end_line),
                )
                identity = "\0".join(
                    (
                        document.source.source_id,
                        document.document_id,
                        section.title,
                        "/".join(section.section_path),
                        str(start),
                        str(end),
                        str(locator.start_line),
                        str(locator.end_line),
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    )
                )
                chunk_id = "md-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
                chunks.append(
                    DocumentChunk(
                        chunk_id=chunk_id,
                        document_id=document.document_id,
                        title=section.title,
                        content=text,
                        section_path=section.section_path,
                        locator=locator,
                        metadata={"parser_version": document.parser_version},
                    )
                )
            if end >= len(section_text):
                break
            effective_overlap = min(overlap_chars, max(0, (end - start) // 2))
            next_start = end - effective_overlap if effective_overlap else end
            start = max(start + 1, next_start)
    return chunks


def ingest_markdown_file(
    path: Path,
    source: DocumentSource,
    store: RagStore,
    *,
    document_id: str | None = None,
    max_chars: int = 900,
    overlap_chars: int = 80,
) -> int:
    """Read and index one explicitly supplied Markdown file into SQLite FTS."""
    resolved = Path(path)
    raw = resolved.read_bytes()
    document = parse_markdown_document(raw, source, document_id=document_id)
    chunks = chunk_markdown_document(document, max_chars=max_chars, overlap_chars=overlap_chars)
    return store.replace_document(
        document.document_id,
        resolved,
        document.content_hash,
        chunks,
        document_source=source,
        parser_version=document.parser_version,
        chunker_version=MARKDOWN_CHUNKER_VERSION,
    )
