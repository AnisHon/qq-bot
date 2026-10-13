"""Contracts for version-pinned SFM source validation.

These contracts do not parse SFM. A future real validator must identify its
implementation and supported SFM / Minecraft version pair in every result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any


MAX_SFM_SOURCE_BYTES = 64 * 1024
MAX_VALIDATION_TIMEOUT_SECONDS = 30.0
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<!\w)[A-Z]:(?:\\|/)")
_UNC_PATH_RE = re.compile(r"(?<!\w)\\\\")
_FILE_URL_RE = re.compile(r"(?i)\bfile://")
# Parser diagnostics are untrusted. Detect the start of an absolute POSIX path
# and hide the whole diagnostic: trying to find its end is unreliable when
# directory names contain spaces.
_POSIX_PATH_START_RE = re.compile(r"(?<![:/\w])/(?!/)")
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|client[_ -]?secret|token|password|secret|authorization)"
    r"\b\s*[:=]\s*(?:(?:bearer|basic)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"hf_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|glpat-[A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)


def safe_diagnostic_code(value: str) -> str:
    """Keep only a short, non-path identifier safe for chat and model output."""
    sanitized = safe_diagnostic_text(value, limit=128)
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", sanitized):
        return sanitized
    return "validator-diagnostic"


def safe_diagnostic_text(value: str, *, limit: int = 300) -> str:
    """Sanitize untrusted parser text before it reaches QQ or a model prompt."""
    message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    if (
        _WINDOWS_PATH_RE.search(message)
        or _UNC_PATH_RE.search(message)
        or _FILE_URL_RE.search(message)
        or _POSIX_PATH_START_RE.search(message)
    ):
        return "诊断含本地路径，已隐藏完整内容。"
    message = _CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", message)
    message = _BEARER_RE.sub("Bearer [已隐藏]", message)
    message = _TOKEN_RE.sub("[已隐藏凭据]", message)
    message = re.sub(r"\s+", " ", message).strip()
    return message[:limit] or "Validator 返回了诊断，但未提供可安全显示的描述。"


@dataclass(frozen=True, slots=True)
class SFMTargetVersion:
    sfm_version: str
    minecraft_version: str

    def __post_init__(self) -> None:
        for field_name in ("sfm_version", "minecraft_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{field_name} must be a non-empty version string of at most 128 characters")
            if any(ord(char) < 32 for char in value):
                raise ValueError(f"{field_name} must not contain control characters")

    def to_dict(self) -> dict[str, str]:
        return {
            "sfm_version": self.sfm_version,
            "minecraft_version": self.minecraft_version,
        }


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ValidatorDiagnostic:
    code: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip() or len(self.code) > 128:
            raise ValueError("diagnostic code must be non-empty and at most 128 characters")
        if not isinstance(self.message, str) or len(self.message) > 2000:
            raise ValueError("diagnostic message must be a string of at most 2000 characters")
        if not isinstance(self.severity, DiagnosticSeverity):
            raise ValueError("diagnostic severity is invalid")
        for name in ("line", "column", "end_line", "end_column"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError(f"{name} must be a positive integer or None")
        if self.end_line is not None and self.line is None:
            raise ValueError("end_line requires line")
        if self.end_column is not None and self.column is None:
            raise ValueError("end_column requires column")
        if self.end_line is not None and self.line is not None and self.end_line < self.line:
            raise ValueError("end_line must not precede line")
        if (
            self.end_line == self.line
            and self.end_column is not None
            and self.column is not None
            and self.end_column < self.column
        ):
            raise ValueError("end_column must not precede column on the same line")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": safe_diagnostic_code(self.code),
            "message": safe_diagnostic_text(self.message),
            "severity": self.severity.value,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }


class ValidationStatus(str, Enum):
    # Only a real, version-compatible validator may emit VALID / INVALID.
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    VERSION_UNSUPPORTED = "version_unsupported"
    TIMEOUT = "timeout"
    ERROR = "error"
    # Test doubles use distinct statuses so their result cannot be reported as
    # successful real syntax validation by downstream tools or workflows.
    SIMULATED_VALID = "simulated_valid"
    SIMULATED_INVALID = "simulated_invalid"
    SIMULATED_VERSION_UNSUPPORTED = "simulated_version_unsupported"


_SIMULATED_STATUSES = frozenset(
    {
        ValidationStatus.SIMULATED_VALID,
        ValidationStatus.SIMULATED_INVALID,
        ValidationStatus.SIMULATED_VERSION_UNSUPPORTED,
    }
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: ValidationStatus
    target: SFMTargetVersion
    validator_id: str
    diagnostics: tuple[ValidatorDiagnostic, ...] = ()
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ValidationStatus):
            raise ValueError("validation status is invalid")
        if not isinstance(self.target, SFMTargetVersion):
            raise ValueError("validation target is invalid")
        if (
            not isinstance(self.validator_id, str)
            or not self.validator_id.strip()
            or len(self.validator_id) > 128
        ):
            raise ValueError("validator_id must be non-empty and at most 128 characters")
        if not isinstance(self.diagnostics, (tuple, list)):
            raise ValueError("diagnostics must be a bounded tuple or list of structured entries")
        if len(self.diagnostics) > 256 or any(
            not isinstance(item, ValidatorDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("diagnostics must contain at most 256 structured entries")
        diagnostics = tuple(self.diagnostics)
        object.__setattr__(self, "diagnostics", diagnostics)
        if self.message is not None and (not isinstance(self.message, str) or len(self.message) > 2000):
            raise ValueError("message must be a string of at most 2000 characters")
        is_fake = self.validator_id == "fake"
        if self.status in _SIMULATED_STATUSES and not is_fake:
            raise ValueError("simulated statuses require validator_id='fake'")
        if is_fake and self.status in {ValidationStatus.VALID, ValidationStatus.INVALID}:
            raise ValueError("Fake Validator cannot emit real VALID / INVALID statuses")

    @property
    def is_simulated(self) -> bool:
        return self.status in _SIMULATED_STATUSES

    @property
    def is_real_validation(self) -> bool:
        return self.status in {ValidationStatus.VALID, ValidationStatus.INVALID}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "target": self.target.to_dict(),
            "validator_id": safe_diagnostic_text(self.validator_id, limit=128),
            "is_simulated": self.is_simulated,
            "is_real_validation": self.is_real_validation,
            "message": safe_diagnostic_text(self.message, limit=240) if self.message else None,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
