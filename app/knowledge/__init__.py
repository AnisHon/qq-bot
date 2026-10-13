"""Shared document ingestion and retrieval contracts.

This package contains format-neutral document types and local parsers. It does
not imply that any GTNH corpus has been acquired or indexed.
"""

from app.knowledge.documents import (
    DocumentChunk,
    DocumentFilter,
    DocumentIndexStatus,
    DocumentLocator,
    DocumentSource,
    IndexState,
    ParsedDocument,
)

__all__ = [
    "DocumentChunk",
    "DocumentFilter",
    "DocumentIndexStatus",
    "DocumentLocator",
    "DocumentSource",
    "IndexState",
    "ParsedDocument",
]
