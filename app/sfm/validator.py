"""Version-pinned asynchronous validation service boundary.

No production parser is bundled. The default implementation reports that
validation is unavailable and never treats generated source as verified.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
import math
from typing import Protocol

from app.sfm.contracts import (
    MAX_SFM_SOURCE_BYTES,
    MAX_VALIDATION_TIMEOUT_SECONDS,
    SFMTargetVersion,
    ValidationResult,
    ValidationStatus,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 5.0


class SFMValidator(Protocol):
    """Async adapter implemented by an unavailable, fake, or real validator."""

    validator_id: str

    async def validate(
        self,
        source: str,
        target: SFMTargetVersion,
        *,
        timeout_seconds: float,
    ) -> ValidationResult: ...


class SFMValidationInputError(ValueError):
    """A caller supplied source or timeout outside the bounded contract."""


class UnavailableSFMValidator:
    """Safe default until a real, version-compatible parser is verified."""

    validator_id = "unavailable"

    async def validate(
        self,
        source: str,
        target: SFMTargetVersion,
        *,
        timeout_seconds: float,
    ) -> ValidationResult:
        del source, timeout_seconds
        return ValidationResult(
            status=ValidationStatus.UNAVAILABLE,
            target=target,
            validator_id=self.validator_id,
            message="没有配置经验证的 SFM Parser；未执行语法验证。",
        )


class SFMValidationService:
    """Apply common bounds, timeout handling, and result identity checks."""

    def __init__(
        self,
        validator: SFMValidator | None = None,
        *,
        trusted_validator_ids: Iterable[str] = (),
    ) -> None:
        self.validator: SFMValidator = validator if validator is not None else UnavailableSFMValidator()
        validator_id = getattr(self.validator, "validator_id", None)
        if not isinstance(validator_id, str) or not validator_id.strip() or len(validator_id) > 128:
            raise ValueError("validator must expose a non-empty validator_id of at most 128 characters")
        trusted = frozenset(trusted_validator_ids)
        if any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in trusted):
            raise ValueError("trusted validator IDs must be non-empty strings of at most 128 characters")
        if "fake" in trusted:
            raise ValueError("Fake Validator cannot be trusted for real syntax validation")
        self._validator_id = validator_id
        self._trusted_validator_ids = trusted

    @property
    def has_configured_validator(self) -> bool:
        """Whether a fake fixture or explicitly trusted real validator is installed."""
        return self._validator_id == "fake" or self._validator_id in self._trusted_validator_ids

    async def validate(
        self,
        source: str,
        *,
        target: SFMTargetVersion,
        timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    ) -> ValidationResult:
        _validate_request(source, target, timeout_seconds)
        validator_id = self._validator_id
        if not self.has_configured_validator:
            return ValidationResult(
                status=ValidationStatus.UNAVAILABLE,
                target=target,
                validator_id=validator_id,
                message="此 SFM Validator 尚未通过项目验证；未执行语法验证。",
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self.validator.validate(
                    source,
                    target,
                    timeout_seconds=timeout_seconds,
                )
        except TimeoutError:
            return ValidationResult(
                status=ValidationStatus.TIMEOUT,
                target=target,
                validator_id=validator_id,
                message="SFM Validator 超时；未得到验证结论。",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not expose parser exception text, which may include source text
            # or local paths. Keep only its class in the operator log.
            LOGGER.warning("SFM validator failed (%s)", type(exc).__name__)
            return ValidationResult(
                status=ValidationStatus.ERROR,
                target=target,
                validator_id=validator_id,
                message="SFM Validator 执行失败；未得到验证结论。",
            )

        if not isinstance(result, ValidationResult):
            return _invalid_adapter_result(target, validator_id)
        if result.target != target or result.validator_id != validator_id:
            return _invalid_adapter_result(target, validator_id)
        return result


def _validate_request(source: str, target: SFMTargetVersion, timeout_seconds: float) -> None:
    if not isinstance(source, str):
        raise SFMValidationInputError("source must be a string")
    if len(source) > MAX_SFM_SOURCE_BYTES:
        raise SFMValidationInputError(
            f"source exceeds the {MAX_SFM_SOURCE_BYTES}-byte validation limit"
        )
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SFMValidationInputError("source must contain valid UTF-8 encodable text") from exc
    if source_size > MAX_SFM_SOURCE_BYTES:
        raise SFMValidationInputError(
            f"source exceeds the {MAX_SFM_SOURCE_BYTES}-byte validation limit"
        )
    if not isinstance(target, SFMTargetVersion):
        raise SFMValidationInputError("target must be an SFMTargetVersion")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_VALIDATION_TIMEOUT_SECONDS
    ):
        raise SFMValidationInputError(
            f"timeout_seconds must be greater than zero and at most {MAX_VALIDATION_TIMEOUT_SECONDS}"
        )


def _invalid_adapter_result(target: SFMTargetVersion, validator_id: str) -> ValidationResult:
    # A malformed adapter response is an adapter failure, not a parser verdict.
    safe_id = validator_id if validator_id != "fake" else "fake"
    return ValidationResult(
        status=ValidationStatus.ERROR,
        target=target,
        validator_id=safe_id,
        message="SFM Validator 返回了与请求不匹配的结果；未得到验证结论。",
    )
