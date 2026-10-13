"""SFM-specific query constraints over the shared document knowledge layer.

The module defines source metadata and retrieval behavior only. It does not
include official SFM documentation, assert that fixture text is valid SFM, or
provide a parser/compiler.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.documents import DocumentFilter, DocumentSource
from app.knowledge.retrieval import DocumentKnowledgeService
from app.tools.contracts import ToolError, ToolResult, ToolStatus


@dataclass(frozen=True, slots=True)
class SFMSampleSourceManifest:
    """Provenance for one versioned SFM document source.

    The generic document schema has pack_version and game_version fields. SFM
    release is stored in pack_version and Minecraft version in game_version so
    existing FTS/Chroma filtering can enforce both versions without new columns.
    """

    source_id: str
    source_uri: str
    sfm_version: str
    minecraft_version: str
    source_revision: str
    license_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "source_uri",
            "sfm_version",
            "minecraft_version",
            "source_revision",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.license_id is not None and (
            not isinstance(self.license_id, str) or not self.license_id.strip()
        ):
            raise ValueError("license_id must be None or non-empty")

    def to_document_source(self) -> DocumentSource:
        return DocumentSource(
            source_id=self.source_id,
            source_uri=self.source_uri,
            source_type="sfm",
            corpus_id="sfm-examples",
            pack_version=self.sfm_version,
            game_version=self.minecraft_version,
            source_revision=self.source_revision,
            license_id=self.license_id,
        )


class SFMKnowledgeService:
    """Retrieve SFM examples using an exact SFM and Minecraft version pair."""

    def __init__(self, documents: DocumentKnowledgeService) -> None:
        self.documents = documents

    async def search_examples(
        self,
        query: str,
        *,
        sfm_version: str,
        minecraft_version: str,
        limit: int = 6,
    ) -> ToolResult:
        if not isinstance(query, str) or not query.strip():
            return _invalid("query")
        if not isinstance(sfm_version, str) or not sfm_version.strip():
            return _invalid("sfm_version")
        if not isinstance(minecraft_version, str) or not minecraft_version.strip():
            return _invalid("minecraft_version")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            return _invalid("limit")

        result = await self.documents.search(
            query,
            DocumentFilter(
                source_type="sfm",
                corpus_id="sfm-examples",
                pack_version=sfm_version.strip(),
                game_version=minecraft_version.strip(),
            ),
            limit=limit,
            evidence_limit=8,
        )
        # Before the first SFM source manifest is imported, the generic service
        # reports an absent corpus. At the SFM boundary this means the examples
        # have not been indexed yet. A disabled/unconfigured document store
        # retains DATA_UNAVAILABLE and backend errors retain their own status.
        if result.status == ToolStatus.DATA_UNAVAILABLE and self.documents.rag_store is not None:
            return ToolResult(
                status=ToolStatus.NOT_INDEXED,
                content={
                    "text": "当前没有已索引的 SFM 文档或示例。",
                    "results": [],
                    "ranking": "rrf",
                },
                error=ToolError("sfm_examples_not_indexed", "当前没有已索引的 SFM 文档或示例。"),
            )
        return result


def _invalid(field: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.INVALID_ARGUMENT,
        content={"text": "SFM 示例查询参数无效。", "results": []},
        error=ToolError("invalid_sfm_query", "SFM 示例查询参数无效。", field=field),
    )
