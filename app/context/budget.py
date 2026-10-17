from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Literal


ContextBudgetMode = Literal["off", "shadow", "enforce"]
ProfileStatus = Literal["verified", "operator_declared"]


@dataclass(frozen=True, slots=True)
class ContextWindowProfile:
    provider: str
    model: str
    context_window_tokens: int
    status: ProfileStatus = "operator_declared"
    max_output_tokens: int | None = None
    preferred_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("context profile requires provider and model identifiers")
        if not 1024 <= self.context_window_tokens <= 10_000_000:
            raise ValueError("context window must be between 1024 and 10000000 tokens")
        if self.status not in {"verified", "operator_declared"}:
            raise ValueError("context profile status must be verified or operator_declared")
        for value in (self.max_output_tokens, self.preferred_output_tokens):
            if value is not None and not 1 <= value <= self.context_window_tokens:
                raise ValueError("profile output token limits must fit within the model window")
        if (
            self.max_output_tokens is not None
            and self.preferred_output_tokens is not None
            and self.preferred_output_tokens > self.max_output_tokens
        ):
            raise ValueError("preferred output tokens cannot exceed the profile output maximum")

    @property
    def key(self) -> str:
        return profile_key(self.provider, self.model)


def profile_key(provider: str, model: str) -> str:
    """Build an exact provider/model key; this intentionally never guesses by model name."""
    return f"{provider.strip().casefold()}:{model.strip().casefold()}"


@dataclass(frozen=True, slots=True)
class ContextBudgetDecision:
    mode: ContextBudgetMode
    profile_key: str
    profile_status: str
    estimated_input_tokens: int | None
    context_window_tokens: int | None
    safety_margin_tokens: int
    requested_output_tokens: int | None
    effective_output_tokens: int | None
    within_estimate: bool | None
    estimate_method: str = "utf8-json-bytes-x0.40-plus-message-overhead-v1"


class ContextWindowError(RuntimeError):
    """A local pre-dispatch context-window check rejected a request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ContextWindowBudget:
    """Conservative, tokenizer-free request-window estimate and dispatch gate."""

    def __init__(
        self,
        profiles: tuple[ContextWindowProfile, ...] | list[ContextWindowProfile],
        *,
        mode: ContextBudgetMode = "off",
        default_output_tokens: int = 2048,
        minimum_output_tokens: int = 128,
        safety_ratio: float = 0.10,
        safety_floor_tokens: int = 512,
        max_payload_bytes: int = 2_000_000,
    ) -> None:
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("context budget mode must be off, shadow, or enforce")
        if not 1 <= minimum_output_tokens <= default_output_tokens <= 1_000_000:
            raise ValueError("invalid context budget output token limits")
        if not 0.0 <= safety_ratio <= 0.5 or safety_floor_tokens < 0:
            raise ValueError("invalid context budget safety margin")
        if not 1024 <= max_payload_bytes <= 16_000_000:
            raise ValueError("context payload limit must be between 1 KiB and 16 MiB")
        self.mode = mode
        self.default_output_tokens = default_output_tokens
        self.minimum_output_tokens = minimum_output_tokens
        self.safety_ratio = safety_ratio
        self.safety_floor_tokens = safety_floor_tokens
        self.max_payload_bytes = max_payload_bytes
        self._profiles = {item.key: item for item in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate provider/model context profile")

    def has_profile(self, key: str | None) -> bool:
        return bool(key and key.casefold() in self._profiles)

    def get_profile(self, key: str | None) -> ContextWindowProfile | None:
        return self._profiles.get(key.casefold()) if key else None

    def profile_status(self, key: str | None) -> str:
        profile = self.get_profile(key)
        return profile.status if profile else "unknown"

    @staticmethod
    def _serialized_prompt(payload: dict[str, object]) -> str:
        prompt_payload = {key: value for key, value in payload.items() if key != "max_tokens"}
        try:
            return json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ContextWindowError("invalid_payload", "LLM request payload is not JSON serializable") from exc

    def estimate_input_tokens(self, payload: dict[str, object]) -> tuple[int, int]:
        serialized = self._serialized_prompt(payload)
        payload_bytes = len(serialized.encode("utf-8"))
        message_count = len(payload.get("messages", [])) if isinstance(payload.get("messages"), list) else 0
        # UTF-8 size is only a conservative proxy, not provider tokenizer accounting.
        estimated = max(32, math.ceil(payload_bytes * 0.40) + 8 * message_count + 32)
        return estimated, payload_bytes

    @classmethod
    def estimate_text_tokens(cls, text: str) -> int:
        """Estimate one user-text item's contribution with the same envelope heuristic."""
        payload = {"messages": [{"role": "user", "content": text}]}
        payload_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return max(32, math.ceil(payload_bytes * 0.40) + 8 + 32)

    def prepare_payload(
        self, payload: dict[str, object], key: str | None
    ) -> tuple[dict[str, object], ContextBudgetDecision]:
        if self.mode == "off":
            return payload, ContextBudgetDecision(
                mode="off", profile_key=key or "unknown", profile_status="unchecked",
                estimated_input_tokens=None, context_window_tokens=None,
                safety_margin_tokens=0, requested_output_tokens=None,
                effective_output_tokens=None, within_estimate=None,
            )

        estimated_input, payload_bytes = self.estimate_input_tokens(payload)
        if payload_bytes > self.max_payload_bytes and self.mode == "enforce":
            raise ContextWindowError("payload_too_large", "LLM request exceeds the configured payload byte limit")

        profile = self._profiles.get(key.casefold()) if key else None
        if profile is None:
            decision = ContextBudgetDecision(
                mode=self.mode, profile_key=key or "unknown", profile_status="unknown",
                estimated_input_tokens=estimated_input, context_window_tokens=None,
                safety_margin_tokens=0, requested_output_tokens=payload.get("max_tokens")
                if isinstance(payload.get("max_tokens"), int) else None,
                effective_output_tokens=None, within_estimate=None,
            )
            if self.mode == "enforce":
                raise ContextWindowError(
                    "profile_missing", f"No context-window profile is configured for {key or 'unknown model'}"
                )
            return payload, decision

        window = profile.context_window_tokens
        margin = max(self.safety_floor_tokens, math.ceil(window * self.safety_ratio))
        profile_default = profile.preferred_output_tokens or self.default_output_tokens
        if profile.max_output_tokens is not None:
            profile_default = min(profile_default, profile.max_output_tokens)
        raw_output = payload.get("max_tokens", profile_default)
        if isinstance(raw_output, bool) or not isinstance(raw_output, int) or raw_output < 1:
            raise ContextWindowError("invalid_output_budget", "LLM output token limit must be a positive integer")
        available_output = window - margin - estimated_input
        output_ceiling = profile.max_output_tokens or raw_output
        within = available_output >= raw_output and raw_output <= output_ceiling
        decision = ContextBudgetDecision(
            mode=self.mode, profile_key=profile.key, profile_status=profile.status,
            estimated_input_tokens=estimated_input, context_window_tokens=window,
            safety_margin_tokens=margin, requested_output_tokens=raw_output,
            effective_output_tokens=(min(raw_output, output_ceiling, available_output) if available_output > 0 else 0),
            within_estimate=within,
        )
        if self.mode == "shadow":
            return payload, decision
        if available_output < self.minimum_output_tokens:
            raise ContextWindowError(
                "context_too_large",
                "Estimated prompt exceeds the configured model context window; reduce context and retry",
            )
        effective_output = min(raw_output, output_ceiling, available_output)
        prepared = dict(payload)
        prepared["max_tokens"] = effective_output
        return prepared, ContextBudgetDecision(
            mode="enforce", profile_key=profile.key, profile_status=profile.status,
            estimated_input_tokens=estimated_input, context_window_tokens=window,
            safety_margin_tokens=margin, requested_output_tokens=raw_output,
            effective_output_tokens=effective_output,
            within_estimate=effective_output == raw_output,
        )
