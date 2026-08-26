"""Public-summary export helpers for ``orchstats sanitize``."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .analysis import RunAnalysis
from .models import _redact_path_tokens, normalize_fork_turns
from .pricing import pricing_catalog
from .reporting import public_analysis_dict


PRIVACY_PROFILE = "public-summary-v1"

# These are the only role labels that are owned by this repository.  A role
# supplied by a trace is never copied into a public export merely because it
# happens to be a string.
_BUILTIN_ROLES = frozenset(
    {
        "root",
        "worker",
        "reviewer",
        "explorer",
        "fixer",
        "sites_deployer",
        "default",
        "unassigned",
    }
)
_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "max", "xhigh", "ultra", "unknown"}
)
_FIXED_FORKS = frozenset({"none", "all", "unknown"})
_AGENT_LABEL = re.compile(r"^agent-[0-9]{3}$")

_CATALOG = pricing_catalog()
_CATALOG_MODELS = frozenset(
    name
    for name in (_CATALOG.get("models", {}) or {})
    if isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name)
)
_CATALOG_ALIASES = {
    alias: target
    for alias, target in (_CATALOG.get("aliases", {}) or {}).items()
    if isinstance(alias, str) and isinstance(target, str) and target in _CATALOG_MODELS
}

_DIAGNOSTIC_MESSAGES = {
    "CTX_FULL_FORK": "child agent uses full context fork",
    "CTX_FORK_UNKNOWN": "child fork mode is unknown",
    "HIGH_TIER_WORKER": "worker or fixer uses a high-tier model",
    "REPEATED_TOOL_CALL": "same non-validation tool command observed across agents",
    "REPEATED_VALIDATION": "same successful validation repeated without an intervening write",
    "PARENT_CHILD_OVERLAP": "parent and child tool spans overlap on a shared resource or command",
    "REVIEW_CHAIN": "reviewer to worker/fixer to reviewer validation chain observed",
}
_DIAGNOSTIC_SEVERITIES = frozenset({"INFO", "MEDIUM", "HIGH"})
_DIAGNOSTIC_EVIDENCE = frozenset({"observed", "derived", "heuristic", "unavailable"})
_DIAGNOSTIC_DEFAULT_SEVERITY = {
    "CTX_FULL_FORK": "HIGH",
    "HIGH_TIER_WORKER": "MEDIUM",
}
_DIAGNOSTIC_DEFAULT_EVIDENCE = {
    "CTX_FULL_FORK": "observed",
}

# Internal analysis limitations are deliberately mapped to a smaller public
# vocabulary.  In particular, the public summary has no quota field and must
# not smuggle quota values or arbitrary text through this string array.
_PUBLIC_LIMITATIONS = {
    "semantic duplicate work unavailable": "semantic duplicate work unavailable",
    "precise fixed context tax unavailable": "precise fixed context tax unavailable",
    "quota delta account-level/shared and not per-agent billing": "delta account-level/shared and not per-agent billing",
    "quota observations unavailable": "observations unavailable",
    "quota snapshot timestamps unavailable; endpoint order is a stable fallback": "snapshot timestamps unavailable; endpoint order is a stable fallback",
    "observed quota delta unavailable without comparable timestamps": "observed delta unavailable without comparable timestamps",
}


class SanitizeError(ValueError):
    """Raised when a sanitized output target is unsafe or unavailable."""


def sanitized_dict(analysis: RunAnalysis) -> Dict[str, Any]:
    """Build the intentionally small public-summary allowlist.

    Quota data and all event-level metadata are excluded.  Per-agent trace
    labels are omitted because array order is enough for this compact public
    summary; diagnostics retain only generated labels needed to explain a
    finding, and the agent allowlist stays limited to role/model/effort/
    fork/token/tool_count.
    """

    public = public_analysis_dict(analysis)
    agents = []
    for item in public.get("agents", ()) or ():
        if not isinstance(item, Mapping):
            continue
        agents.append(
            {
                "role": _canonical_role(item.get("role")),
                "model": _canonical_model(item.get("model")),
                "effort": _canonical_effort(item.get("effort")),
                "fork": _canonical_fork(item.get("fork_mode")),
                "token": _public_token(item.get("token_usage")),
                "tool_count": _nonnegative_int(item.get("tool_count")),
            }
        )
    diagnostics = []
    for item in public.get("diagnostics", ()) or ():
        if not isinstance(item, Mapping):
            continue
        code = _canonical_diagnostic_code(item.get("code"))
        if code is None:
            continue
        severity = _canonical_diagnostic_severity(code, item.get("severity"))
        evidence_level = _canonical_diagnostic_evidence(code, item.get("evidence_level"))
        labels = _public_agent_labels(item.get("agent_labels"))
        diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "evidence_level": evidence_level,
                # Always use a repository-owned template.  The source
                # diagnostic message is never trusted or interpolated.
                "message": _DIAGNOSTIC_MESSAGES[code],
                "agent_labels": labels,
                "count": _nonnegative_int(item.get("count")),
            }
        )
    limitations = []
    for item in public.get("limitations", ()) or ():
        limitation = _public_limitation(item)
        if limitation is not None and limitation not in limitations:
            limitations.append(limitation)
    return {
        "privacy_profile": PRIVACY_PROFILE,
        "schema_version": "0.1",
        "agents": agents,
        "diagnostics": diagnostics,
        "limitations": limitations,
    }


def write_sanitized(
    analysis: RunAnalysis,
    output: Any,
    *,
    input_root: Optional[Any] = None,
) -> Path:
    """Write a sanitized JSON export without overwriting any existing file."""

    target = Path(output)
    target_resolved = target.resolve()
    if input_root is not None:
        source = Path(input_root).resolve()
        if target_resolved == source:
            raise SanitizeError("output target must differ from input")
    if target.exists():
        raise SanitizeError("output target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)

    import json

    payload = sanitized_dict(analysis)
    try:
        with target.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError:
        raise SanitizeError("output target already exists")
    return target


def _public_token(value: Any) -> Dict[str, int]:
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {name: _nonnegative_int(value.get(name, 0) if isinstance(value, Mapping) else 0) for name in names}


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
        if not math.isfinite(number):
            return 0
        return min(9_007_199_254_740_991, max(0, int(number)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _public_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = _redact_path_tokens(str(value).strip())
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return text[:128] or None


def _public_limitation(value: Any) -> Optional[str]:
    """Return only one of the repository-owned public limitation templates."""

    text = _public_text(value)
    if text is None:
        return None
    return _PUBLIC_LIMITATIONS.get(text.casefold())


def _canonical_role(value: Any) -> Optional[str]:
    text = _public_text(value)
    if text is None:
        return None
    lowered = text.casefold()
    return lowered if lowered in _BUILTIN_ROLES else "custom"


def _canonical_model(value: Any) -> Optional[str]:
    text = _public_text(value)
    if text is None:
        return None
    if text in _CATALOG_MODELS:
        return text
    if text in _CATALOG_ALIASES:
        return _CATALOG_ALIASES[text]
    return "unknown-model"


def _canonical_effort(value: Any) -> Optional[str]:
    text = _public_text(value)
    if text is None:
        return None
    lowered = text.casefold()
    return lowered if lowered in _EFFORTS else "unknown"


def _canonical_fork(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = normalize_fork_turns(value)
    if normalized in _FIXED_FORKS or re.fullmatch(r"recent_[0-9]{1,6}", normalized):
        return normalized
    return "unknown"


def _canonical_diagnostic_code(value: Any) -> Optional[str]:
    text = _public_text(value)
    if text is None:
        return None
    upper = text.upper()
    return upper if upper in _DIAGNOSTIC_MESSAGES else None


def _canonical_diagnostic_severity(code: str, value: Any) -> str:
    text = _public_text(value)
    upper = text.upper() if text else ""
    if upper in _DIAGNOSTIC_SEVERITIES:
        return upper
    return _DIAGNOSTIC_DEFAULT_SEVERITY.get(code, "INFO")


def _canonical_diagnostic_evidence(code: str, value: Any) -> str:
    text = _public_text(value)
    lowered = text.casefold() if text else ""
    if lowered in _DIAGNOSTIC_EVIDENCE:
        return lowered
    return _DIAGNOSTIC_DEFAULT_EVIDENCE.get(code, "unavailable")


def _public_agent_labels(value: Any) -> list:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for item in value:
        text = _public_text(item)
        if text is not None and _AGENT_LABEL.fullmatch(text):
            if text not in result:
                result.append(text)
    return result


__all__ = ["PRIVACY_PROFILE", "SanitizeError", "sanitized_dict", "write_sanitized"]
