"""Deterministic Validator test double; it does not implement SFM grammar."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.sfm.contracts import (
    SFMTargetVersion,
    ValidationResult,
    ValidationStatus,
    ValidatorDiagnostic,
)


class FakeSFMValidator:
    """Return simulated outcomes only for explicitly supplied test samples."""

    validator_id = "fake"

    def __init__(
        self,
        *,
        valid_samples: Iterable[str] = (),
        invalid_samples: Mapping[str, tuple[ValidatorDiagnostic, ...]] | None = None,
        unsupported_targets: Iterable[SFMTargetVersion] = (),
    ) -> None:
        self._valid_samples = frozenset(valid_samples)
        self._invalid_samples = dict(invalid_samples or {})
        self._unsupported_targets = frozenset(unsupported_targets)

    async def validate(
        self,
        source: str,
        target: SFMTargetVersion,
        *,
        timeout_seconds: float,
    ) -> ValidationResult:
        del timeout_seconds
        if target in self._unsupported_targets:
            return ValidationResult(
                status=ValidationStatus.SIMULATED_VERSION_UNSUPPORTED,
                target=target,
                validator_id=self.validator_id,
                message="Fake Validator 配置为不支持此测试版本；这不是 Parser 兼容性结论。",
            )
        if source in self._valid_samples:
            return ValidationResult(
                status=ValidationStatus.SIMULATED_VALID,
                target=target,
                validator_id=self.validator_id,
                message="Fake Validator 命中了固定测试样例；这不是 SFM 语法验证。",
            )
        diagnostics = self._invalid_samples.get(source)
        if diagnostics is not None:
            return ValidationResult(
                status=ValidationStatus.SIMULATED_INVALID,
                target=target,
                validator_id=self.validator_id,
                diagnostics=diagnostics,
                message="Fake Validator 返回固定测试诊断；这不是 SFM 语法验证。",
            )
        return ValidationResult(
            status=ValidationStatus.SIMULATED_INVALID,
            target=target,
            validator_id=self.validator_id,
            diagnostics=(
                ValidatorDiagnostic(
                    code="fake_sample_not_configured",
                    message="Fake Validator 没有此固定样例的预设结果。",
                    line=1,
                    column=1,
                ),
            ),
            message="没有匹配的 Fake Validator 样例；不能据此判断真实语法。",
        )
