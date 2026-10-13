"""Read-only adapters for explicitly configured local repositories."""

from app.repositories.local_git import (
    LocalGitRepositoryService,
    RepositoryConfigurationError,
    RepositoryDataUnavailable,
    RepositoryError,
    RepositoryFile,
    RepositoryFileUnavailable,
    RepositoryPathDenied,
    SnapshotRef,
    safe_relative_path,
)

__all__ = [
    "LocalGitRepositoryService",
    "RepositoryConfigurationError",
    "RepositoryDataUnavailable",
    "RepositoryError",
    "RepositoryFile",
    "RepositoryFileUnavailable",
    "RepositoryPathDenied",
    "SnapshotRef",
    "safe_relative_path",
]
