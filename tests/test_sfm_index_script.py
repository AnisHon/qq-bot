from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import index_sfm_pdf


class _Page:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class SfmIndexScriptTest(unittest.TestCase):
    def test_preview_parses_pdf_and_returns_chunk_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "fixture.pdf"
            pdf_path.write_bytes(b"synthetic PDF fixture bytes")
            fake_reader = SimpleNamespace(pages=[_Page("SFM fixture page content")])
            with patch.object(index_sfm_pdf, "PdfReader", return_value=fake_reader):
                self.assertEqual(index_sfm_pdf.preview_pdf(pdf_path), 1)

    def test_cli_index_dry_run_does_not_initialize_any_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "fixture.pdf"
            pdf_path.write_bytes(b"synthetic PDF fixture bytes")
            fake_reader = SimpleNamespace(pages=[_Page("SFM fixture page content")])
            stdout = io.StringIO()
            with (
                patch.object(index_sfm_pdf.Settings, "from_env", return_value=SimpleNamespace(sqlite_path=Path(temp_dir) / "db.sqlite")),
                patch.object(index_sfm_pdf, "RagStore", side_effect=AssertionError("dry run opened SQLite")),
                patch.object(index_sfm_pdf, "PdfReader", return_value=fake_reader),
                patch.object(sys, "argv", ["index_sfm_pdf.py", str(pdf_path), "--db", str(Path(temp_dir) / "db.sqlite"), "--dry-run"]),
                redirect_stdout(stdout),
            ):
                index_sfm_pdf.main()
            self.assertIn("would index 1 chunks", stdout.getvalue())
            self.assertFalse((Path(temp_dir) / "db.sqlite").exists())

    def test_cli_delete_dry_run_does_not_initialize_or_delete_stores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            with (
                patch.object(index_sfm_pdf.Settings, "from_env", return_value=SimpleNamespace(sqlite_path=Path(temp_dir) / "db.sqlite")),
                patch.object(index_sfm_pdf, "RagStore", side_effect=AssertionError("dry run opened SQLite")),
                patch.object(sys, "argv", ["index_sfm_pdf.py", "--name", "sfm-fixture", "--delete", "--dry-run"]),
                redirect_stdout(stdout),
            ):
                index_sfm_pdf.main()
            self.assertIn("would delete sfm-fixture", stdout.getvalue())
            self.assertFalse((Path(temp_dir) / "db.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
