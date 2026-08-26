"""Build a privacy-safe history index from local Codex JSONL sessions.

The history path is intentionally separate from the single-run CLI loader.
It scans the supplied directory once, parses each file at most once, and
constructs runs only from sessions with an explicit nested ``session_meta``
identity.  Filesystem metadata is never used for chronology or inclusion.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .analysis import analyze_run
from .models import AgentTrace, RunTrace, TokenUsage
from .parser import _match_fork_mode, parse_session
from .reporting import public_analysis_dict


_SCHEMA_VERSION = "dashboard-history-0.1"
_SUPPORTED_WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DASHBOARD_WINDOWS = ("24h", "7d", "30d", "all")
_PARTIAL_WARNING_KINDS = {"truncated", "invalid_json", "invalid_record"}
_RUN_FIELDS = ("label", "project", "status", "quality_flags", "start_at", "end_at", "time_evidence", "agent_tree", "analysis")
_SCAN_FIELDS = (
    "files_seen",
    "files_parsed",
    "invalid_identity_count",
    "duplicate_id_count",
    "orphan_count",
    "cycle_count",
    "unreadable_count",
    "unknown_time_count",
)


def build_dashboard_history(
    sessions_root: Any,
    since: str = "7d",
    now: Any = None,
) -> Dict[str, Any]:
    """Return the stable, safe dashboard-history payload.

    ``since`` accepts ``24h``, ``7d``, ``30d``, or ``all``.  For a bounded
    window, a run is included when its latest observed event timestamp is on
    or after the cutoff.  Runs without any parseable event timestamp are
    counted by the scan but omitted from bounded runs and KPIs.
    """

    window = _normalize_window(since)
    generated_at, now_number = _resolve_now(now)
    root = Path(sessions_root)

    # This is deliberately the sole recursive discovery call.  Keep the
    # resulting list local so no later stage needs another filesystem walk.
    files = sorted(root.rglob("*.jsonl"), key=lambda path: str(path))
    scan: Dict[str, int] = {
        "files_seen": len(files),
        "files_parsed": 0,
        "invalid_identity_count": 0,
        "duplicate_id_count": 0,
        "orphan_count": 0,
        "cycle_count": 0,
        "unreadable_count": 0,
        "unknown_time_count": 0,
    }

    parsed: List[AgentTrace] = []
    for path in files:
        try:
            trace = parse_session(path)
        except (OSError, UnicodeError, ValueError, TypeError):
            scan["unreadable_count"] += 1
            continue
        if not isinstance(trace, AgentTrace):
            scan["unreadable_count"] += 1
            continue
        scan["files_parsed"] += 1
        if _trace_has_no_time(trace):
            scan["unknown_time_count"] += 1
        if not bool(getattr(trace, "identity_observed", False)) or not getattr(trace, "session_id", None):
            scan["invalid_identity_count"] += 1
            continue
        parsed.append(trace)

    by_id: Dict[str, List[AgentTrace]] = defaultdict(list)
    for trace in parsed:
        by_id[str(trace.session_id)].append(trace)

    duplicate_ids = {session_id for session_id, values in by_id.items() if len(values) > 1}
    scan["duplicate_id_count"] = len(duplicate_ids)
    unique: Dict[str, AgentTrace] = {
        session_id: values[0]
        for session_id, values in by_id.items()
        if session_id not in duplicate_ids
    }

    cycle_nodes, cycle_count = _cycle_nodes(unique)
    scan["cycle_count"] = cycle_count
    valid_ids = set(unique)

    children: Dict[str, List[str]] = defaultdict(list)
    for session_id, trace in unique.items():
        parent_id = getattr(trace, "parent_thread_id", None)
        if session_id in cycle_nodes or parent_id not in valid_ids or parent_id in cycle_nodes:
            continue
        if parent_id is not None:
            children[str(parent_id)].append(session_id)
    for parent_id in children:
        children[parent_id].sort()

    root_ids = sorted(
        session_id
        for session_id, trace in unique.items()
        if session_id not in cycle_nodes
        and getattr(trace, "parent_thread_id", None) is None
    )

    records: List[Dict[str, Any]] = []
    reachable: Set[str] = set()
    for root_id in root_ids:
        member_ids = _reachable_ids(root_id, children, cycle_nodes)
        if not member_ids:
            continue
        reachable.update(member_ids)
        run_trace = _make_run_trace(root_id, member_ids, unique, children)
        records.append(_run_record(run_trace, root_id, now_number, window))

    # A unique, non-cyclic session that cannot be reached from any root is an
    # orphan.  Descendants of excluded cycles are intentionally included here.
    scan["orphan_count"] = len(valid_ids - cycle_nodes - reachable)

    if window != "all":
        records = [record for record in records if record["_include"]]
    # Newest observations come first. The raw root id is used only as an
    # internal deterministic tie-breaker and is never serialized.
    records.sort(key=lambda record: record["_root_id"])
    records.sort(key=lambda record: record["_sort_end"], reverse=True)

    runs: List[Dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        public = {key: record[key] for key in _RUN_FIELDS}
        public["label"] = "run-%03d" % index
        runs.append(public)

    summary = _summary(runs)
    limitations: List[str] = [
        "chronology uses only safe event timestamps; filesystem mtime is ignored",
        "session identities and parent links require explicit nested session_meta metadata",
        "projects use only the root session cwd directory name; absolute paths are omitted and same-named directories are combined",
    ]
    if window != "all":
        limitations.append("runs without observed event timestamps are excluded from bounded windows and KPIs")
    if scan["unreadable_count"]:
        limitations.append("some local session files were unreadable")

    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated_at,
        "since": window,
        "scan": {key: scan[key] for key in _SCAN_FIELDS},
        "summary": summary,
        "runs": runs,
        "limitations": limitations,
    }


def _normalize_window(value: Any) -> str:
    text = "7d" if value is None else str(value).strip().lower()
    if text != "all" and text not in _SUPPORTED_WINDOWS:
        raise ValueError("since must be one of 24h, 7d, 30d, all")
    return text


def _resolve_now(value: Any) -> Tuple[str, float]:
    if value is None:
        current = datetime.now(timezone.utc)
        return _format_datetime(current), current.timestamp()
    number = _timestamp_number(value)
    if number is None:
        raise ValueError("now must be a numeric or ISO timestamp")
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return _format_datetime(current), number
    if isinstance(value, str):
        return value[:128], number
    return _format_datetime(datetime.fromtimestamp(number, timezone.utc)), number


def _format_datetime(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return normalized.replace("+00:00", "Z")


def _timestamp_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.timestamp()
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


def _safe_time(value: Any) -> Optional[Any]:
    number = _timestamp_number(value)
    if number is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:128]
    return _format_datetime(datetime.fromtimestamp(number, timezone.utc))


def _trace_has_no_time(trace: AgentTrace) -> bool:
    return _timestamp_number(getattr(trace, "observed_start_at", None)) is None and _timestamp_number(
        getattr(trace, "observed_end_at", None)
    ) is None


def _cycle_nodes(unique: Mapping[str, AgentTrace]) -> Tuple[Set[str], int]:
    state: Dict[str, int] = {}
    nodes: Set[str] = set()
    cycles: Set[frozenset] = set()

    for start in sorted(unique):
        if state.get(start, 0) == 2:
            continue
        path: List[str] = []
        positions: Dict[str, int] = {}
        current: Optional[str] = start
        while current in unique and state.get(current, 0) != 2:
            if current in positions:
                cycle = frozenset(path[positions[current] :])
                cycles.add(cycle)
                nodes.update(cycle)
                break
            if state.get(current, 0) == 1:
                # A node marked while another walk is active can only be part
                # of a cycle reachable from this path; retain the conservative
                # cycle suffix rather than treating it as an orphan.
                if current in path:
                    cycle = frozenset(path[path.index(current) :])
                    cycles.add(cycle)
                    nodes.update(cycle)
                break
            positions[current] = len(path)
            path.append(current)
            state[current] = 1
            parent = getattr(unique[current], "parent_thread_id", None)
            current = str(parent) if parent is not None and str(parent) in unique else None
        for item in path:
            state[item] = 2

    return nodes, len(cycles)


def _reachable_ids(root_id: str, children: Mapping[str, Sequence[str]], cycle_nodes: Set[str]) -> Set[str]:
    result: Set[str] = set()
    queue = [root_id]
    while queue:
        session_id = queue.pop(0)
        if session_id in result or session_id in cycle_nodes:
            continue
        result.add(session_id)
        queue.extend(children.get(session_id, ()))
    return result


def _make_run_trace(
    root_id: str,
    member_ids: Set[str],
    unique: Mapping[str, AgentTrace],
    all_children: Mapping[str, Sequence[str]],
) -> RunTrace:
    selected = {session_id: unique[session_id] for session_id in member_ids}
    run_children: Dict[str, List[AgentTrace]] = {}
    for parent_id in sorted(member_ids):
        child_ids = [child_id for child_id in all_children.get(parent_id, ()) if child_id in member_ids]
        if child_ids:
            run_children[parent_id] = [selected[child_id] for child_id in child_ids]
            for child_id in child_ids:
                _match_fork_mode(selected[parent_id], selected[child_id])
    warnings = []
    for session_id in sorted(member_ids):
        warnings.extend(getattr(selected[session_id], "warnings", ()) or ())
    return RunTrace(root=selected[root_id], sessions=selected, children=run_children, warnings=warnings)


def _ordered_traces(run_trace: RunTrace) -> List[AgentTrace]:
    """Mirror analysis' root/DFS ordering for tree-label alignment."""

    result: List[AgentTrace] = []
    visited: Set[int] = set()

    def visit(trace: AgentTrace) -> None:
        marker = id(trace)
        if marker in visited:
            return
        visited.add(marker)
        result.append(trace)
        for child in run_trace.children.get(trace.session_id or "", ()):
            visit(child)

    visit(run_trace.root)
    for session_id in sorted(run_trace.sessions):
        visit(run_trace.sessions[session_id])
    return result


def _run_record(run_trace: RunTrace, root_id: str, now_number: float, window: str) -> Dict[str, Any]:
    analysis = public_analysis_dict(analyze_run(run_trace))
    traces = _ordered_traces(run_trace)
    labels = {id(trace): "agent-%03d" % index for index, trace in enumerate(traces, 1)}
    parent_labels = {
        id(trace): labels.get(id(run_trace.sessions.get(trace.parent_thread_id)))
        if trace.parent_thread_id in run_trace.sessions
        else None
        for trace in traces
    }
    depth_by_trace = _tree_depths(run_trace)
    tree = [
        {
            "label": labels[id(trace)],
            "parent_label": parent_labels[id(trace)],
            "depth": depth_by_trace.get(id(trace), 0),
        }
        for trace in traces
    ]
    start_at, end_at = _run_time_bounds(traces)
    safe_start = _safe_time(start_at)
    safe_end = _safe_time(end_at)
    quality_flags = _quality_flags(traces, run_trace.root)
    status = _status(traces)
    project_name = str(getattr(run_trace.root, "project_name", "") or "").strip()
    include = window == "all" or (safe_end is not None and _timestamp_number(safe_end) >= now_number - _SUPPORTED_WINDOWS[window].total_seconds())
    return {
        "label": "",
        "project": {
            "label": project_name or "Unassigned",
            "evidence_level": "derived" if project_name else "unavailable",
        },
        "status": status,
        "quality_flags": quality_flags,
        "start_at": safe_start,
        "end_at": safe_end,
        # Source timestamps are observed; selecting run-wide bounds is a
        # deterministic derivation across the safe trace projection.
        "time_evidence": "derived" if safe_start is not None and safe_end is not None else "unavailable",
        "agent_tree": tree,
        "analysis": analysis,
        "_include": include,
        "_sort_end": _timestamp_number(safe_end) if safe_end is not None else float("-inf"),
        "_root_id": str(root_id),
    }


def _tree_depths(run_trace: RunTrace) -> Dict[int, int]:
    # Dataclasses are not hashable; key this internal map by object identity.
    result: Dict[int, int] = {}
    queue: List[Tuple[AgentTrace, int]] = [(run_trace.root, 0)]
    seen: Set[int] = set()
    while queue:
        trace, depth = queue.pop(0)
        marker = id(trace)
        if marker in seen:
            continue
        seen.add(marker)
        result[marker] = depth
        queue.extend((child, depth + 1) for child in run_trace.children.get(trace.session_id or "", ()))
    return result


def _run_time_bounds(traces: Sequence[AgentTrace]) -> Tuple[Optional[Any], Optional[Any]]:
    observed: List[Tuple[float, int, Any]] = []
    for index, trace in enumerate(traces):
        for endpoint in (getattr(trace, "observed_start_at", None), getattr(trace, "observed_end_at", None)):
            number = _timestamp_number(endpoint)
            if number is not None:
                observed.append((number, index, endpoint))
    if not observed:
        return None, None
    observed.sort(key=lambda item: (item[0], item[1]))
    return observed[0][2], observed[-1][2]


def _quality_flags(traces: Sequence[AgentTrace], root: AgentTrace) -> List[str]:
    flags: List[str] = []
    for trace in traces:
        for warning in getattr(trace, "warnings", ()) or ():
            kind = str(getattr(warning, "kind", "")).strip()
            if kind and kind not in flags and (kind in _PARTIAL_WARNING_KINDS or kind.startswith("invalid")):
                flags.append(kind)
    if not bool(getattr(root, "parent_present", False)):
        flags.append("parent_unknown")
    if _trace_has_no_time(root) and all(_trace_has_no_time(trace) for trace in traces):
        flags.append("no_time")
    if _has_in_progress_span(traces):
        flags.append("in_progress")
    return flags


def _has_in_progress_span(traces: Sequence[AgentTrace]) -> bool:
    active = {"running", "in_progress", "in-progress", "started", "pending", "queued"}
    terminal = {"completed", "complete", "success", "successful", "succeeded", "passed", "pass", "ok", "failed", "error", "cancelled", "canceled"}
    for trace in traces:
        for span in getattr(trace, "tool_spans", ()) or ():
            status = str(getattr(span, "status", "") or "").strip().lower()
            if status in active:
                return True
            if getattr(span, "ended_at", None) is None and status not in terminal:
                return True
    return False


def _status(traces: Sequence[AgentTrace]) -> str:
    for trace in traces:
        for warning in getattr(trace, "warnings", ()) or ():
            kind = str(getattr(warning, "kind", "")).strip().lower()
            if kind in _PARTIAL_WARNING_KINDS or kind.startswith("invalid"):
                return "partial"
    if _has_in_progress_span(traces):
        return "in_progress"
    return "observed"


def _empty_quota() -> Dict[str, Any]:
    return {
        "plan_type": None,
        "current_used_percent": None,
        "observed_delta_percent": None,
        "window_minutes": None,
        "resets_at": None,
        "has_credits": None,
        "observed_at": None,
        "evidence_level": "unavailable",
    }


def _summary(runs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total = TokenUsage()
    agent_count = 0
    tool_count = 0
    high_count = 0
    for run in runs:
        analysis = run.get("analysis") if isinstance(run.get("analysis"), Mapping) else {}
        usage = analysis.get("total_token_usage") if isinstance(analysis, Mapping) else {}
        total = total + TokenUsage(**{field: usage.get(field, 0) for field in TokenUsage._FIELDS})
        agents = analysis.get("agents", ()) if isinstance(analysis, Mapping) else ()
        agent_count += len(agents) if isinstance(agents, (list, tuple)) else 0
        for agent in agents if isinstance(agents, (list, tuple)) else ():
            if isinstance(agent, Mapping):
                try:
                    tool_count += max(0, int(agent.get("tool_count", 0)))
                except (TypeError, ValueError):
                    pass
        diagnostics = analysis.get("diagnostics", ()) if isinstance(analysis, Mapping) else ()
        for diagnostic in diagnostics if isinstance(diagnostics, (list, tuple)) else ():
            if not isinstance(diagnostic, Mapping):
                continue
            severity = str(diagnostic.get("severity", "")).upper()
            if severity not in {"HIGH", "CRITICAL", "ERROR"}:
                continue
            try:
                high_count += max(1, int(diagnostic.get("count", 1)))
            except (TypeError, ValueError):
                high_count += 1
    latest_quota = _empty_quota()
    quota_candidates: List[Tuple[float, int, Mapping[str, Any]]] = []
    stable_quota: Optional[Mapping[str, Any]] = None
    for index, run in enumerate(runs):
        analysis = run.get("analysis")
        quota = analysis.get("quota") if isinstance(analysis, Mapping) else None
        if not isinstance(quota, Mapping):
            continue
        if stable_quota is None:
            stable_quota = quota
        observed = _timestamp_number(quota.get("observed_at"))
        if observed is not None:
            quota_candidates.append((observed, -index, quota))
    if quota_candidates:
        latest_quota = dict(max(quota_candidates, key=lambda item: (item[0], item[1]))[2])
    elif stable_quota is not None:
        latest_quota = dict(stable_quota)
    return {
        "run_count": len(runs),
        "agent_count": agent_count,
        "tool_count": tool_count,
        "total_token_usage": total.to_dict(),
        "high_diagnostic_count": high_count,
        "latest_quota": latest_quota,
    }


__all__ = ["DASHBOARD_WINDOWS", "build_dashboard_history"]
