from __future__ import annotations

import asyncio
import unittest

from app.sfm.contracts import (
    DiagnosticSeverity,
    MAX_SFM_SOURCE_BYTES,
    SFMTargetVersion,
    ValidationResult,
    ValidationStatus,
    ValidatorDiagnostic,
    safe_diagnostic_text,
    safe_diagnostic_code,
)
from app.sfm.fake_validator import FakeSFMValidator
from app.sfm.validator import (
    SFMValidationInputError,
    SFMValidationService,
    UnavailableSFMValidator,
)


TARGET = SFMTargetVersion("sfm-fixture-1", "mc-fixture-1")
VALID_SAMPLE = "SYNTHETIC_FAKE_VALID_SAMPLE\n"
INVALID_SAMPLE = "SYNTHETIC_FAKE_INVALID_SAMPLE\n"


class SFMValidationContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_safe_diagnostic_text_redacts_paths_and_common_token_formats(self) -> None:
        message = (
            r"failure at \\server\share\parser.py /tmp github_pat_1234567890abcdef "
            "hf_1234567890abcdef sk-1234567890abcdefgh api_key=fixture-secret "
            "API key=another-secret Authorization: Basic dXNlcjpwYXNzd29yZA=="
        )
        sanitized = safe_diagnostic_text(message)
        for secret in (
            r"\\server\share\parser.py",
            "/tmp",
            "github_pat_1234567890abcdef",
            "hf_1234567890abcdef",
            "sk-1234567890abcdefgh",
            "fixture-secret",
            "another-secret",
            "dXNlcjpwYXNzd29yZA==",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertEqual(sanitized, "诊断含本地路径，已隐藏完整内容。")

    async def test_absolute_paths_with_spaces_hide_the_entire_diagnostic(self) -> None:
        for message in (
            r"parser failed: C:\Program Files\SFM Parser\parser.exe at C:\Users\Example Name\source.sfm",
            "parser failed: /opt/sfm parser/bin/parser.py while reading /home/user name/source.sfm",
            r"parser failed: \\server\share name\SFM Parser\parser.exe",
            "parser failed: file:///opt/sfm parser/parser.py",
        ):
            with self.subTest(message=message):
                sanitized = safe_diagnostic_text(message)
                self.assertEqual(sanitized, "诊断含本地路径，已隐藏完整内容。")
                self.assertNotIn("parser.exe", sanitized)
                self.assertNotIn("parser.py", sanitized)
                self.assertNotIn("source.sfm", sanitized)

    async def test_diagnostic_codes_cannot_expose_credentials_or_paths(self) -> None:
        for code in (
            "sk-1234567890abcdefgh",
            "github_pat_1234567890abcdef",
            "hf_1234567890abcdef",
            "glpat-1234567890abcdef",
            r"C:\Program Files\SFM Parser\parser.exe",
            "/opt/sfm parser/bin/parser.py",
        ):
            with self.subTest(code=code):
                safe_code = safe_diagnostic_code(code)
                self.assertEqual(safe_code, "validator-diagnostic")
                self.assertNotIn(code, safe_code)

    async def test_default_validator_is_explicitly_unavailable(self) -> None:
        self.assertIsInstance(SFMValidationService().validator, UnavailableSFMValidator)
        result = await SFMValidationService().validate("anything", target=TARGET)
        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertEqual(result.validator_id, "unavailable")
        self.assertFalse(result.is_real_validation)
        self.assertFalse(result.is_simulated)
        self.assertIn("未执行语法验证", result.message)
        self.assertEqual(result.to_dict()["target"], TARGET.to_dict())

    async def test_fake_fixture_outcomes_are_never_real_valid_or_invalid(self) -> None:
        diagnostic = ValidatorDiagnostic(
            code="synthetic_expected_error",
            message="人工构造的诊断；不代表 SFM Parser 结果。",
            severity=DiagnosticSeverity.ERROR,
            line=2,
            column=5,
            end_line=2,
            end_column=9,
        )
        fake = FakeSFMValidator(
            valid_samples={VALID_SAMPLE},
            invalid_samples={INVALID_SAMPLE: (diagnostic,)},
            unsupported_targets={SFMTargetVersion("sfm-other", "mc-other")},
        )
        service = SFMValidationService(fake)

        simulated_valid = await service.validate(VALID_SAMPLE, target=TARGET)
        self.assertEqual(simulated_valid.status, ValidationStatus.SIMULATED_VALID)
        self.assertEqual(simulated_valid.validator_id, "fake")
        self.assertTrue(simulated_valid.is_simulated)
        self.assertFalse(simulated_valid.is_real_validation)

        simulated_invalid = await service.validate(INVALID_SAMPLE, target=TARGET)
        self.assertEqual(simulated_invalid.status, ValidationStatus.SIMULATED_INVALID)
        self.assertEqual(simulated_invalid.diagnostics[0].line, 2)
        self.assertEqual(simulated_invalid.diagnostics[0].column, 5)
        encoded = simulated_invalid.to_dict()
        self.assertEqual(encoded["validator_id"], "fake")
        self.assertTrue(encoded["is_simulated"])
        self.assertFalse(encoded["is_real_validation"])
        self.assertEqual(encoded["diagnostics"][0]["end_column"], 9)

        unsupported = await service.validate(
            VALID_SAMPLE,
            target=SFMTargetVersion("sfm-other", "mc-other"),
        )
        self.assertEqual(unsupported.status, ValidationStatus.SIMULATED_VERSION_UNSUPPORTED)
        self.assertFalse(unsupported.is_real_validation)

    async def test_fake_validator_rejects_real_status_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot emit real"):
            ValidationResult(
                status=ValidationStatus.VALID,
                target=TARGET,
                validator_id="fake",
            )
        with self.assertRaisesRegex(ValueError, "require validator_id='fake'"):
            ValidationResult(
                status=ValidationStatus.SIMULATED_VALID,
                target=TARGET,
                validator_id="sfm-parser-unverified",
            )

    async def test_untrusted_adapter_cannot_claim_real_validation(self) -> None:
        adapter = _UntrustedValidValidator()
        service = SFMValidationService(adapter)
        result = await service.validate("source", target=TARGET)
        self.assertFalse(service.has_configured_validator)
        self.assertEqual(result.status, ValidationStatus.UNAVAILABLE)
        self.assertFalse(result.is_real_validation)
        self.assertFalse(adapter.called)

    async def test_diagnostics_are_frozen_and_must_be_bounded_sequences(self) -> None:
        diagnostic = ValidatorDiagnostic(code="fixture", message="fixture")
        result = ValidationResult(
            status=ValidationStatus.ERROR,
            target=TARGET,
            validator_id="fixture-adapter",
            diagnostics=[diagnostic],  # type: ignore[arg-type]
        )
        self.assertEqual(result.diagnostics, (diagnostic,))
        with self.assertRaisesRegex(ValueError, "bounded tuple or list"):
            ValidationResult(
                status=ValidationStatus.ERROR,
                target=TARGET,
                validator_id="fixture-adapter",
                diagnostics=iter((diagnostic,)),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "at most 256"):
            ValidationResult(
                status=ValidationStatus.ERROR,
                target=TARGET,
                validator_id="fixture-adapter",
                diagnostics=(diagnostic,) * 257,
            )

    async def test_input_source_target_and_timeout_are_bounded(self) -> None:
        service = SFMValidationService()
        with self.assertRaises(SFMValidationInputError):
            await service.validate("x" * (MAX_SFM_SOURCE_BYTES + 1), target=TARGET)
        with self.assertRaises(SFMValidationInputError):
            await service.validate("\ud800", target=TARGET)
        with self.assertRaises(SFMValidationInputError):
            await service.validate("source", target="sfm-1")  # type: ignore[arg-type]
        for timeout in (0, -1, 31, float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(SFMValidationInputError):
                await service.validate("source", target=TARGET, timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            SFMTargetVersion("\n", "mc-1")

    async def test_timeout_returns_structured_status_and_cancels_adapter(self) -> None:
        adapter = _HangingValidator()
        service = SFMValidationService(adapter, trusted_validator_ids={adapter.validator_id})
        result = await service.validate("source", target=TARGET, timeout_seconds=0.01)
        self.assertEqual(result.status, ValidationStatus.TIMEOUT)
        self.assertEqual(result.validator_id, adapter.validator_id)
        self.assertFalse(result.is_real_validation)
        self.assertTrue(adapter.cancelled.is_set())

    async def test_caller_cancellation_propagates_to_adapter(self) -> None:
        adapter = _HangingValidator()
        service = SFMValidationService(adapter, trusted_validator_ids={adapter.validator_id})
        task = asyncio.create_task(service.validate("source", target=TARGET, timeout_seconds=10))
        await asyncio.wait_for(adapter.entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(adapter.cancelled.is_set())

    async def test_mismatched_adapter_result_and_exception_are_errors(self) -> None:
        mismatch_adapter = _MismatchedValidator()
        mismatch = SFMValidationService(
            mismatch_adapter,
            trusted_validator_ids={mismatch_adapter.validator_id},
        )
        result = await mismatch.validate("source", target=TARGET)
        self.assertEqual(result.status, ValidationStatus.ERROR)
        self.assertFalse(result.is_real_validation)

        broken_adapter = _BrokenValidator()
        broken = SFMValidationService(
            broken_adapter,
            trusted_validator_ids={broken_adapter.validator_id},
        )
        failed = await broken.validate("source", target=TARGET)
        self.assertEqual(failed.status, ValidationStatus.ERROR)
        self.assertNotIn("private detail", failed.message or "")


class _HangingValidator:
    validator_id = "test-hanging-validator"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def validate(self, source, target, *, timeout_seconds):
        del source, timeout_seconds
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _UntrustedValidValidator:
    validator_id = "unverified-parser"

    def __init__(self) -> None:
        self.called = False

    async def validate(self, source, target, *, timeout_seconds):
        del source, timeout_seconds
        self.called = True
        return ValidationResult(
            status=ValidationStatus.VALID,
            target=target,
            validator_id=self.validator_id,
        )


class _MismatchedValidator:
    validator_id = "test-validator"

    async def validate(self, source, target, *, timeout_seconds):
        del source, timeout_seconds
        return ValidationResult(
            status=ValidationStatus.VALID,
            target=SFMTargetVersion("different-sfm", "different-mc"),
            validator_id=self.validator_id,
        )


class _BrokenValidator:
    validator_id = "test-broken-validator"

    async def validate(self, source, target, *, timeout_seconds):
        del source, target, timeout_seconds
        raise RuntimeError("private detail that may contain source code")


if __name__ == "__main__":
    unittest.main()
