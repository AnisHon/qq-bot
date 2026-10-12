from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.rag import RagStore
from app.bot_service import _looks_like_sfm_query, _format_rag_chunks


class RagStoreTest(unittest.TestCase):
    def test_replace_and_search_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = RagStore(db_path)
            count = store.replace_document(
                "sfm",
                Path("sfm.pdf"),
                "hash",
                [
                    ("sfm:p1:c1", 1, "SFM", "every 20 ticks input from chest output to furnace"),
                    ("sfm:p2:c1", 2, "SFM", "label gun 可以给方块添加标签"),
                ],
            )
            self.assertEqual(count, 2)
            results = store.search("label gun 标签枪", document_name="sfm", limit=1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].page, 2)

    def test_sfm_trigger_and_format(self) -> None:
        self.assertTrue(_looks_like_sfm_query("SFM 的 input output 怎么写"))
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bot.db"
            store = RagStore(db_path)
            store.replace_document(
                "sfm",
                Path("sfm.pdf"),
                "hash",
                [("sfm:p1:c1", 1, "SFM", "input 从标签中输入物品，output 输出到目标。")],
            )
            text = _format_rag_chunks(store.search("input output", document_name="sfm", limit=1))
            self.assertIn("第 1 页", text)
            self.assertIn("input", text)


if __name__ == "__main__":
    unittest.main()
