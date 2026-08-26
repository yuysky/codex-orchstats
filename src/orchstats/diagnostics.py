"""Deterministic, privacy-safe diagnostics for an orchestration run.

The diagnostics layer deliberately works only with the safe projection in
``orchstats.models``.  It never needs a session id, call id, command text, or
path; callers provide a label map so findings can refer to generated agent
labels instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .models import AgentTrace, RunTrace, ToolSpan


@dataclass
class Diagnostic:
    """One concise finding produced by :func:`lint_run`.

    ``agent_labels`` contains only generated labels (for example,
    ``agent-001``); no source/session identifiers are accepted by the
    analyzer.  ``count`` is the number of deterministic findings represented
    by this row.
    """

    code: str
    severity: str
    evidence_level: str
    message: str
    agent_labels: List[str] = field(default_factory=list)
    count: int = 1

    def __post_init__(self) -> None:
        self.code = _safe_text(self.code)
        self.severity = _safe_text(self.severity)
        self.evidence_level = _safe_text(self.evidence_level)
        self.message = _safe_text(self.message)
        if not isinstance(self.agent_labels, list):
            self.agent_labels = list(self.agent_labels or ())
        self.agent_labels = [_safe_text(label) for label in self.agent_labels]
        try:
            self.count = max(0, int(self.count))
        except (TypeError, ValueError):
            self.count = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "evidence_level": self.evidence_level,
            "message": self.message,
            "agent_labels": list(self.agent_labels),
            "count": self.count,
        }


@dataclass
class _Finding:
    """Internal aggregate for one diagnostic code."""

    code: str
    severity: str
    evidence_level: str
    message: str
    labels: Set[str] = field(default_factory=set)
    count: int = 0


@dataclass
class _TimedSpan:
    span: ToolSpan
    start: float
    end: float
    label: str
    ordinal: int


_RULE_ORDER: Tuple[str, ...] = (
    "CTX_FULL_FORK",
    "CTX_FORK_UNKNOWN",
    "HIGH_TIER_WORKER",
    "REPEATED_TOOL_CALL",
    "REPEATED_VALIDATION",
    "PARENT_CHILD_OVERLAP",
    "REVIEW_CHAIN",
)

_MESSAGES: Dict[str, str] = {
    "CTX_FULL_FORK": "child agent uses full context fork",
    "CTX_FORK_UNKNOWN": "child fork mode is unknown",
    "HIGH_TIER_WORKER": "worker or fixer uses a high-tier model",
    "REPEATED_TOOL_CALL": "same non-validation tool command observed across agents",
    "REPEATED_VALIDATION": "same successful validation repeated without an intervening write",
    "PARENT_CHILD_OVERLAP": "parent and child tool spans overlap on a shared resource or command",
    "REVIEW_CHAIN": "reviewer to worker/fixer to reviewer validation chain observed",
}


def lint_run(run: RunTrace, label_map: Mapping[Any, str]) -> List[Diagnostic]:
    """Return deterministic diagnostics for ``run``.

    ``label_map`` is normally produced by ``analysis.analyze_run`` and maps
    trace identities (and, for convenience, safe session keys) to generated
    labels.  The implementation also accepts a map keyed by ``id(trace)`` so
    direct callers can construct synthetic ``RunTrace`` objects without
    making ``AgentTrace`` hashable.
    """

    traces = _ordered_traces(run)
    findings: Dict[str, _Finding] = {}

    def add(code: str, labels: Iterable[str], count: int = 1, *, severity: Optional[str] = None,
            evidence_level: Optional[str] = None) -> None:
        labels_set = {label for label in labels if label}
        if code not in findings:
            findings[code] = _Finding(
                code=code,
                severity=severity or _default_severity(code),
                evidence_level=evidence_level or _default_evidence(code),
                message=_MESSAGES[code],
            )
        finding = findings[code]
        # Keep the strongest severity if a code has both resource and command
        # overlap findings.  This intentionally merges those into one row.
        if _severity_rank(severity or finding.severity) > _severity_rank(finding.severity):
            finding.severity = severity or finding.severity
        if evidence_level == "observed":
            finding.evidence_level = "observed"
        elif evidence_level == "derived" and finding.evidence_level == "heuristic":
            finding.evidence_level = "derived"
        finding.labels.update(labels_set)
        finding.count += max(1, count)

    # Rule 1: direct children whose context fork mode is explicit/full or
    # unavailable.  Use the actual children map rather than inferred order so
    # grandchildren do not get reported as direct children.
    for parent in traces:
        for child in _direct_children(run, parent):
            label = _label_for(child, label_map)
            mode = _fork_mode(child)
            if mode == "all":
                add("CTX_FULL_FORK", (label,))
            elif mode == "unknown":
                add("CTX_FORK_UNKNOWN", (label,))

    # Rule 2: a worker/fixer assigned a model whose name contains ``sol``.
    for trace in traces:
        role = _lower(getattr(trace, "role", None))
        model = _lower(getattr(trace, "model", None))
        if role in {"worker", "fixer"} and "sol" in model:
            add("HIGH_TIER_WORKER", (_label_for(trace, label_map),), severity="MEDIUM")

    # Rule 3: repeated non-validation commands across distinct agents.
    command_groups: Dict[str, Dict[int, str]] = {}
    for trace in traces:
        trace_key = id(trace)
        label = _label_for(trace, label_map)
        for span in _spans(trace):
            fingerprint = getattr(span, "command_fingerprint", None)
            if not fingerprint or _lower(getattr(span, "command_category", None)) == "validation":
                continue
            command_groups.setdefault(str(fingerprint), {})[trace_key] = label
    for labels_by_trace in command_groups.values():
        labels = set(labels_by_trace.values())
        if len(labels) >= 2:
            add("REPEATED_TOOL_CALL", labels)

    timed_by_trace = _timed_spans_by_trace(traces, label_map)
    all_timed = [item for values in timed_by_trace.values() for item in values]

    # Rule 4: repeated successful validations, only where the endpoint times
    # and all potentially-intervening writes are comparable.
    validation_groups: Dict[str, List[_TimedSpan]] = {}
    for item in all_timed:
        span = item.span
        fingerprint = getattr(span, "command_fingerprint", None)
        if not fingerprint or _lower(getattr(span, "command_category", None)) != "validation":
            continue
        if not _span_successful(span):
            continue
        validation_groups.setdefault(str(fingerprint), []).append(item)
    write_items = [
        item for item in all_timed
        if _lower(getattr(item.span, "resource_access", None)) == "write"
    ]
    unknown_write_time = any(
        _lower(getattr(span, "resource_access", None)) == "write"
        and _span_interval(span) is None
        for trace in traces
        for span in _spans(trace)
    )
    for occurrences in validation_groups.values():
        labels = {item.label for item in occurrences}
        if len(occurrences) < 2:
            continue
        occurrences = sorted(occurrences, key=lambda item: (item.start, item.end, item.label, item.ordinal))
        if _has_validation_pair_without_write(occurrences, write_items, unknown_write_time):
            add("REPEATED_VALIDATION", labels, count=len(occurrences), evidence_level="derived")

    # Rule 5: overlap among direct parent/child spans.  Resource overlap with
    # a write is medium severity; same-command overlap without that condition
    # is an informational finding.  A pair contributes only once.
    for parent in traces:
        parent_items = timed_by_trace.get(id(parent), ())
        if not parent_items:
            continue
        for child in _direct_children(run, parent):
            child_items = timed_by_trace.get(id(child), ())
            if not child_items:
                continue
            pair_severity: Optional[str] = None
            for left in parent_items:
                for right in child_items:
                    if not _intervals_overlap(left.start, left.end, right.start, right.end):
                        continue
                    shared_resources = set(getattr(left.span, "resource_fingerprints", ()) or ()) & set(
                        getattr(right.span, "resource_fingerprints", ()) or ()
                    )
                    left_command = getattr(left.span, "command_fingerprint", None)
                    right_command = getattr(right.span, "command_fingerprint", None)
                    same_command = bool(left_command and left_command == right_command)
                    left_write = _lower(getattr(left.span, "resource_access", None)) == "write"
                    right_write = _lower(getattr(right.span, "resource_access", None)) == "write"
                    if shared_resources and (left_write or right_write):
                        pair_severity = "MEDIUM"
                        break
                    elif same_command:
                        if pair_severity is None:
                            pair_severity = "INFO"
                if pair_severity == "MEDIUM":
                    break
            if pair_severity is not None:
                add(
                    "PARENT_CHILD_OVERLAP",
                    (_label_for(parent, label_map), _label_for(child, label_map)),
                    severity=pair_severity,
                    evidence_level="derived",
                )

    # Rule 6: a reliably ordered reviewer -> worker/fixer -> reviewer chain
    # sharing one successful validation fingerprint.
    for occurrences in validation_groups.values():
        role_items: List[Tuple[str, _TimedSpan]] = []
        for item in occurrences:
            role = _role_for_label(item.label, traces, label_map)
            if role:
                role_items.append((role, item))
        reviewer_before = [item for role, item in role_items if role == "reviewer"]
        worker_middle = [item for role, item in role_items if role in {"worker", "fixer"}]
        reviewer_after = reviewer_before
        chain_found = False
        chain_labels: Tuple[str, str, str] = ("", "", "")
        for first in reviewer_before:
            for middle in worker_middle:
                for last in reviewer_after:
                    if first is middle or middle is last or first is last:
                        continue
                    if (
                        first.start < middle.start < last.start
                        and first.end <= middle.start
                        and middle.end <= last.start
                    ):
                        chain_found = True
                        chain_labels = (first.label, middle.label, last.label)
                        break
                if chain_found:
                    break
            if chain_found:
                break
        if chain_found:
            add("REVIEW_CHAIN", chain_labels, evidence_level="heuristic")

    result: List[Diagnostic] = []
    for code in _RULE_ORDER:
        finding = findings.get(code)
        if finding is None:
            continue
        result.append(
            Diagnostic(
                code=finding.code,
                severity=finding.severity,
                evidence_level=finding.evidence_level,
                message=finding.message,
                agent_labels=_sort_labels(finding.labels, label_map),
                count=finding.count,
            )
        )
    return result


def _ordered_traces(run: RunTrace) -> List[AgentTrace]:
    """Return root, child-first DFS, then stable remaining sessions."""

    root = getattr(run, "root", None)
    result: List[AgentTrace] = []
    visited: Set[int] = set()
    visited_session_ids: Set[str] = set()

    def add(trace: Any) -> bool:
        if not isinstance(trace, AgentTrace):
            return False
        marker = id(trace)
        session_id = getattr(trace, "session_id", None)
        if marker in visited or (session_id and session_id in visited_session_ids):
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
    entries.sort(key=lambda item: (str(getattr(item[1], "session_id", "") or ""), str(item[0])))
    for _, trace in entries:
        if isinstance(trace, AgentTrace):
            visit(trace)
    return result


def _direct_children(run: RunTrace, parent: AgentTrace) -> List[AgentTrace]:
    children = getattr(run, "children", {}) or {}
    candidates: Any = ()
    if isinstance(children, Mapping):
        session_id = getattr(parent, "session_id", None)
        candidates = children.get(session_id, ())
        if not candidates:
            candidates = children.get(id(parent), ())
    if not isinstance(candidates, (list, tuple)):
        candidates = tuple(candidates or ()) if isinstance(candidates, Iterable) else ()
    return [candidate for candidate in candidates if isinstance(candidate, AgentTrace)]


def _label_for(trace: AgentTrace, label_map: Mapping[Any, str]) -> str:
    if not isinstance(label_map, Mapping):
        return ""
    for key in (id(trace), trace, getattr(trace, "session_id", None)):
        try:
            value = label_map.get(key)
        except (TypeError, AttributeError):
            value = None
        if value:
            return _safe_text(value)
    return ""


def _sort_labels(labels: Iterable[str], label_map: Mapping[Any, str]) -> List[str]:
    order: Dict[str, int] = {}
    index = 0
    for value in label_map.values() if isinstance(label_map, Mapping) else ():
        text = _safe_text(value)
        if text and text not in order:
            order[text] = index
            index += 1
    return sorted({_safe_text(label) for label in labels if label}, key=lambda label: (order.get(label, 10**9), label))


def _spans(trace: AgentTrace) -> List[ToolSpan]:
    spans = getattr(trace, "tool_spans", ()) or ()
    return [span for span in spans if isinstance(span, ToolSpan)]


def _timed_spans_by_trace(
    traces: Sequence[AgentTrace], label_map: Mapping[Any, str]
) -> Dict[int, List[_TimedSpan]]:
    result: Dict[int, List[_TimedSpan]] = {}
    for trace in traces:
        label = _label_for(trace, label_map)
        timed: List[_TimedSpan] = []
        for ordinal, span in enumerate(_spans(trace)):
            interval = _span_interval(span)
            if interval is None:
                continue
            timed.append(_TimedSpan(span, interval[0], interval[1], label, ordinal))
        result[id(trace)] = timed
    return result


def _span_interval(span: ToolSpan) -> Optional[Tuple[float, float]]:
    start = _timestamp_number(getattr(span, "started_at", None))
    end = _timestamp_number(getattr(span, "ended_at", None))
    duration = _number(getattr(span, "duration_seconds", None))
    if start is None:
        return None
    if end is None and duration is not None and duration >= 0:
        end = start + duration
    if end is None or end < start:
        return None
    return start, end


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


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _intervals_overlap(left_start: float, left_end: float, right_start: float, right_end: float) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def _span_successful(span: ToolSpan) -> bool:
    success = getattr(span, "success", None)
    if success is not None:
        return success is True
    exit_code = getattr(span, "exit_code", None)
    if exit_code is not None:
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            pass
    status = _lower(getattr(span, "status", None))
    return status in {"success", "successful", "succeeded", "passed", "pass", "completed", "ok"}


def _has_validation_pair_without_write(
    occurrences: Sequence[_TimedSpan], writes: Sequence[_TimedSpan], unknown_write_time: bool = False
) -> bool:
    # Every known write needs a comparable interval.  If an un-timed write
    # exists, absence of an intervening write cannot be established safely.
    if unknown_write_time:
        return False
    for index, earlier in enumerate(occurrences):
        for later in occurrences[index + 1 :]:
            if later.start <= earlier.end:
                continue
            blocked = False
            for write in writes:
                if write.end <= earlier.end or write.start >= later.start:
                    continue
                blocked = True
                break
            if not blocked:
                return True
    return False


def _role_for_label(label: str, traces: Sequence[AgentTrace], label_map: Mapping[Any, str]) -> str:
    for trace in traces:
        if _label_for(trace, label_map) == label:
            return _lower(getattr(trace, "role", None))
    return ""


def _fork_mode(trace: AgentTrace) -> str:
    value = getattr(trace, "fork_mode", None)
    return _lower(value)


def _lower(value: Any) -> str:
    return _safe_text(value).lower() if value is not None else ""


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value[:512]
    else:
        text = str(value)[:512]
    if text.startswith("/") or text.startswith("~"):
        return "<redacted>"
    return text


def _severity_rank(value: str) -> int:
    return {"INFO": 1, "MEDIUM": 2, "HIGH": 3}.get(value.upper(), 0)


def _default_severity(code: str) -> str:
    return {
        "CTX_FULL_FORK": "HIGH",
        "CTX_FORK_UNKNOWN": "INFO",
        "HIGH_TIER_WORKER": "MEDIUM",
        "REPEATED_TOOL_CALL": "INFO",
        "REPEATED_VALIDATION": "MEDIUM",
        "PARENT_CHILD_OVERLAP": "MEDIUM",
        "REVIEW_CHAIN": "MEDIUM",
    }[code]


def _default_evidence(code: str) -> str:
    return {
        "CTX_FULL_FORK": "observed",
        "CTX_FORK_UNKNOWN": "observed",
        "HIGH_TIER_WORKER": "observed",
        "REPEATED_TOOL_CALL": "heuristic",
        "REPEATED_VALIDATION": "derived",
        "PARENT_CHILD_OVERLAP": "derived",
        "REVIEW_CHAIN": "heuristic",
    }[code]


__all__ = ["Diagnostic", "lint_run"]
