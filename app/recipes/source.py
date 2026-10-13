"""Source-neutral contracts for importing versioned recipe records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol

from app.recipes.models import (
    RawRecipeRecord,
    Recipe,
    RecipeDataset,
    SourceLocator,
)


class ImportIssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RecipeSourceRecord:
    """One preserved source record and the domain recipes parsed from it."""

    raw_record: RawRecipeRecord
    recipes: tuple[Recipe, ...]
    issues: tuple[ImportIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.raw_record, RawRecipeRecord):
            raise ValueError("source record must contain a RawRecipeRecord")
        recipes = tuple(self.recipes)
        if any(not isinstance(recipe, Recipe) for recipe in recipes):
            raise ValueError("source record recipes must contain Recipe values")
        issues = tuple(self.issues)
        if any(not isinstance(issue, ImportIssue) for issue in issues):
            raise ValueError("source record issues must contain ImportIssue values")
        object.__setattr__(self, "recipes", recipes)
        object.__setattr__(self, "issues", issues)


class RecipeSource(Protocol):
    """A repeatable source adapter; implementations do not need a specific exporter."""

    @property
    def manifest(self) -> RecipeDataset: ...

    def iter_records(self) -> Iterable[RecipeSourceRecord]: ...


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: str
    message: str
    severity: ImportIssueSeverity = ImportIssueSeverity.ERROR
    source_record_id: str | None = None
    source_locator: SourceLocator | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code or not self.code.replace("_", "").isalnum():
            raise ValueError("import issue code must be a stable identifier")
        if not isinstance(self.message, str) or not self.message or "\x00" in self.message:
            raise ValueError("import issue message must be non-empty text")
        if not isinstance(self.severity, ImportIssueSeverity):
            raise ValueError("import issue severity is invalid")
        if self.source_record_id is not None and (
            not isinstance(self.source_record_id, str) or not self.source_record_id
        ):
            raise ValueError("source_record_id must be non-empty when supplied")
        if self.source_locator is not None and not isinstance(self.source_locator, SourceLocator):
            raise ValueError("source_locator must be a SourceLocator")
