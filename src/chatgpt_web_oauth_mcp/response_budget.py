"""Shared token-aware response budget primitives."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from typing import Any

import tiktoken


DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET = 8500
TOKEN_ENCODING_NAME = "o200k_base"


def validate_token_budget(value: object, *, name: str) -> int:
    """Parse and validate a positive integer token budget."""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer; got {value!r}.") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer; got {value!r}.")
    return parsed


def resolve_token_budget(
    global_budget: object = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    tool_budget: object | None = None,
    *,
    global_name: str = "global token budget",
    tool_name: str = "tool token budget",
) -> int:
    """Return a validated tool override, or inherit the validated global value."""
    validated_global = validate_token_budget(global_budget, name=global_name)
    if tool_budget is None:
        return validated_global
    return validate_token_budget(tool_budget, name=tool_name)


@lru_cache(maxsize=1)
def _token_encoding():
    return tiktoken.get_encoding(TOKEN_ENCODING_NAME)


@dataclass(frozen=True)
class BudgetMeasurement:
    """Byte and token measurements for one prospective response body."""

    rendered_bytes: int
    estimated_tokens: int
    exceeds_byte_budget: bool
    exceeds_token_budget: bool

    @property
    def fits(self) -> bool:
        return not self.exceeds_byte_budget and not self.exceeds_token_budget

    @property
    def stop_reason(self) -> str | None:
        if self.exceeds_byte_budget and self.exceeds_token_budget:
            return "byte_and_token_budget"
        if self.exceeds_byte_budget:
            return "byte_budget"
        if self.exceeds_token_budget:
            return "token_budget"
        return None


@dataclass(frozen=True)
class ResponseBudget:
    """Measure rendered response content against byte and o200k token ceilings."""

    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_tokens",
            validate_token_budget(self.max_tokens, name="max_tokens"),
        )
        if self.max_bytes is not None:
            try:
                max_bytes = int(self.max_bytes)
            except (TypeError, ValueError):
                raise ValueError(
                    f"max_bytes must be a positive integer; got {self.max_bytes!r}."
                ) from None
            if max_bytes <= 0:
                raise ValueError(
                    f"max_bytes must be a positive integer; got {self.max_bytes!r}."
                )
            object.__setattr__(self, "max_bytes", max_bytes)

    @property
    def encoding_name(self) -> str:
        return TOKEN_ENCODING_NAME

    def count_tokens(self, text: str) -> int:
        return len(_token_encoding().encode(text))

    def measure(self, text: str) -> BudgetMeasurement:
        rendered_bytes = len(text.encode("utf-8"))
        estimated_tokens = self.count_tokens(text)
        return BudgetMeasurement(
            rendered_bytes=rendered_bytes,
            estimated_tokens=estimated_tokens,
            exceeds_byte_budget=(
                self.max_bytes is not None and rendered_bytes > self.max_bytes
            ),
            exceeds_token_budget=estimated_tokens > self.max_tokens,
        )


def render_json_payload(payload: object) -> str:
    """Render a tool payload exactly once for deterministic budget estimates."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def measure_json_payload(
    payload: object,
    budget: ResponseBudget,
) -> BudgetMeasurement:
    """Measure a JSON-compatible payload with the shared o200k budget."""
    return budget.measure(render_json_payload(payload))


def with_budget_metadata(
    payload: dict[str, Any],
    *,
    budget: ResponseBudget,
    truncated: bool,
    stop_reason: str,
) -> tuple[dict[str, Any], BudgetMeasurement]:
    """Attach the common response contract and return its final measurement.

    ``estimated_tokens`` is part of the measured response, so the value is
    iterated to a fixed point. In practice this converges after one update; the
    bounded loop protects against a token-count digit boundary.
    """
    result = dict(payload)
    result.pop("budget_exceeded", None)
    result["complete"] = not truncated
    result["partial"] = truncated
    result["truncated"] = truncated
    result["stop_reason"] = stop_reason
    result["estimated_tokens"] = 0
    result["effective_budget"] = {
        "tokens": budget.max_tokens,
        "bytes": budget.max_bytes,
        "token_encoding": budget.encoding_name,
    }

    measurement = measure_json_payload(result, budget)
    for _ in range(5):
        if result["estimated_tokens"] == measurement.estimated_tokens:
            break
        result["estimated_tokens"] = measurement.estimated_tokens
        measurement = measure_json_payload(result, budget)
    if not measurement.fits:
        # Some caller-supplied budgets are smaller than the irreducible JSON
        # envelope (paths, error fields, and this contract). No serializer can
        # make such a response fit, so report the condition honestly instead of
        # claiming a complete response.
        result["complete"] = False
        result["partial"] = True
        result["truncated"] = True
        if not truncated:
            result["stop_reason"] = "response_metadata_exceeds_budget"
        result["budget_exceeded"] = {
            "tokens": measurement.exceeds_token_budget,
            "bytes": measurement.exceeds_byte_budget,
        }
        for _ in range(5):
            measurement = measure_json_payload(result, budget)
            if result["estimated_tokens"] == measurement.estimated_tokens:
                break
            result["estimated_tokens"] = measurement.estimated_tokens
    return result, measurement
