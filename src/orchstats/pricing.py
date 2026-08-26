"""Static API-equivalent pricing estimates for the local dashboard.

The catalog is intentionally small and explicit: it records the OpenAI
Standard, short-context rates that are safe to use for a local heuristic
estimate.  This module does not make network requests and does not claim to
reconstruct ChatGPT or Codex subscription billing.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Dict, Mapping, Optional


PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_AS_OF = "2026-08-23"
PRICING_UNIT = "USD per 1M tokens"
PRICING_MODE = "standard"
PRICING_CONTEXT = "short"

_RATE_FIELDS = ("input", "cached_input", "cache_write", "output")
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


# Prices are dollars per one million tokens.  Keep this table as plain
# literals so the public copy is JSON-safe without a custom encoder.
_MODELS: Dict[str, Dict[str, float]] = {
    "gpt-5.6-sol": {
        "input": 4.0,
        "cached_input": 0.4,
        "cache_write": 5.0,
        "output": 20.0,
    },
    "gpt-5.6-terra": {
        "input": 2.0,
        "cached_input": 0.2,
        "cache_write": 2.5,
        "output": 12.0,
    },
    "gpt-5.6-luna": {
        "input": 0.2,
        "cached_input": 0.02,
        "cache_write": 0.25,
        "output": 1.2,
    },
}

_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


def pricing_catalog() -> Dict[str, Any]:
    """Return the immutable-in-practice, JSON-safe pricing snapshot.

    A fresh deep copy is returned so a caller cannot alter later estimates by
    mutating the dictionary it received.
    """

    return deepcopy(
        {
            "schema_version": "pricing-catalog-0.1",
            "as_of": PRICING_AS_OF,
            "source": PRICING_SOURCE,
            "currency": "USD",
            "unit": PRICING_UNIT,
            "billing_mode": PRICING_MODE,
            "context": PRICING_CONTEXT,
            "models": _MODELS,
            "aliases": _ALIASES,
        }
    )


def estimate_api_equivalent(model: Any, token_usage: Any) -> Dict[str, Any]:
    """Estimate API-equivalent token cost for one safe token projection.

    ``token_usage`` may be a mapping (for example a serialized
    ``TokenUsage``), or any object exposing the five token fields by
    attribute.  Input counters are read only; this function never mutates
    the supplied object or mapping.

    ``input_tokens`` is treated as the combined input counter.  The billable
    input partition is ``max(input - cached - cache_write, 0)`` plus cached
    and cache-write tokens. ``reasoning_output_tokens`` remains available as
    an informational subset of ``output_tokens`` and is never charged a
    second time.
    """

    counts = _token_counts(token_usage)
    breakdown = _billable_breakdown(counts)
    total_tokens = (
        breakdown["uncached_input_tokens"]
        + breakdown["cached_input_tokens"]
        + breakdown["cache_write_input_tokens"]
        + breakdown["output_tokens"]
    )
    model_name = _model_name(model)
    canonical_model = _ALIASES.get(model_name, model_name) if model_name else None
    rates = _MODELS.get(canonical_model) if canonical_model else None

    if not _has_complete_rates(rates):
        return {
            "model": model_name,
            "canonical_model": None,
            "total_usd": None,
            "priced_tokens": 0,
            "unpriced_tokens": total_tokens,
            "rate_available": False,
            "evidence_level": "unavailable",
            "basis": (
                "No official Standard short-context rate is recorded for this "
                "model in the pricing snapshot as of "
                + PRICING_AS_OF
                + "; API-equivalent cost is unavailable."
            ),
            "token_breakdown": breakdown,
        }

    # Every component is present for the currently published catalog entries,
    # but calculate by named component so a future partial entry cannot cause
    # an unpriced component to be silently treated as free.
    assert rates is not None
    cost = (
        breakdown["uncached_input_tokens"] * rates["input"]
        + breakdown["cached_input_tokens"] * rates["cached_input"]
        + breakdown["cache_write_input_tokens"] * rates["cache_write"]
        + breakdown["output_tokens"] * rates["output"]
    ) / 1_000_000.0
    return {
        "model": model_name,
        "canonical_model": canonical_model,
        "total_usd": cost,
        "priced_tokens": total_tokens,
        "unpriced_tokens": 0,
        "rate_available": True,
        "evidence_level": "heuristic",
        "basis": (
            "OpenAI Standard short-context rates in USD per 1M tokens, "
            "snapshot as of "
            + PRICING_AS_OF
            + "; uncached=max(input-cached-cache_write, 0), cached and "
            "cache-write input use their respective rates, and output is "
            "charged once because reasoning tokens are already included in "
            "the output total."
        ),
        "token_breakdown": breakdown,
    }


def _model_name(model: Any) -> Optional[str]:
    if model is None:
        return None
    try:
        value = str(model).strip()
    except Exception:
        return None
    return value or None


def _token_counts(token_usage: Any) -> Dict[str, int]:
    """Read and sanitize token counters without writing to ``token_usage``."""

    source: Any = token_usage
    if isinstance(source, Mapping):
        nested = source.get("total_token_usage")
        if isinstance(nested, Mapping):
            source = nested

    result: Dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        if isinstance(source, Mapping):
            raw = source.get(field, 0)
        else:
            raw = getattr(source, field, 0) if source is not None else 0
        result[field] = _nonnegative_int(raw)
    return result


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not isfinite(number) or number <= 0:
        return 0
    return int(number)


def _billable_breakdown(counts: Mapping[str, int]) -> Dict[str, int]:
    cached = counts["cached_input_tokens"]
    cache_write = counts["cache_write_input_tokens"]
    uncached = max(counts["input_tokens"] - cached - cache_write, 0)
    return {
        "uncached_input_tokens": uncached,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": counts["output_tokens"],
        "reasoning_output_tokens": counts["reasoning_output_tokens"],
    }


def _has_complete_rates(rates: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(rates, Mapping):
        return False
    for field in _RATE_FIELDS:
        value = rates.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not isfinite(float(value)) or float(value) < 0:
            return False
    return True


__all__ = ["estimate_api_equivalent", "pricing_catalog"]
