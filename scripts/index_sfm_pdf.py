#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.rag import RagStore


def _clean_pdf_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"([\u4e00-\u9fff])\1", r"\1", text)
    text = re.sub(r"\b([A-Za-z])(?:\s+([A-Za-z])){2,}\b", _join_spaced_letters, text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _join_spaced_letters(match: re.Match[str]) -> str:
    return re.sub(r"\s+", "", match.group(0))


def _split_page(text: str, max_chars: int = 900, overlap: int = 120) -> list[str]:
    text = _clean_pdf_text(text)
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(paragraph):
                part = paragraph[start : start + max_chars]
                chunks.append(part.strip())
                start += max_chars - overlap
            continue
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current.strip())
            tail = current[-overlap:].strip() if overlap > 0 else ""
            current = f"{tail}\n{paragraph}" if tail else paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current.strip())
    return chunks


def index_pdf(pdf_path: Path, sqlite_path: Path, document_name: str = "sfm") -> int:
    pdf_path = pdf_path.resolve()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    reader = PdfReader(str(pdf_path))
    chunks: list[tuple[str, int, str, str]] = []
    for page_index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for chunk_index, chunk in enumerate(_split_page(text), start=1):
            chunk_id = f"{document_name}:p{page_index}:c{chunk_index}"
            title = f"SFM 用法说明 第 {page_index} 页"
            chunks.append((chunk_id, page_index, title, chunk))
    store = RagStore(sqlite_path)
    chunks_for_vector = chunks
    n = store.replace_document(document_name, pdf_path, digest, chunks)
    print(f"FTS index: {n} chunks stored.")

    # Also build semantic vector index
    try:
        from app.vector_rag import VectorRagStore
        vector_store = VectorRagStore(sqlite_path.parent / "chromadb")
        chunk_list = [(cid, page, title, text) for cid, page, title, text in chunks_for_vector]
        n_vec = vector_store.upsert_chunks(document_name, chunk_list)
        print(f"Vector index: {n_vec} chunks embedded.")
    except Exception as exc:
        print(f"Warning: vector indexing failed: {exc}")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Index sfm.pdf into SQLite RAG tables")
    parser.add_argument("pdf", nargs="?", default="sfm.pdf")
    parser.add_argument("--db", default=None)
    parser.add_argument("--name", default="sfm")
    args = parser.parse_args()

    settings = Settings.from_env()
    sqlite_path = Path(args.db) if args.db else settings.sqlite_path
    count = index_pdf(Path(args.pdf), sqlite_path, args.name)
    print(f"indexed {count} chunks into {sqlite_path}")


if __name__ == "__main__":
    main()
