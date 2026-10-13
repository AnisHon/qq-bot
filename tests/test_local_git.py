from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from app.config import Settings, _parse_local_git_repositories
from app.repositories.local_git import (
    LocalGitRepositoryService,
    RepositoryConfigurationError,
    RepositoryDataUnavailable,
    RepositoryExecutionError,
    RepositoryFileUnavailable,
    RepositoryInvalidRange,
    RepositoryPathDenied,
    RepositoryTimedOut,
)
from app.tools.agent import TOOL_REGISTRY
from app.tools.contracts import PolicySnapshot, ToolContext, ToolStatus
from app.storage_executor import StorageExecutor


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Synthetic Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=5,
        text=True,
    ).stdout.strip()


def _make_repo(root: Path, *, max_file_bytes: int = 128) -> tuple[Path, LocalGitRepositoryService]:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text(
        "# Synthetic local repository\n\nFirst version line.\nSecond line.\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("fixture-secret-do-not-read\n", encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "credentials.json").write_text("fixture only\n", encoding="utf-8")
    (root / "keys").mkdir()
    (root / "keys" / "server.pem").write_text("synthetic key fixture\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "client_secret.yaml").write_text("fixture only\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"fixture\x00binary")
    (root / "invalid-utf8.bin").write_bytes(b"\xffbinary without nul")
    (root / "oversized.txt").write_text("x" * (max_file_bytes + 1), encoding="utf-8")
    outside = root.parent / "outside-fixture.txt"
    outside.write_text("outside target is synthetic", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)
    _git(root, "add", "--all")
    _git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture one")
    return root, LocalGitRepositoryService({"fixture-repo": root}, max_file_bytes=max_file_bytes)


async def _wait_for_event(event: threading.Event, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    return event.is_set()


class LocalGitRepositoryConfigTest(unittest.TestCase):
    def test_empty_allowlist_is_supported_and_unconfigured_repo_is_unavailable(self):
        settings = Settings(local_git_repositories=())
        service = LocalGitRepositoryService.from_settings(settings)
        self.assertEqual(service.configured_repo_ids, ())
        with self.assertRaises(RepositoryDataUnavailable):
            service.snapshot("model-supplied-path")

    def test_settings_parse_static_absolute_repo_roots(self):
        parsed = _parse_local_git_repositories("source=/srv/repos/source;wiki=/srv/repos/wiki")
        self.assertEqual(parsed, (("source", Path("/srv/repos/source")), ("wiki", Path("/srv/repos/wiki"))))

    def test_settings_load_repository_allowlist_without_changing_defaults(self):
        with patch.dict(os.environ, {"LOCAL_GIT_REPOSITORIES": "fixture=/tmp/synthetic-repo"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.local_git_repositories, (("fixture", Path("/tmp/synthetic-repo")),))
        self.assertEqual(settings.sqlite_path, Path("data/bot.db"))

        with patch.dict(os.environ, {"LOCAL_GIT_REPOSITORIES": "fixture=relative"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_settings_reject_relative_duplicate_and_malformed_entries(self):
        for raw in ("repo=relative/path", "repo=/a;repo=/b", "missing-equals", "bad id=/tmp/repo"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _parse_local_git_repositories(raw)

    def test_non_git_root_subdirectory_and_symlink_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            ordinary = base / "ordinary"
            ordinary.mkdir()
            with self.assertRaises(RepositoryConfigurationError):
                LocalGitRepositoryService({"ordinary": ordinary})

            repo = base / "repo"
            _git(repo.parent, "init", "-q", str(repo))
            (repo / "nested").mkdir()
            with self.assertRaises(RepositoryConfigurationError):
                LocalGitRepositoryService({"nested": repo / "nested"})
            alias = base / "repo-alias"
            alias.symlink_to(repo, target_is_directory=True)
            with self.assertRaises(RepositoryConfigurationError):
                LocalGitRepositoryService({"alias": alias})

    def test_git_root_without_a_commit_is_configured_but_snapshot_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "empty-repo"
            root.mkdir()
            _git(root, "init", "-q")
            service = LocalGitRepositoryService({"empty": root})
            self.assertEqual(service.configured_repo_ids, ("empty",))
            with self.assertRaises(RepositoryDataUnavailable):
                service.snapshot("empty")

class LocalGitRepositoryBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.repo, self.service = _make_repo(self.base / "repo")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_git_timeout_and_execution_failure_are_not_reported_as_missing_data(self):
        with patch(
            "app.repositories.local_git.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=0.1),
        ):
            with self.assertRaises(RepositoryTimedOut):
                self.service.snapshot("fixture-repo")

        failed_process = subprocess.CompletedProcess(["git"], 128, stdout=b"", stderr=b"")
        with patch("app.repositories.local_git.subprocess.run", return_value=failed_process):
            with self.assertRaises(RepositoryExecutionError):
                self.service.snapshot("fixture-repo")

    def test_snapshot_is_full_commit_and_pinned_to_git_objects(self):
        first = self.service.snapshot("fixture-repo")
        self.assertRegex(first.commit_sha, r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        original = self.service.inspect_file("fixture-repo", "docs/guide.md", first)

        (self.repo / "docs" / "guide.md").write_text("uncommitted worktree change\n", encoding="utf-8")
        pinned_again = self.service.inspect_file("fixture-repo", "docs/guide.md", first)
        self.assertEqual(pinned_again.object_id, original.object_id)
        self.assertEqual(pinned_again.byte_size, original.byte_size)

        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture two")
        second = self.service.snapshot("fixture-repo")
        updated = self.service.inspect_file("fixture-repo", "docs/guide.md", second)
        self.assertNotEqual(first.commit_sha, second.commit_sha)
        self.assertNotEqual(original.object_id, updated.object_id)
        self.assertEqual(self.service.snapshot("fixture-repo", first.commit_sha), first)

    def test_path_traversal_absolute_encoded_and_invalid_snapshots_fail_before_lookup(self):
        snapshot = self.service.snapshot("fixture-repo")
        for path in (
            "../outside-fixture.txt",
            "docs/../../etc/passwd",
            "/etc/passwd",
            "C:/Windows/win.ini",
            "docs\\guide.md",
            "docs/%2fsecret",
            "docs//guide.md",
            "docs/./guide.md",
            "docs/\x00guide.md",
        ):
            with self.subTest(path=path), self.assertRaises(RepositoryPathDenied):
                self.service.inspect_file("fixture-repo", path, snapshot)
        with self.assertRaises(RepositoryPathDenied):
            self.service.snapshot("fixture-repo", "HEAD~1")
        with self.assertRaises(RepositoryPathDenied):
            self.service.snapshot("fixture-repo", "abc123")

    def test_sensitive_paths_symlinks_binary_and_large_files_are_denied(self):
        snapshot = self.service.snapshot("fixture-repo")
        denied = (
            ".env",
            "secrets/credentials.json",
            "keys/server.pem",
            ".git/config",
            "config/client_secret.yaml",
            "linked.txt",
            "binary.bin",
            "invalid-utf8.bin",
            "oversized.txt",
        )
        for path in denied:
            with self.subTest(path=path), self.assertRaises(RepositoryPathDenied):
                self.service.inspect_file("fixture-repo", path, snapshot)

    def test_missing_or_unconfigured_repository_file_has_a_distinct_safe_error(self):
        snapshot = self.service.snapshot("fixture-repo")
        with self.assertRaises(RepositoryFileUnavailable):
            self.service.inspect_file("fixture-repo", "does-not-exist.md", snapshot)
        with self.assertRaises(RepositoryDataUnavailable):
            self.service.inspect_file("another-repo", "docs/guide.md", snapshot)

    def test_read_operations_do_not_mutate_git_index_or_worktree(self):
        snapshot = self.service.snapshot("fixture-repo")
        index_path = self.repo / ".git" / "index"
        before = index_path.read_bytes()
        guide_path = self.repo / "docs" / "guide.md"
        guide_stat = guide_path.stat()

        self.service.snapshot("fixture-repo")
        self.service.inspect_file("fixture-repo", "docs/guide.md", snapshot)

        self.assertEqual(index_path.read_bytes(), before)
        self.assertEqual(guide_path.stat().st_mtime_ns, guide_stat.st_mtime_ns)

    def test_inherited_git_environment_cannot_redirect_repository_or_config(self):
        unrelated = self.base / "unrelated"
        _make_repo(unrelated)
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(unrelated / ".git"),
                "GIT_WORK_TREE": str(unrelated),
                "GIT_OBJECT_DIRECTORY": str(unrelated / ".git" / "objects"),
            },
        ):
            isolated = LocalGitRepositoryService({"fixture-repo": self.repo})
            pinned = isolated.snapshot("fixture-repo")
            file_ref = isolated.inspect_file("fixture-repo", "docs/guide.md", pinned)
        self.assertEqual(pinned.repo_id, "fixture-repo")
        self.assertIn("docs/guide.md", file_ref.path)

    def test_path_search_is_paginated_and_never_returns_sensitive_entries(self):
        for name in ("guide-a.md", "guide-b.md"):
            (self.repo / "docs" / name).write_text(f"{name}\n", encoding="utf-8")
        _git(self.repo, "add", "docs/guide-a.md", "docs/guide-b.md")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture two")
        snapshot = self.service.snapshot("fixture-repo")

        first = self.service.search("fixture-repo", "guide", mode="path", limit=2)
        second = self.service.search(
            "fixture-repo", "guide", mode="path", commit_sha=first.snapshot.commit_sha,
            offset=first.next_offset or 0, limit=2,
        )
        paths = {match.path for match in first.matches + second.matches}
        self.assertIn("docs/guide.md", paths)
        self.assertIn("docs/guide-a.md", paths)
        self.assertIn("docs/guide-b.md", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(first.snapshot, snapshot)
        self.assertIsNotNone(first.next_offset)

    def test_text_search_reports_line_and_reads_the_same_commit_after_worktree_changes(self):
        first = self.service.snapshot("fixture-repo")
        page = self.service.search(
            "fixture-repo", "Second line", mode="text", commit_sha=first.commit_sha, limit=5
        )
        match = next(item for item in page.matches if item.path == "docs/guide.md")
        self.assertEqual(match.line, 4)
        self.assertIn("Second line.", match.snippet)

        result = self.service.read_lines(
            "fixture-repo", "docs/guide.md", first, start_line=3, end_line=4
        )
        self.assertIn("3: First version line.", result.text)
        self.assertIn("4: Second line.", result.text)

        (self.repo / "docs" / "guide.md").write_text("new committed version\n", encoding="utf-8")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture two")
        pinned = self.service.read_lines(
            "fixture-repo", "docs/guide.md", first, start_line=4, end_line=4
        )
        self.assertIn("Second line.", pinned.text)
        current = self.service.snapshot("fixture-repo")
        self.assertNotEqual(current.commit_sha, first.commit_sha)
        with self.assertRaises(RepositoryInvalidRange):
            self.service.read_lines("fixture-repo", "docs/guide.md", current, start_line=5, end_line=6)

    def test_search_scan_cap_is_reported_as_partial(self):
        with patch("app.repositories.local_git._MAX_SEARCH_FILES", 1):
            page = self.service.search("fixture-repo", "unlikely", mode="text")
        self.assertTrue(page.truncated)
        self.assertLessEqual(page.files_scanned, 1)

    def test_read_line_and_byte_limits_are_enforced(self):
        with self.assertRaises(RepositoryInvalidRange):
            self.service.read_lines(
                "fixture-repo", "docs/guide.md", self.service.snapshot("fixture-repo"),
                start_line=1, end_line=201,
            )

        long_file = self.repo / "long.txt"
        long_file.write_text("x" * 20_000 + "\n", encoding="utf-8")
        _git(self.repo, "add", "long.txt")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "long line fixture")
        service = LocalGitRepositoryService({"fixture-repo": self.repo}, max_file_bytes=100_000)
        result = service.read_lines(
            "fixture-repo", "long.txt", service.snapshot("fixture-repo"), start_line=1, end_line=1
        )
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text.encode("utf-8")), 16 * 1024)

    def test_special_filename_bytes_are_searched_and_read_literally(self):
        fixtures = {
            "docs/space name [v1].md": "space fixture\n",
            "docs/中文说明.md": "unicode fixture\n",
            "docs/-leading-dash.md": "dash fixture\n",
        }
        for relative, content in fixtures.items():
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        _git(self.repo, "add", "docs")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "special paths")
        snapshot = self.service.snapshot("fixture-repo")

        page = self.service.search("fixture-repo", "space name", mode="path", commit_sha=snapshot.commit_sha)
        self.assertIn("docs/space name [v1].md", {item.path for item in page.matches})
        unicode_file = self.service.inspect_file("fixture-repo", "docs/中文说明.md", snapshot)
        self.assertEqual(unicode_file.byte_size, len("unicode fixture\n".encode("utf-8")))
        dash_file = self.service.read_lines(
            "fixture-repo", "docs/-leading-dash.md", snapshot, start_line=1, end_line=1
        )
        self.assertIn("1: dash fixture", dash_file.text)

    def test_submodule_gitlink_is_not_a_readable_directory_or_file(self):
        module = self.base / "submodule-fixture"
        module.mkdir()
        _git(module, "init", "-q")
        (module / "payload.txt").write_text("synthetic submodule data\n", encoding="utf-8")
        _git(module, "add", "payload.txt")
        _git(module, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "module content")
        module_sha = _git(module, "rev-parse", "HEAD")

        _git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{module_sha},vendor/submodule")
        _git(self.repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "add gitlink")
        snapshot = self.service.snapshot("fixture-repo")
        with self.assertRaises(RepositoryPathDenied):
            self.service.inspect_file("fixture-repo", "vendor/submodule/payload.txt", snapshot)
        page = self.service.search("fixture-repo", "submodule", mode="path", commit_sha=snapshot.commit_sha)
        self.assertNotIn("vendor/submodule", {item.path for item in page.matches})

    def test_tree_listing_overflow_is_reported_as_partial(self):
        with patch("app.repositories.local_git._MAX_TREE_OUTPUT_BYTES", 1):
            page = self.service.search("fixture-repo", "not-present", mode="path")
        self.assertTrue(page.truncated)

    def test_tree_listing_timeout_kills_and_reaps_child_process(self):
        service = LocalGitRepositoryService({"fixture-repo": self.repo})
        snapshot = service.snapshot("fixture-repo")
        real_popen = subprocess.Popen
        children: list[subprocess.Popen] = []

        def blocking_child(*_args, **kwargs):
            child = real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=kwargs["stdin"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
                env=kwargs["env"],
            )
            children.append(child)
            return child

        with patch("app.repositories.local_git.subprocess.Popen", side_effect=blocking_child):
            with self.assertRaises(RepositoryTimedOut):
                service._list_tree(self.repo, snapshot.commit_sha, timeout_seconds=0.1)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


class LocalGitRegistryToolTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo, _ = _make_repo(Path(self.tmpdir.name) / "repo")
        self.service = LocalGitRepositoryService({"fixture-repo": self.repo}, max_file_bytes=1024)
        self.snapshot = self.service.snapshot("fixture-repo")
        self.storage_executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=4)
        self.context = ToolContext(
            request_id="local-git-fixture",
            group_id="fixture-group",
            user_id="fixture-user",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            extensions={
                "local_repository_service": self.service,
                "local_repository_runner": self.storage_executor.run_vector,
            },
        )

    async def asyncTearDown(self):
        await self.storage_executor.aclose()
        self.tmpdir.cleanup()

    async def test_registry_search_and_read_return_pinned_git_evidence(self):
        search = await TOOL_REGISTRY.dispatch(
            "repository_search",
            {"repo_id": "fixture-repo", "query": "Second line", "mode": "text"},
            self.context,
        )
        self.assertEqual(search.status, ToolStatus.OK)
        self.assertIn(self.snapshot.commit_sha, search.content["text"])
        self.assertEqual(search.content["snapshot"], self.snapshot.commit_sha)
        evidence = search.evidence[0]
        self.assertEqual(evidence.kind, "repository_file")
        self.assertIn(self.snapshot.commit_sha, evidence.locator)
        self.assertTrue(evidence.locator.endswith("#L4"))

        read = await TOOL_REGISTRY.dispatch(
            "repository_read",
            {
                "repo_id": "fixture-repo",
                "commit_sha": self.snapshot.commit_sha,
                "path": "docs/guide.md",
                "start_line": 3,
                "end_line": 4,
            },
            self.context,
        )
        self.assertEqual(read.status, ToolStatus.OK)
        self.assertIn("3: First version line.", read.content["text"])
        self.assertEqual(read.evidence[0].revision, self.snapshot.commit_sha)
        self.assertTrue(read.evidence[0].locator.endswith("#L3-L4"))

    async def test_registry_does_not_read_unconfigured_or_sensitive_paths(self):
        empty_context = ToolContext(
            request_id="empty", policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"}))
        )
        unavailable = await TOOL_REGISTRY.dispatch(
            "repository_search",
            {"repo_id": "fixture-repo", "query": "guide", "mode": "path"},
            empty_context,
        )
        self.assertEqual(unavailable.status, ToolStatus.DATA_UNAVAILABLE)

        denied = await TOOL_REGISTRY.dispatch(
            "repository_read",
            {
                "repo_id": "fixture-repo",
                "commit_sha": self.snapshot.commit_sha,
                "path": ".env",
                "start_line": 1,
                "end_line": 2,
            },
            self.context,
        )
        self.assertEqual(denied.status, ToolStatus.DENIED)

    async def test_search_schema_rejects_unbounded_or_unsupported_arguments(self):
        result = await TOOL_REGISTRY.dispatch(
            "repository_search",
            {"repo_id": "fixture-repo", "query": "guide", "mode": "path", "limit": 500},
            self.context,
        )
        self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)

    async def test_registry_classifies_local_repository_timeout_and_execution_error(self):
        with patch.object(self.service, "search", side_effect=RepositoryTimedOut("fixture timeout")):
            timed_out = await TOOL_REGISTRY.dispatch(
                "repository_search",
                {"repo_id": "fixture-repo", "query": "guide", "mode": "path"},
                self.context,
            )
        self.assertEqual(timed_out.status, ToolStatus.TIMED_OUT)

        with patch.object(self.service, "search", side_effect=RepositoryExecutionError("fixture error")):
            failed = await TOOL_REGISTRY.dispatch(
                "repository_search",
                {"repo_id": "fixture-repo", "query": "guide", "mode": "path"},
                self.context,
            )
        self.assertEqual(failed.status, ToolStatus.ERROR)

    async def test_cancelled_search_keeps_sync_repository_future_tracked(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        real_search = self.service.search

        def blocking_search(*_args, **_kwargs):
            entered.set()
            release.wait()
            finished.set()
            return real_search("fixture-repo", "guide", mode="path")

        task = None
        try:
            with patch.object(self.service, "search", side_effect=blocking_search):
                task = asyncio.create_task(
                    TOOL_REGISTRY.dispatch(
                        "repository_search",
                        {"repo_id": "fixture-repo", "query": "guide", "mode": "path"},
                        self.context,
                    )
                )
                self.assertTrue(await _wait_for_event(entered))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.storage_executor.pending_count, 1)
                release.set()
                self.assertTrue(await _wait_for_event(finished))
                deadline = asyncio.get_running_loop().time() + 1.0
                while self.storage_executor.pending_count and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.005)
                self.assertEqual(self.storage_executor.pending_count, 0)
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_tree_listing_overflow_maps_to_partial_tool_status(self):
        with patch("app.repositories.local_git._MAX_TREE_OUTPUT_BYTES", 1):
            result = await TOOL_REGISTRY.dispatch(
                "repository_search",
                {"repo_id": "fixture-repo", "query": "not-present", "mode": "path"},
                self.context,
            )
        self.assertEqual(result.status, ToolStatus.PARTIAL)


if __name__ == "__main__":
    unittest.main()
