from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import subprocess
import time
from typing import Mapping


_REPO_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[._-])(?:secret|credentials?|token|password|passwd|private[_-]?key)(?:[._-]|$)",
    flags=re.IGNORECASE,
)
_DENIED_DIRS = frozenset({".git", ".ssh", ".aws", ".azure", ".kube", "secrets", "credentials"})
_DENIED_NAMES = frozenset(
    {
        "authorized_keys",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "known_hosts",
        "secrets",
        "secrets.json",
        "token",
        "tokens.json",
    }
)
_DENIED_SUFFIXES = frozenset(
    {".db", ".dump", ".jks", ".key", ".keystore", ".log", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3"}
)
_MAX_CONFIGURED_FILE_BYTES = 8 * 1024 * 1024
_MAX_TREE_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_SEARCH_FILES = 1000
_MAX_SEARCH_CONTENT_BYTES = 4 * 1024 * 1024
_MAX_SEARCH_QUERY_CHARS = 256
_MAX_SEARCH_PAGE_SIZE = 20
_MAX_READ_LINES = 200
_MAX_READ_OUTPUT_BYTES = 16 * 1024


class RepositoryError(RuntimeError):
    """Base class for safe local Git repository failures."""


class RepositoryDataUnavailable(RepositoryError):
    """The requested repository is not configured or has no commit."""


class RepositoryTimedOut(RepositoryError):
    """A bounded local Git operation exceeded its time budget."""


class RepositoryExecutionError(RepositoryError):
    """A local Git operation failed for a reason other than missing data."""


class RepositoryConfigurationError(RepositoryError):
    """An administrator-configured repository root is invalid."""


class RepositoryPathDenied(RepositoryError):
    """A path is outside the repository or violates the read policy."""


class RepositoryFileUnavailable(RepositoryError):
    """A safe regular file does not exist in the pinned commit."""


class RepositoryInvalidRange(RepositoryError):
    """A requested line range is invalid or outside the file."""


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    repo_id: str
    commit_sha: str


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    repo_id: str
    snapshot: SnapshotRef
    path: str
    object_id: str
    byte_size: int
    executable: bool


@dataclass(frozen=True, slots=True)
class RepositorySearchMatch:
    path: str
    line: int | None = None
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class RepositorySearchPage:
    snapshot: SnapshotRef
    matches: tuple[RepositorySearchMatch, ...]
    next_offset: int | None
    truncated: bool
    files_scanned: int


@dataclass(frozen=True, slots=True)
class RepositoryReadResult:
    file: RepositoryFile
    start_line: int
    end_line: int
    text: str
    truncated: bool


def safe_relative_path(value: str) -> PurePosixPath:
    """Normalize a model-supplied Git path without accepting filesystem paths."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise RepositoryPathDenied("repository path is empty or too long")
    if "\x00" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RepositoryPathDenied("repository path contains control characters")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise RepositoryPathDenied("repository path must be a relative POSIX path")
    if re.search(r"%(?:2f|5c)", value, flags=re.IGNORECASE):
        raise RepositoryPathDenied("repository path contains an encoded separator")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or len(parts) > 32:
        raise RepositoryPathDenied("repository path contains an unsafe component")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise RepositoryPathDenied("repository path component is too long")
    path = PurePosixPath(*parts)
    _assert_not_sensitive(path)
    return path


def _assert_not_sensitive(path: PurePosixPath) -> None:
    lowered_parts = tuple(part.casefold() for part in path.parts)
    if any(part in _DENIED_DIRS for part in lowered_parts):
        raise RepositoryPathDenied("repository path is excluded by the sensitive-path policy")
    for name in lowered_parts:
        if (
            name.startswith(".env")
            or name in _DENIED_NAMES
            or _SENSITIVE_NAME_RE.search(name)
            or Path(name).suffix in _DENIED_SUFFIXES
        ):
            raise RepositoryPathDenied("repository path is excluded by the sensitive-path policy")


class LocalGitRepositoryService:
    """Read-only access to a fixed administrator allowlist of Git worktrees.

    Only fixed Git inspection commands are issued. They read commit objects and
    never checkout, fetch, run repository scripts, or follow worktree symlinks.
    """

    def __init__(
        self,
        repositories: Mapping[str, str | Path] | tuple[tuple[str, str | Path], ...] = (),
        *,
        max_file_bytes: int = 1_000_000,
        command_timeout_seconds: float = 3.0,
    ) -> None:
        if not 1 <= max_file_bytes <= _MAX_CONFIGURED_FILE_BYTES:
            raise ValueError("max_file_bytes must be between 1 byte and 8 MiB")
        if not 0.1 <= command_timeout_seconds <= 30:
            raise ValueError("command_timeout_seconds must be between 0.1 and 30 seconds")
        items = repositories.items() if isinstance(repositories, Mapping) else repositories
        roots: dict[str, Path] = {}
        for raw_repo_id, raw_root in items:
            repo_id = str(raw_repo_id)
            if not _REPO_ID_RE.fullmatch(repo_id) or repo_id in {".", ".."}:
                raise RepositoryConfigurationError("repository id is invalid")
            if repo_id in roots:
                raise RepositoryConfigurationError("repository id is duplicated")
            configured = Path(raw_root).expanduser()
            if not configured.is_absolute():
                raise RepositoryConfigurationError("repository root must be absolute")
            try:
                root = configured.resolve(strict=True)
            except OSError as exc:
                raise RepositoryConfigurationError("repository root is unavailable") from exc
            if Path(os.path.abspath(configured)) != root or not root.is_dir():
                raise RepositoryConfigurationError("repository root must be a real, non-symlink directory")
            roots[repo_id] = root
        self._roots = roots
        self._max_file_bytes = max_file_bytes
        self._timeout = command_timeout_seconds
        for root in roots.values():
            try:
                reported_root = Path(
                    self._git(root, "rev-parse", "--show-toplevel")
                    .decode("utf-8", errors="strict")
                    .strip()
                ).resolve(strict=True)
            except (RepositoryError, UnicodeError, OSError) as exc:
                raise RepositoryConfigurationError("configured path is not a readable Git worktree") from exc
            if reported_root != root:
                raise RepositoryConfigurationError("configured path must be the Git worktree root")

    @classmethod
    def from_settings(cls, settings, **kwargs) -> LocalGitRepositoryService:
        """Build from the static settings allowlist; model input never sets roots."""
        return cls(getattr(settings, "local_git_repositories", ()), **kwargs)

    @property
    def configured_repo_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._roots))

    def snapshot(
        self,
        repo_id: str,
        commit_sha: str | None = None,
        *,
        deadline_monotonic: float | None = None,
    ) -> SnapshotRef:
        root = self._get_root(repo_id)
        timeout_seconds = (
            self._remaining(deadline_monotonic)
            if deadline_monotonic is not None
            else self._timeout
        )
        if commit_sha is None:
            output = self._git(
                root,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                "HEAD^{commit}",
                timeout_seconds=timeout_seconds,
                missing_object_is_unavailable=True,
            )
            resolved = output.decode("ascii", errors="strict").strip()
        else:
            if not _COMMIT_RE.fullmatch(commit_sha):
                raise RepositoryPathDenied("snapshot must be a full commit SHA")
            output = self._git(
                root,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{commit_sha.lower()}^{{commit}}",
                timeout_seconds=timeout_seconds,
                missing_object_is_unavailable=True,
            )
            resolved = output.decode("ascii", errors="strict").strip()
            if resolved.lower() != commit_sha.lower():
                raise RepositoryPathDenied("snapshot does not identify the requested commit")
        if not _COMMIT_RE.fullmatch(resolved):
            raise RepositoryError("Git returned an invalid commit identifier")
        return SnapshotRef(repo_id=repo_id, commit_sha=resolved.lower())

    def inspect_file(
        self,
        repo_id: str,
        relative_path: str,
        snapshot: SnapshotRef,
        *,
        deadline_monotonic: float | None = None,
    ) -> RepositoryFile:
        """Validate and return metadata for one regular text blob in a commit."""
        deadline = deadline_monotonic or time.monotonic() + min(8.0, self._timeout * 4)
        root = self._get_root(repo_id)
        if snapshot.repo_id != repo_id or not _COMMIT_RE.fullmatch(snapshot.commit_sha):
            raise RepositoryPathDenied("snapshot does not belong to the configured repository")
        pinned = self.snapshot(repo_id, snapshot.commit_sha, deadline_monotonic=deadline)
        path = safe_relative_path(relative_path)
        treeish = pinned.commit_sha
        entry: tuple[str, str, str, int | None] | None = None
        for index, component in enumerate(path.parts):
            entry = self._tree_entry(
                root,
                treeish,
                component,
                timeout_seconds=self._remaining(deadline),
            )
            if entry is None:
                raise RepositoryFileUnavailable("file is not present in the pinned commit")
            mode, object_type, object_id, object_size = entry
            last = index == len(path.parts) - 1
            if not last:
                if mode != "040000" or object_type != "tree":
                    raise RepositoryPathDenied("repository path crosses a non-directory or symlink")
                treeish = object_id
                continue
            if mode not in {"100644", "100755"} or object_type != "blob":
                raise RepositoryPathDenied("only regular Git files are readable")
            if object_size is None or object_size > self._max_file_bytes:
                raise RepositoryPathDenied("repository file exceeds the configured size limit")
            blob = self._git(
                root,
                "cat-file",
                "blob",
                object_id,
                timeout_seconds=self._remaining(deadline),
            )
            if len(blob) != object_size:
                raise RepositoryError("Git blob size did not match its tree entry")
            if b"\x00" in blob:
                raise RepositoryPathDenied("binary repository files are not readable")
            try:
                blob.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RepositoryPathDenied("binary repository files are not readable") from exc
            return RepositoryFile(
                repo_id=repo_id,
                snapshot=pinned,
                path=path.as_posix(),
                object_id=object_id,
                byte_size=object_size,
                executable=mode == "100755",
            )
        raise RepositoryFileUnavailable("file is not present in the pinned commit")

    def search(
        self,
        repo_id: str,
        query: str,
        *,
        mode: str = "path",
        commit_sha: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> RepositorySearchPage:
        """Search paths or UTF-8 text at one bounded, immutable Git snapshot."""
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_SEARCH_QUERY_CHARS:
            raise RepositoryPathDenied("repository search query is empty or too long")
        if "\x00" in query or any(ord(char) < 32 for char in query):
            raise RepositoryPathDenied("repository search query contains control characters")
        if mode not in {"path", "text"}:
            raise RepositoryPathDenied("repository search mode is invalid")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000:
            raise RepositoryPathDenied("repository search offset is outside the supported range")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_SEARCH_PAGE_SIZE:
            raise RepositoryPathDenied("repository search page size is outside the supported range")

        root = self._get_root(repo_id)
        deadline = time.monotonic() + min(6.0, self._timeout * 2)
        pinned = self.snapshot(repo_id, commit_sha, deadline_monotonic=deadline)
        tree_entries, tree_truncated = self._list_tree(
            root,
            pinned.commit_sha,
            timeout_seconds=self._remaining(deadline),
        )
        query_folded = query.casefold()
        matches: list[RepositorySearchMatch] = []
        files_scanned = 0
        bytes_scanned = 0
        truncated = tree_truncated
        wanted = offset + limit + 1

        for raw_path, mode_bits, object_id, size in tree_entries:
            if mode_bits not in {"100644", "100755"} or size is None:
                continue
            try:
                path_text = raw_path.decode("utf-8", errors="strict")
                safe_path = safe_relative_path(path_text)
            except (UnicodeDecodeError, RepositoryPathDenied):
                continue
            normalized_path = safe_path.as_posix()
            if mode == "path":
                if query_folded in normalized_path.casefold():
                    matches.append(RepositorySearchMatch(path=normalized_path))
                    if len(matches) >= wanted:
                        break
                continue

            if files_scanned >= _MAX_SEARCH_FILES:
                truncated = True
                break
            if bytes_scanned + size > _MAX_SEARCH_CONTENT_BYTES:
                truncated = True
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                break
            files_scanned += 1
            bytes_scanned += size
            if size > self._max_file_bytes:
                truncated = True
                continue
            try:
                blob = self._git(
                    root,
                    "cat-file",
                    "blob",
                    object_id,
                    timeout_seconds=remaining,
                )
                if len(blob) != size or b"\x00" in blob:
                    continue
                text = blob.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
            except RepositoryError:
                truncated = True
                break
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query_folded in line.casefold():
                    snippet = re.sub(r"\s+", " ", line).strip()
                    if len(snippet) > 240:
                        snippet = snippet[:237].rstrip() + "..."
                    matches.append(RepositorySearchMatch(path=normalized_path, line=line_number, snippet=snippet))
                    break
            if len(matches) >= wanted:
                break

        page = tuple(matches[offset : offset + limit])
        has_more = len(matches) > offset + len(page)
        next_offset = offset + len(page) if has_more and page else None
        return RepositorySearchPage(
            snapshot=pinned,
            matches=page,
            next_offset=next_offset,
            truncated=truncated,
            files_scanned=files_scanned,
        )

    def read_lines(
        self,
        repo_id: str,
        relative_path: str,
        snapshot: SnapshotRef,
        *,
        start_line: int,
        end_line: int,
    ) -> RepositoryReadResult:
        """Read a bounded one-based line range from a pinned regular text blob."""
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
            or end_line - start_line + 1 > _MAX_READ_LINES
        ):
            raise RepositoryInvalidRange("requested line range is invalid")
        root = self._get_root(repo_id)
        deadline = time.monotonic() + min(6.0, self._timeout * 2)
        file = self.inspect_file(repo_id, relative_path, snapshot, deadline_monotonic=deadline)
        blob = self._git(
            root,
            "cat-file",
            "blob",
            file.object_id,
            timeout_seconds=self._remaining(deadline),
        )
        text = blob.decode("utf-8", errors="strict")
        lines = text.splitlines()
        if start_line > len(lines):
            raise RepositoryInvalidRange("requested start line is outside the file")
        last_line = min(end_line, len(lines))
        rendered = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start_line, last_line + 1))
        encoded = rendered.encode("utf-8")
        was_truncated = len(encoded) > _MAX_READ_OUTPUT_BYTES
        if was_truncated:
            marker = "\n[output clipped]"
            marker_bytes = len(marker.encode("utf-8"))
            rendered = encoded[: _MAX_READ_OUTPUT_BYTES - marker_bytes].decode("utf-8", errors="ignore").rstrip()
            rendered += marker
        return RepositoryReadResult(
            file=file,
            start_line=start_line,
            end_line=last_line,
            text=rendered,
            truncated=was_truncated,
        )

    def _list_tree(
        self,
        root: Path,
        commit_sha: str,
        *,
        timeout_seconds: float,
    ) -> tuple[list[tuple[bytes, str, str, int | None]], bool]:
        """Return a size-bounded recursive tree listing; terminate oversized output."""
        env = self._git_environment()
        command = ["git", "-C", str(root), "--literal-pathspecs", "ls-tree", "-r", "-z", "-l", commit_sha]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise RepositoryExecutionError("local Git search failed") from exc
        assert process.stdout is not None
        records: list[tuple[bytes, str, str, int | None]] = []
        buffer = bytearray()
        total = 0
        truncated = False
        deadline = time.monotonic() + min(self._timeout, timeout_seconds)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise RepositoryTimedOut("local Git search timed out")
                ready = selector.select(min(remaining, 0.1))
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), min(64 * 1024, _MAX_TREE_OUTPUT_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                buffer.extend(chunk)
                while b"\x00" in buffer:
                    record, _, rest = buffer.partition(b"\x00")
                    buffer[:] = rest
                    if record:
                        records.append(self._parse_tree_record(record))
                if total > _MAX_TREE_OUTPUT_BYTES:
                    truncated = True
                    process.terminate()
                    break
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RepositoryTimedOut("local Git search timed out")
            if not truncated and process.returncode != 0:
                raise RepositoryExecutionError("local Git search failed")
            if buffer and not truncated:
                records.append(self._parse_tree_record(bytes(buffer)))
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        return records, truncated

    @staticmethod
    def _parse_tree_record(record: bytes) -> tuple[bytes, str, str, int | None]:
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RepositoryError("Git returned malformed tree metadata")
        fields = header.decode("ascii", errors="strict").split()
        if len(fields) != 4:
            raise RepositoryError("Git returned incomplete tree metadata")
        try:
            size = int(fields[3]) if fields[3] != "-" else None
        except ValueError as exc:
            raise RepositoryError("Git returned invalid file size metadata") from exc
        return raw_path, fields[0], fields[2], size

    @staticmethod
    def _git_environment() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
        return env

    def _get_root(self, repo_id: str) -> Path:
        root = self._roots.get(repo_id)
        if root is None:
            raise RepositoryDataUnavailable("local repository is not configured")
        return root

    @staticmethod
    def _remaining(deadline_monotonic: float) -> float:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise RepositoryTimedOut("local Git request timed out")
        return remaining

    def _tree_entry(
        self,
        root: Path,
        treeish: str,
        component: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, str, str, int | None] | None:
        output = self._git(
            root,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "-l",
            treeish,
            "--",
            component,
            timeout_seconds=timeout_seconds,
        )
        matches: list[tuple[str, str, str, int | None]] = []
        for record in output.split(b"\x00"):
            if not record:
                continue
            header, separator, raw_name = record.partition(b"\t")
            if not separator or raw_name != component.encode("utf-8"):
                continue
            fields = header.decode("ascii", errors="strict").split()
            if len(fields) not in {3, 4}:
                raise RepositoryError("Git returned malformed tree metadata")
            try:
                size = int(fields[3]) if len(fields) == 4 and fields[3] != "-" else None
            except ValueError as exc:
                raise RepositoryError("Git returned invalid file size metadata") from exc
            matches.append((fields[0], fields[1], fields[2], size))
        if len(matches) > 1:
            raise RepositoryError("Git returned ambiguous tree metadata")
        return matches[0] if matches else None

    def _git(
        self,
        root: Path,
        *args: str,
        timeout_seconds: float | None = None,
        missing_object_is_unavailable: bool = False,
    ) -> bytes:
        # Git honors many environment overrides (GIT_DIR, GIT_WORK_TREE,
        # GIT_OBJECT_DIRECTORY, config injection, SSH command, and others).
        # None are needed for these fixed local read commands.
        command = ["git", "-C", str(root), *args]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._git_environment(),
                timeout=min(self._timeout, timeout_seconds) if timeout_seconds is not None else self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepositoryTimedOut("local Git inspection timed out") from exc
        except OSError as exc:
            raise RepositoryExecutionError("local Git inspection failed") from exc
        if result.returncode != 0:
            if missing_object_is_unavailable and result.returncode == 1:
                raise RepositoryDataUnavailable("local Git object or commit is unavailable")
            raise RepositoryExecutionError("local Git command failed")
        return result.stdout
