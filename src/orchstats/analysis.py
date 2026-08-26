"""Pure-rule run summaries for the safe orchestration trace model.

This module intentionally performs no model calls, network access, or source
session reads.  It consumes only ``RunTrace`` objects produced by the parser
or constructed from the same safe dataclasses in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .diagnostics import Diagnostic, lint_run
from .models import AgentTrace, EvidenceLevel, RateLimitSnapshot, RunTrace, TokenUsage


@dataclass
class AgentSummary:
    label: str
    role: Optional[str]
    model: Optional[str]
    effort: Optional[str]
    fork_mode: str
    token_usage: TokenUsage
    tool_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": _safe_label(self.label),
            "role": _safe_optional_text(self.role),
            "model": _safe_optional_text(self.model),
            "effort": _safe_optional_text(self.effort),
            "fork_mode": _safe_optional_text(self.fork_mode),
            "token_usage": _coerce_token_usage(self.token_usage).to_dict(),
            "tool_count": max(0, _safe_int(self.tool_count, 0)),
        }


@dataclass
class QuotaSummary:
    plan_type: Optional[str] = None
    current_used_percent: Optional[float] = None
    observed_delta_percent: Optional[float] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[Any] = None
    has_credits: Optional[bool] = None
    observed_at: Optional[Any] = None
    evidence_level: str = EvidenceLevel.UNAVAILABLE.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_type": _safe_optional_text(self.plan_type),
            "current_used_percent": _safe_float_or_none(self.current_used_percent),
            "observed_delta_percent": _safe_float_or_none(self.observed_delta_percent),
            "window_minutes": _safe_nonnegative_int_or_none(self.window_minutes),
            "resets_at": _safe_timestamp_output(self.resets_at),
            "has_credits": self.has_credits if isinstance(self.has_credits, bool) else None,
            "observed_at": _safe_timestamp_output(self.observed_at),
            "evidence_level": _safe_optional_text(self.evidence_level),
        }


@dataclass
class RunAnalysis:
    schema_version: str = "0.1"
    agents: List[AgentSummary] = field(default_factory=list)
    total_token_usage: TokenUsage = field(default_factory=TokenUsage)
    quota: QuotaSummary = field(default_factory=QuotaSummary)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": _safe_optional_text(self.schema_version) or "0.1",
            "agents": [agent.to_dict() for agent in self.agents],
            "total_token_usage": _coerce_token_usage(self.total_token_usage).to_dict(),
            "quota": self.quota.to_dict(),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "limitations": [_safe_optional_text(item) for item in self.limitations],
        }


_BASE_LIMITATIONS: Tuple[str, ...] = (
    "semantic duplicate work unavailable",
    "precise fixed context tax unavailable",
    "quota delta account-level/shared and not per-agent billing",
)


def analyze_run(run: RunTrace) -> RunAnalysis:
    """Build a deterministic, privacy-safe summary from one ``RunTrace``."""

    traces = _ordered_traces(run)
    label_map: Dict[Any, str] = {}
    labels: Dict[int, str] = {}
    for index, trace in enumerate(traces, 1):
        label = "agent-%03d" % index
        labels[id(trace)] = label
        # Session ids are used only as an internal lookup convenience for
        # diagnostics.  They never enter an output dataclass or message.
        label_map[id(trace)] = label
        session_id = getattr(trace, "session_id", None)
        if session_id and session_id not in label_map:
            label_map[session_id] = label

    summaries: List[AgentSummary] = []
    total = TokenUsage()
    for trace in traces:
        usage = _coerce_token_usage(getattr(trace, "token_usage", None))
        total = total + usage
        spans = getattr(trace, "tool_spans", ()) or ()
        try:
            tool_count = len(spans)
        except TypeError:
            tool_count = 0
        summaries.append(
            AgentSummary(
                label=labels[id(trace)],
                role=_safe_optional_text(getattr(trace, "role", None)),
                model=_safe_optional_text(getattr(trace, "model", None)),
                effort=_safe_optional_text(getattr(trace, "effort", None)),
                fork_mode=_safe_optional_text(getattr(trace, "fork_mode", "unknown")) or "unknown",
                token_usage=usage,
                tool_count=max(0, tool_count),
            )
        )

    quota, quota_limitations = _summarize_quota(traces)
    diagnostics = lint_run(run, label_map)
    limitations = list(_BASE_LIMITATIONS)
    limitations.extend(quota_limitations)

    return RunAnalysis(
        schema_version="0.1",
        agents=summaries,
        total_token_usage=total,
        quota=quota,
        diagnostics=diagnostics,
        limitations=_stable_unique(limitations),
    )


def _ordered_traces(run: RunTrace) -> List[AgentTrace]:
    """Return root, direct-children DFS, then stable remaining sessions."""

    root = getattr(run, "root", None)
    result: List[AgentTrace] = []
    visited: Set[int] = set()
    visited_session_ids: Set[str] = set()

    def add(trace: Any) -> bool:
        if not isinstance(trace, AgentTrace):
            return False
        marker = id(trace)
        session_id = getattr(trace, "session_id", None)
        if marker in visited or (session_id and str(session_id) in visited_session_ids):
            return False
        visited.add(marker)
        if session_id:
            visited_session_ids.add(str(session_id))
        result.append(trace)
        return True

    def visit(trace: AgentTrace) -> None:
        if not add(trace):
            return
        for child in _direct_children(run, trace):
            visit(child)

    if isinstance(root, AgentTrace):
        visit(root)

    sessions = getattr(run, "sessions", {}) or {}
    entries = list(sessions.items()) if isinstance(sessions, Mapping) else []
    entries.sort(
        key=lambda item: (
            str(getattr(item[1], "session_id", "") or ""),
            str(item[0]),
        )
    )
    for _, trace in entries:
        visit(trace)
    return result


def _direct_children(run: RunTrace, parent: AgentTrace) -> List[AgentTrace]:
    children = getattr(run, "children", {}) or {}
    candidates: Any = ()
    if isinstance(children, Mapping):
        candidates = children.get(getattr(parent, "session_id", None), ())
        if not candidates:
            candidates = children.get(id(parent), ())
    if not isinstance(candidates, (list, tuple)):
        candidates = tuple(candidates or ()) if isinstance(candidates, Iterable) else ()
    return [candidate for candidate in candidates if isinstance(candidate, AgentTrace)]


def _summarize_quota(
    traces: Sequence[AgentTrace],
) -> Tuple[QuotaSummary, List[str]]:
    """Select comparable codex/primary snapshots and derive an observed delta."""

    # Each item is (canonical window key, snapshot, trace order, endpoint
    # order).  Only first/last maps are considered; parser internals may have
    # more snapshots but they are intentionally outside this model boundary.
    candidates: List[Tuple[str, RateLimitSnapshot, int, int]] = []
    for trace_index, trace in enumerate(traces):
        first = getattr(trace, "rate_limits_first", None)
        last = getattr(trace, "rate_limits_last", None)
        if not isinstance(first, Mapping):
            first = {}
        if not isinstance(last, Mapping):
            last = {}
        for endpoint, rates in ((0, first), (1, last)):
            for key, snapshot in sorted(rates.items(), key=lambda item: str(item[0])):
                if not isinstance(snapshot, RateLimitSnapshot):
                    continue
                canonical = _quota_key(key)
                if canonical is None:
                    continue
                candidates.append((canonical, snapshot, trace_index, endpoint))

    limitations: List[str] = []
    if not candidates:
        limitations.append("quota observations unavailable")
        return QuotaSummary(), limitations

    # Prefer a window with the greatest number of observations, then the
    # canonical key's lexical order.  This keeps mixed synthetic fixtures
    # stable while retaining a single comparable quota stream.
    by_key: Dict[str, List[Tuple[RateLimitSnapshot, int, int]]] = {}
    for key, snapshot, trace_index, endpoint in candidates:
        by_key.setdefault(key, []).append((snapshot, trace_index, endpoint))
    key = sorted(by_key, key=lambda value: (-len(by_key[value]), value))[0]
    stream = by_key[key]

    timed = [
        item
        for item in stream
        if _timestamp_number(getattr(item[0], "observed_at", None)) is not None
    ]
    timed.sort(
        key=lambda item: (
            _timestamp_number(getattr(item[0], "observed_at", None)),
            item[1],
            item[2],
        )
    )

    if timed and len(timed) < len(stream):
        limitations.append("quota snapshot timestamps unavailable; endpoint order is a stable fallback")

    if timed:
        earliest = timed[0][0]
        latest = timed[-1][0]
    else:
        # Without timestamps there is no defensible chronology.  We still
        # return the latest stable input endpoint so current metadata remains
        # useful, while explicitly limiting the delta claim below.
        # The same limitation is useful for the no-timestamp case, but avoid
        # adding it twice when the branch is reached after a partial stream.
        if "quota snapshot timestamps unavailable; endpoint order is a stable fallback" not in limitations:
            limitations.append("quota snapshot timestamps unavailable; endpoint order is a stable fallback")
        stream_sorted = sorted(stream, key=lambda item: (item[1], item[2]))
        earliest = stream_sorted[0][0]
        latest = stream_sorted[-1][0]

    # A delta is derived only from two numerically comparable, chronologically
    # observed endpoints.  Negative values are intentionally retained as an
    # observed reset signal; no token-to-quota conversion is attempted.
    delta: Optional[float] = None
    if len(timed) >= 2:
        earliest_value = _safe_float_or_none(getattr(earliest, "used_percent", None))
        latest_value = _safe_float_or_none(getattr(latest, "used_percent", None))
        if earliest_value is not None and latest_value is not None:
            delta = latest_value - earliest_value
    elif len(stream) >= 2:
        limitations.append("observed quota delta unavailable without comparable timestamps")

    current = _safe_float_or_none(getattr(latest, "used_percent", None))
    evidence = EvidenceLevel.UNAVAILABLE.value
    if current is not None:
        evidence = EvidenceLevel.DERIVED.value if delta is not None else EvidenceLevel.OBSERVED.value

    return (
        QuotaSummary(
            plan_type=_safe_optional_text(getattr(latest, "plan_type", None)),
            current_used_percent=current,
            observed_delta_percent=delta,
            window_minutes=_safe_nonnegative_int_or_none(getattr(latest, "window_minutes", None)),
            resets_at=_safe_timestamp_output(getattr(latest, "resets_at", None)),
            has_credits=(
                getattr(latest, "has_credits", None)
                if isinstance(getattr(latest, "has_credits", None), bool)
                else None
            ),
            observed_at=_safe_timestamp_output(getattr(latest, "observed_at", None)),
            evidence_level=evidence,
        ),
        limitations,
    )


def _quota_key(value: Any) -> Optional[str]:
    text = _safe_optional_text(value)
    if not text:
        return None
    lowered = text.lower().replace("_", "-")
    # Codex exporters have used primary, codex, limit-primary, and compact
    # aliases.  Other named windows are retained only when they are plainly a
    # primary/codex window; this prevents unrelated limits being compared.
    if lowered in {"primary", "codex", "codex-primary", "primary-codex"}:
        return "primary"
    if "primary" in lowered or "codex" in lowered:
        # ``limit-primary`` and similar exporter labels are aliases for the
        # same comparable primary window; do not split the observed stream by
        # cosmetic key naming.
        return "primary"
    return None


def _coerce_token_usage(value: Any) -> TokenUsage:
    if isinstance(value, TokenUsage):
        return TokenUsage(**value.to_dict())
    fields = {}
    for name in TokenUsage._FIELDS:
        if isinstance(value, Mapping):
            fields[name] = value.get(name, 0)
        else:
            fields[name] = getattr(value, name, 0)
    return TokenUsage(**fields)


def _timestamp_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _safe_timestamp_output(value: Any) -> Optional[Any]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    text = value[:128]
    # Timestamp fields are safe to expose only when they parse as numeric or
    # ISO timestamps; arbitrary strings could otherwise smuggle paths or ids.
    if _timestamp_number(text) is None:
        return None
    return text


def _safe_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value[:512]
    else:
        text = str(value)[:512]
    if text.startswith("/") or text.startswith("~"):
        return "<redacted>"
    return text


def _safe_label(value: Any) -> str:
    text = _safe_optional_text(value) or ""
    return text if text.startswith("agent-") else "agent-unknown"


def _safe_float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stable_unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = _safe_optional_text(value) or ""
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


__all__ = [
    "AgentSummary",
    "QuotaSummary",
    "RunAnalysis",
    "analyze_run",
]
