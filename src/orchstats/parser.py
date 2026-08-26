"""Streaming parser for Codex JSONL session traces.

Only a small safe projection of each record is retained.  In particular,
prompt text, reasoning text, tool output, raw commands, working directories,
and balances are discarded while parsing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .models import (
    AgentTrace,
    ParseWarning,
    RateLimitSnapshot,
    RunTrace,
    TokenUsage,
    ToolSpan,
    _redact_path_tokens,
    _safe_project_name as _model_safe_project_name,
    _safe_relative_path as _model_safe_relative_path,
    normalize_fork_turns,
)


_TOKEN_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

_TOKEN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens", "input"),
    "cached_input_tokens": (
        "cached_input_tokens",
        "cachedInputTokens",
        "cached_input",
        "cache_read_input_tokens",
    ),
    "cache_write_input_tokens": (
        "cache_write_input_tokens",
        "cacheWriteInputTokens",
        "cache_creation_input_tokens",
        "cache_write_input",
    ),
    "output_tokens": ("output_tokens", "outputTokens", "output"),
    "reasoning_output_tokens": (
        "reasoning_output_tokens",
        "reasoningOutputTokens",
        "reasoning_output",
    ),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}

_GENERIC_WRAPPERS = {
    "event_msg",
    "response_item",
    "message",
    "record",
    "event",
    "item",
    "notification",
}

_KIND_ALIASES = {
    "sessionmeta": "session_meta",
    "session_metadata": "session_meta",
    "turncontext": "turn_context",
    "tokencount": "token_count",
    "tokenusage": "token_count",
    "functioncall": "function_call",
    "function_call_output": "function_call_output",
    "customtoolcall": "custom_tool_call",
    "custom_tool_call_output": "custom_tool_call_output",
    "toolcall": "tool_call",
    "commandexecution": "command_execution",
    "command_execution_output": "command_execution_output",
    "itemcompleted": "item_completed",
    "itemstarted": "item_started",
    "subagentactivity": "subagent_activity",
    "patch_apply_end": "patch_apply_end",
}

_TOOL_KINDS = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "tool_call",
    "command_execution",
    "command_execution_output",
    "item_completed",
    "item_started",
    "subagent_activity",
    "patch_apply_end",
}


@dataclass
class _Event:
    """Safe intermediate event; no raw JSON is retained here."""

    kind: str
    ordinal: Optional[float]
    line_number: int
    timestamp: Any
    data: Dict[str, Any]

    @property
    def sort_key(self) -> Tuple[int, float, int]:
        if self.ordinal is None:
            return (1, float(self.line_number), self.line_number)
        return (0, self.ordinal, self.line_number)


@dataclass
class _TokenSnapshot:
    usage: TokenUsage
    present: Tuple[str, ...]
    rate_limits: Dict[str, RateLimitSnapshot]


def parse_session(path: Any) -> AgentTrace:
    """Parse one JSONL session with a streaming line reader.

    Unknown record types and malformed lines are non-fatal.  The returned
    trace contains only safe metadata and derived token/quota summaries.
    """

    session_path = Path(path)
    events: List[_Event] = []
    warnings: List[ParseWarning] = []

    with session_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                is_tail = not raw_line.endswith("\n")
                kind = "truncated" if is_tail else "invalid_json"
                message = (
                    "truncated final JSONL record"
                    if is_tail
                    else "invalid JSONL record"
                )
                warnings.append(ParseWarning(kind, message, line_number))
                continue
            if not isinstance(record, dict):
                warnings.append(
                    ParseWarning("invalid_record", "JSONL record is not an object", line_number)
                )
                continue

            kind, candidates = _event_kind_and_candidates(record)
            ordinal = _extract_ordinal(record, candidates)
            timestamp = _extract_timestamp(record, candidates)
            event = _make_event(kind, record, candidates, ordinal, timestamp, line_number)
            if event is None:
                event_name = _safe_event_name(kind)
                warnings.append(
                    ParseWarning(
                        "unknown_type",
                        "unknown trace event type",
                        line_number,
                        event_name,
                    )
                )
                # Retain only the scalar timestamp for chronology.  The
                # unknown event's payload remains discarded, but a valid
                # event timestamp is still safe evidence for history bounds.
                events.append(_Event("unknown", ordinal, line_number, timestamp, {}))
                continue
            events.append(event)

    events.sort(key=lambda event: event.sort_key)
    return _build_trace(events, warnings, session_path)


def load_run(
    root_path: Any,
    sessions_root: Optional[Any] = None,
    discover: bool = True,
) -> RunTrace:
    """Load a root session and, optionally, discovered descendant sessions.

    ``root_path`` can be a JSONL file or a directory containing fixtures.  A
    session under ``.../sessions/YYYY/MM/DD`` causes the enclosing ``sessions``
    directory to be searched; a flat fixture directory searches its parent.
    Only ``*.jsonl`` files are read, and the explicit root is always included.
    """

    requested = Path(root_path)
    if not requested.exists():
        raise FileNotFoundError(str(requested))

    root_file: Optional[Path]
    if requested.is_file():
        root_file = requested
    else:
        root_file = _choose_root_file(requested)

    if root_file is None or root_file.suffix.lower() != ".jsonl":
        raise ValueError("root_path must identify or contain a .jsonl root session")

    files: List[Path] = [root_file]
    if discover:
        search_root = Path(sessions_root) if sessions_root is not None else _find_sessions_root(root_file)
        if search_root is None:
            # Flat synthetic fixtures are intentionally local to the root's
            # parent; avoid walking an unrelated workspace.
            search_root = root_file.parent
        files.extend(_jsonl_files(search_root))

    # Keep stable order and include the root even if it lies outside an
    # explicitly supplied sessions_root.
    unique: Dict[str, Path] = {}
    for candidate in files:
        if candidate.suffix.lower() == ".jsonl" and candidate.is_file():
            unique[str(candidate.resolve())] = candidate
    files = sorted(unique.values(), key=lambda candidate: str(candidate))
    root_key = str(root_file.resolve())
    if root_key not in unique:
        files.insert(0, root_file)
    else:
        # Sorting above is useful for reproducibility; parse root first so a
        # duplicate session id can be resolved in its intended direction.
        files.remove(unique[root_key])
        files.insert(0, unique[root_key])

    traces_by_file: Dict[str, AgentTrace] = {}
    for candidate in files:
        trace = parse_session(candidate)
        traces_by_file[str(candidate.resolve())] = trace

    root_trace = traces_by_file[root_key]

    # The discovery scan is only an index.  A run consists of the explicit
    # root and traces reachable through parent_thread_id; unrelated sessions
    # (including malformed/orphan fixtures) must not contribute agents,
    # tokens, or warnings to this RunTrace.
    candidates_by_id: Dict[str, List[Tuple[Path, AgentTrace]]] = {}
    for candidate in files:
        trace = traces_by_file[str(candidate.resolve())]
        session_id = trace.session_id or candidate.stem
        candidates_by_id.setdefault(session_id, []).append((candidate, trace))

    root_id = root_trace.session_id or root_file.stem
    selected: Dict[str, AgentTrace] = {root_id: root_trace}
    children: Dict[str, List[AgentTrace]] = {}
    queue: List[AgentTrace] = [root_trace]
    while queue:
        parent = queue.pop(0)
        parent_id = parent.session_id
        if not parent_id:
            continue
        grouped: Dict[str, List[Tuple[Path, AgentTrace]]] = {}
        for child_id, options in candidates_by_id.items():
            matching = [
                option
                for option in options
                if option[1].parent_thread_id == parent_id
            ]
            if matching:
                grouped[child_id] = matching

        for child_id, options in sorted(grouped.items(), key=lambda item: item[0]):
            # Root wins over a duplicate ID.  Any other duplicate with the
            # same selected parent is ambiguous and is excluded rather than
            # silently counting one arbitrary session.
            if child_id in selected or len(options) != 1:
                continue
            child = options[0][1]
            selected[child_id] = child
            children.setdefault(parent_id, []).append(child)
            _match_fork_mode(parent, child)
            queue.append(child)

    for child_list in children.values():
        child_list.sort(
            key=lambda trace: (trace.depth is None, trace.depth or 0, trace.session_id or "")
        )

    all_warnings: List[ParseWarning] = []
    for trace in selected.values():
        all_warnings.extend(trace.warnings)
    return RunTrace(root=root_trace, sessions=selected, children=children, warnings=all_warnings)


def normalize_command(command: Any) -> Optional[str]:
    """Apply the small deterministic normalization used before hashing.

    Commands are intentionally never returned by a trace model.  Normalizing
    only folded whitespace and obvious POSIX/Windows absolute path tokens
    makes equivalent local invocations share a fingerprint without attempting
    to parse shell syntax.
    """

    if not isinstance(command, str):
        return None
    text = re.sub(r"\s+", " ", command.strip())
    if not text:
        return ""
    # Keep the command itself private while canonicalizing every supported
    # absolute path spelling, including file:// URIs and UNC paths.
    text = _redact_path_tokens(text, "<ABS_PATH>")
    return text


def command_fingerprint(command: Any) -> Optional[str]:
    """Return a SHA-256 fingerprint without retaining the command itself."""

    normalized = normalize_command(command)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def resource_fingerprint(resource: Any) -> Optional[str]:
    """Return a SHA-256 fingerprint for a resource/path value."""

    if not isinstance(resource, str):
        return None
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()


def classify_command(command: Any) -> Optional[str]:
    """Classify common validation/inspection commands without storing them."""

    if not isinstance(command, str):
        return None
    text = command.strip().lower()
    if not text:
        return None
    if (
        "unittest" in text
        or "pytest" in text
        or "test" in text and ("npm" in text or "cargo" in text)
        or "ruff" in text
        or "mypy" in text
        or "compileall" in text
    ):
        return "validation"
    if re.search(r"(^|\s)git\s+(status|diff|show|log)\b", text):
        return "git_inspection"
    if re.search(r"(^|\s)(rg|grep|find|ls|pwd|sed|head|tail)\b", text):
        return "inspection"
    if re.search(r"(^|\s)(python|python3|node|ruby)\b", text):
        return "runtime"
    return "other"


# A descriptive alias is convenient for analyzer code and keeps the parser's
# public surface intentionally small.
fingerprint_command = command_fingerprint


def _make_event(
    kind: str,
    record: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    ordinal: Optional[float],
    timestamp: Any,
    line_number: int,
) -> Optional[_Event]:
    if kind == "session_meta":
        return _Event(kind, ordinal, line_number, timestamp, _extract_session_meta(candidates, record))
    if kind == "turn_context":
        return _Event(kind, ordinal, line_number, timestamp, _extract_turn_context(candidates))
    if kind == "token_count":
        snapshot = _extract_token_snapshot(candidates, timestamp)
        if snapshot is None:
            # A token_count marker without usage is still a recognized record;
            # no raw info is kept and no warning is necessary.
            snapshot = _TokenSnapshot(TokenUsage(), (), {})
        return _Event(kind, ordinal, line_number, timestamp, {"snapshot": snapshot})
    if kind in _TOOL_KINDS:
        details = _extract_tool_details(kind, candidates, timestamp)
        # ``item_completed`` can complete a normal assistant message.  Only
        # retain an event when it carries tool/call metadata.
        if details is None:
            return None
        return _Event(kind, ordinal, line_number, timestamp, details)
    return None


def _build_trace(
    events: Sequence[_Event], warnings: List[ParseWarning], session_path: Path
) -> AgentTrace:
    meta: Dict[str, Any] = {}
    model: Optional[str] = None
    effort: Optional[str] = None
    total_usage = TokenUsage()
    previous_values: Dict[str, int] = {}
    seen_token_fields: Set[str] = set()
    first_rates: Dict[str, RateLimitSnapshot] = {}
    last_rates: Dict[str, RateLimitSnapshot] = {}
    span_order: List[ToolSpan] = []
    spans_by_call_id: Dict[str, ToolSpan] = {}
    activity_call_ids: List[str] = []
    activity_task_names: List[str] = []
    activity_agent_paths: List[str] = []
    inferred_fork_mode = "unknown"
    observed_start_at, observed_end_at = _event_time_bounds(events)

    for event in events:
        if event.kind == "session_meta":
            for key, value in event.data.items():
                if value is not None and value != "":
                    meta[key] = value
            continue
        if event.kind == "turn_context":
            if event.data.get("model"):
                model = event.data["model"]
            if event.data.get("effort"):
                effort = event.data["effort"]
            continue
        if event.kind == "token_count":
            snapshot = event.data.get("snapshot")
            if not isinstance(snapshot, _TokenSnapshot):
                continue
            current = snapshot.usage
            for name in snapshot.present:
                seen_token_fields.add(name)
                value = getattr(current, name)
                previous = previous_values.get(name)
                if previous is None:
                    delta = value
                elif value < previous:
                    delta = value
                else:
                    delta = value - previous
                setattr(total_usage, name, getattr(total_usage, name) + max(0, delta))
                previous_values[name] = value
            for key, rate in snapshot.rate_limits.items():
                if key not in first_rates:
                    first_rates[key] = rate
                last_rates[key] = rate
            continue
        details = event.data
        span = _span_from_details(details)
        if span is None:
            continue
        if span.call_id and span.call_id in spans_by_call_id:
            _merge_span(spans_by_call_id[span.call_id], span)
        else:
            span_order.append(span)
            if span.call_id:
                spans_by_call_id[span.call_id] = span
        if event.kind == "subagent_activity" or span.tool_kind == "subagent_activity":
            if span.call_id:
                _append_unique(activity_call_ids, span.call_id)
            if span.task_name:
                _append_unique(activity_task_names, span.task_name)
            if span.agent_path:
                _append_unique(activity_agent_paths, span.agent_path)
            if span.fork_turns != "unknown":
                inferred_fork_mode = span.fork_turns

    if not seen_token_fields:
        total_usage = TokenUsage()
    elif "total_tokens" not in seen_token_fields:
        # A few exporters omit the aggregate counter.  Derive a transparent
        # fallback rather than pretending the missing field was observed.
        total_usage.total_tokens = total_usage.input_tokens + total_usage.output_tokens

    # The parser keeps the historical filename fallback for the older
    # ``load_run`` API, but records whether a usable id was actually observed
    # in the nested session_meta payload.  Phase 3's history index filters on
    # this explicit provenance marker and never treats a filename as identity.
    identity_observed = bool(meta.get("identity_observed"))
    session_id = _safe_identifier(meta.get("session_id") or meta.get("id"))
    if not session_id:
        session_id = _safe_identifier(session_path.stem)
    parent_id = _safe_identifier(
        meta.get("parent_thread_id") or meta.get("parent_id") or meta.get("parent_session_id")
    )
    parent_present = bool(meta.get("parent_present"))
    path_value = meta.get("path") or meta.get("agent_path")
    path_value = _safe_relative_path(path_value) if path_value else None
    agent_path = _safe_relative_path(meta.get("agent_path")) if meta.get("agent_path") else None
    task_name = _safe_text(meta.get("task_name")) if meta.get("task_name") else None
    role = _safe_text(meta.get("role") or meta.get("agent_type"))
    depth = _safe_int(meta.get("depth"))
    return AgentTrace(
        session_id=session_id,
        parent_thread_id=parent_id,
        identity_observed=identity_observed,
        parent_present=parent_present,
        observed_start_at=observed_start_at,
        observed_end_at=observed_end_at,
        role=role,
        project_name=_safe_text(meta.get("project_name")),
        path=path_value,
        depth=depth,
        model=_safe_text(model),
        effort=_safe_text(effort),
        token_usage=total_usage,
        rate_limits_first=first_rates,
        rate_limits_last=last_rates,
        tool_spans=span_order,
        warnings=warnings,
        fork_mode=inferred_fork_mode,
        task_name=task_name,
        agent_path=agent_path,
        activity_call_ids=tuple(activity_call_ids),
        activity_task_names=tuple(activity_task_names),
        activity_agent_paths=tuple(activity_agent_paths),
    )


def _event_kind_and_candidates(record: Mapping[str, Any]) -> Tuple[str, List[Mapping[str, Any]]]:
    candidates = _candidate_dicts(record)
    raw_kind: Any = record.get("type") or record.get("record_type") or record.get("event_type")
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        nested_kind = payload.get("type") or payload.get("event_type") or payload.get("record_type")
        if nested_kind and (_normalize_kind(raw_kind) in _GENERIC_WRAPPERS or not raw_kind):
            raw_kind = nested_kind
    # A nested response item is a common wrapper for function/custom calls.
    for candidate in candidates[1:]:
        nested_kind = candidate.get("type") or candidate.get("event_type")
        if nested_kind and (_normalize_kind(raw_kind) in _GENERIC_WRAPPERS or not raw_kind):
            raw_kind = nested_kind
            break
    return _normalize_kind(raw_kind), candidates


def _candidate_dicts(record: Mapping[str, Any], max_depth: int = 4) -> List[Mapping[str, Any]]:
    result: List[Mapping[str, Any]] = []
    seen: Set[int] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > max_depth or not isinstance(value, Mapping):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        result.append(value)
        # Keep the usual payload/item fields first for lookup while allowing
        # generic nested wrappers from observed exporters.
        for key in ("payload", "data", "event", "item", "info", "message", "activity"):
            child = value.get(key)
            if isinstance(child, Mapping):
                visit(child, depth + 1)
        for key, child in value.items():
            if key in {"payload", "data", "event", "item", "info", "message", "activity"}:
                continue
            if isinstance(child, Mapping) and key in {"result", "details", "context", "metadata"}:
                visit(child, depth + 1)

    visit(record, 0)
    return result


def _normalize_kind(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return _KIND_ALIASES.get(normalized, _KIND_ALIASES.get(compact, normalized))


def _extract_ordinal(record: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> Optional[float]:
    value = record.get("ordinal")
    if value is None:
        value = _first_value(candidates, ("ordinal", "sequence", "seq"))
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_timestamp(record: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> Any:
    value = record.get("timestamp")
    if value is None:
        value = _first_value(candidates, ("timestamp", "created_at", "started_at", "time"))
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    return None


def _extract_session_meta(
    candidates: Sequence[Mapping[str, Any]], record: Mapping[str, Any]
) -> Dict[str, Any]:
    # A history identity is valid only when an explicit id is present in a
    # nested session_meta payload.  In particular, do not use a top-level id,
    # session_id, or the JSONL filename as a substitute.
    # Restrict the nested walk to event/payload wrappers.  Metadata/result
    # dictionaries can contain unrelated ids and therefore are not identity
    # evidence for the session itself.
    nested: List[Mapping[str, Any]] = []
    seen: Set[int] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > 4 or not isinstance(value, Mapping):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if value is not record:
            nested.append(value)
        for key in ("payload", "data", "event", "item", "info", "message", "activity"):
            child = value.get(key)
            if isinstance(child, Mapping):
                visit(child, depth + 1)

    visit(record, 0)
    session_id: Any = None
    identity_observed = False
    for candidate in nested:
        if "id" in candidate:
            value = candidate.get("id")
            if _safe_identifier(value):
                session_id = value
                identity_observed = True
                break

    parent_present = False
    parent_value: Any = None
    for candidate in nested:
        for key in ("parent_thread_id", "parent_id", "parent_session_id"):
            if key in candidate:
                parent_present = True
                parent_value = candidate.get(key)
                break
        if parent_present:
            break

    def nested_value(keys: Iterable[str]) -> Any:
        return _first_value(nested, tuple(keys))

    return {
        "session_id": _safe_identifier(session_id),
        "identity_observed": identity_observed,
        "parent_present": parent_present,
        "parent_thread_id": _safe_identifier(parent_value),
        "role": _safe_text(nested_value(("role", "agent_role", "agent_type"))),
        "project_name": _safe_project_name(
            nested_value(("cwd", "working_directory", "workingDirectory"))
        ),
        "path": _safe_relative_path(nested_value(("path", "session_path")))
        if nested_value(("path", "session_path")) is not None
        else None,
        "agent_path": _safe_relative_path(nested_value(("agent_path",)))
        if nested_value(("agent_path",)) is not None
        else None,
        "task_name": _safe_text(nested_value(("task_name",))),
        "depth": _safe_int(nested_value(("depth", "agent_depth"))),
    }


def _extract_turn_context(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "model": _safe_text(_first_value(candidates, ("model", "model_name"))),
        "effort": _safe_text(
            _first_value(candidates, ("effort", "reasoning_effort", "reasoning_level"))
        ),
    }


def _extract_token_snapshot(
    candidates: Sequence[Mapping[str, Any]], observed_at: Any = None
) -> Optional[_TokenSnapshot]:
    usage_map: Optional[Mapping[str, Any]] = None
    for candidate in candidates:
        for key in ("total_token_usage", "totalTokenUsage", "token_usage", "tokenUsage"):
            nested = candidate.get(key)
            if isinstance(nested, Mapping):
                usage_map = nested
                break
        if usage_map is not None:
            break
    if usage_map is None:
        # Some compact fixtures put token counters directly in the event.
        if any(_first_present(candidate, aliases) for candidate in candidates for aliases in _TOKEN_ALIASES.values()):
            usage_map = _first_mapping_with_token_fields(candidates)
    if usage_map is None:
        return None

    values: Dict[str, int] = {}
    present: List[str] = []
    for field_name in _TOKEN_FIELDS:
        raw = _mapping_value(usage_map, _TOKEN_ALIASES[field_name])
        if raw is None:
            continue
        parsed = _safe_nonnegative_int(raw)
        values[field_name] = parsed
        present.append(field_name)
    usage = TokenUsage(**values)
    rates = _extract_rate_limits(candidates, observed_at)
    return _TokenSnapshot(usage=usage, present=tuple(present), rate_limits=rates)


def _first_mapping_with_token_fields(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for candidate in candidates:
        if any(_mapping_value(candidate, aliases) is not None for aliases in _TOKEN_ALIASES.values()):
            return candidate
    return {}


def _extract_rate_limits(
    candidates: Sequence[Mapping[str, Any]], observed_at: Any = None
) -> Dict[str, RateLimitSnapshot]:
    raw: Any = None
    for candidate in candidates:
        for key in ("rate_limits", "rateLimits", "rate_limit", "rateLimit"):
            value = candidate.get(key)
            if isinstance(value, (Mapping, list, tuple)):
                raw = value
                break
        if raw is not None:
            break
    if raw is None:
        # Direct snapshot shape is useful for compact synthetic fixtures.
        if any(_first_present(candidate, ("used_percent", "usedPercent")) for candidate in candidates):
            raw = _first_mapping_with_rate_fields(candidates)
    if raw is None:
        return {}
    result: Dict[str, RateLimitSnapshot] = {}
    if isinstance(raw, Mapping):
        # The observed Codex shape keeps plan and credit flags beside the
        # primary window.  Merge only the safe booleans/plan into that window;
        # the credits object may also contain a balance, which is deliberately
        # never inspected or copied.
        primary = raw.get("primary")
        if isinstance(primary, Mapping):
            merged_primary = dict(primary)
            if raw.get("plan_type") is not None and "plan_type" not in merged_primary:
                merged_primary["plan_type"] = raw.get("plan_type")
            credits = raw.get("credits")
            if isinstance(credits, Mapping):
                for key in ("has_credits", "hasCredits", "unlimited", "is_unlimited", "isUnlimited"):
                    if key in credits and key not in merged_primary:
                        merged_primary[key] = credits[key]
            snapshot = _rate_snapshot(merged_primary, observed_at)
            if snapshot is not None:
                limit_key = _safe_text(raw.get("limit_id") or raw.get("limitId")) or "primary"
                result[limit_key] = snapshot
            # Preserve any additional named windows, while excluding the
            # credits metadata object from the public quota map.
            for name, value in raw.items():
                if name in {"primary", "credits", "limit_id", "limitId", "plan_type", "planType"}:
                    continue
                if not isinstance(value, Mapping):
                    continue
                extra = _rate_snapshot(value, observed_at)
                if extra is not None:
                    result[_safe_text(name) or "default"] = extra
            return result
        direct_keys = {"used_percent", "usedPercent", "window_minutes", "windowMinutes", "resets_at", "resetsAt", "plan_type", "planType", "has_credits", "hasCredits", "unlimited"}
        if any(key in raw for key in direct_keys):
            snapshot = _rate_snapshot(raw, observed_at)
            if snapshot is not None:
                result["default"] = snapshot
            return result
        for name, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            snapshot = _rate_snapshot(value, observed_at)
            if snapshot is not None:
                result[_safe_text(name) or "default"] = snapshot
    elif isinstance(raw, (list, tuple)):
        for index, value in enumerate(raw):
            if not isinstance(value, Mapping):
                continue
            name = _safe_text(value.get("name") or value.get("window") or index) or str(index)
            snapshot = _rate_snapshot(value, observed_at)
            if snapshot is not None:
                result[name] = snapshot
    return result


def _first_mapping_with_rate_fields(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for candidate in candidates:
        if _first_present(candidate, ("used_percent", "usedPercent")):
            return candidate
    return {}


def _rate_snapshot(
    value: Mapping[str, Any], observed_at: Any = None
) -> Optional[RateLimitSnapshot]:
    used = _mapping_value(value, ("used_percent", "usedPercent", "usage_percent", "usagePercent"))
    window = _mapping_value(value, ("window_minutes", "windowMinutes", "window"))
    resets = _mapping_value(value, ("resets_at", "resetsAt", "reset_at", "resetAt"))
    plan = _mapping_value(value, ("plan_type", "planType", "plan"))
    credits = _mapping_value(value, ("has_credits", "hasCredits", "credits_available"))
    unlimited = _mapping_value(value, ("unlimited", "is_unlimited", "isUnlimited"))
    if all(item is None for item in (used, window, resets, plan, credits, unlimited)):
        return None
    return RateLimitSnapshot(
        used_percent=_safe_float(used),
        window_minutes=_safe_int(window),
        resets_at=_safe_timestamp(resets),
        observed_at=_safe_timestamp(observed_at),
        plan_type=_safe_text(plan),
        has_credits=_safe_bool(credits),
        unlimited=_safe_bool(unlimited),
    )


def _extract_tool_details(
    kind: str, candidates: Sequence[Mapping[str, Any]], timestamp: Any
) -> Optional[Dict[str, Any]]:
    if kind == "patch_apply_end":
        return _extract_patch_apply_details(candidates, timestamp)

    item_candidates = list(candidates)
    item = _first_mapping(candidates, ("item",))
    if item is not None:
        item_candidates = _candidate_dicts(item) + item_candidates

    call_id = _safe_identifier(_first_value(item_candidates, ("call_id", "callId", "tool_call_id", "id")))
    name = _safe_text(_first_value(item_candidates, ("name", "tool_name", "function_name")))
    item_type = _normalize_kind(_first_value(item_candidates, ("type", "item_type", "tool_type")))
    if item_type in _GENERIC_WRAPPERS:
        item_type = kind
    status = _safe_text(_first_value(item_candidates, ("status", "state", "result_status")))
    if kind == "item_completed" and not status:
        status = "completed"

    argument_value = _first_value(item_candidates, ("arguments", "args", "input", "parameters"))
    argument_map = _argument_mapping(argument_value)
    if not name and argument_map:
        name = _safe_text(argument_map.get("name") or argument_map.get("tool_name"))
    if not call_id:
        call_id = _safe_identifier(argument_map.get("call_id") or argument_map.get("callId"))

    is_command = (
        kind in {"command_execution", "command_execution_output"}
        or item_type in {"command_execution", "command_execution_output"}
        or (name or "").lower() in {"exec_command", "command_execution", "run_command", "shell"}
        or any(key in argument_map for key in ("cmd", "command", "shell_command"))
    )
    is_activity = kind == "subagent_activity" or item_type == "subagent_activity"
    is_spawn = (name or "").lower() in {"spawn_agent", "spawnagent"}

    # Completion markers without a call id/name/type are ordinary message
    # completions and are intentionally ignored.
    if not (call_id or name or is_command or is_activity):
        return None

    started_at_ms = _first_value(item_candidates, ("started_at_ms", "startedAtMs"))
    completed_at_ms = _first_value(item_candidates, ("completed_at_ms", "completedAtMs"))
    started_at = (
        _safe_timestamp_ms(started_at_ms)
        if started_at_ms is not None
        else _safe_timestamp(
            _first_value(item_candidates, ("started_at", "start_time", "started", "created_at"))
        )
    )
    ended_at = (
        _safe_timestamp_ms(completed_at_ms)
        if completed_at_ms is not None
        else _safe_timestamp(
            _first_value(item_candidates, ("ended_at", "end_time", "completed_at", "finished_at"))
        )
    )
    if started_at is None and timestamp is not None and kind in {"function_call", "custom_tool_call", "tool_call", "command_execution", "item_started"}:
        started_at = _safe_timestamp(timestamp)
    if ended_at is None and timestamp is not None and kind in {"function_call_output", "custom_tool_call_output", "command_execution_output", "item_completed"}:
        ended_at = _safe_timestamp(timestamp)
    duration = _duration_from(item_candidates, started_at, ended_at)

    fork_value = None
    agent_type = None
    task_name = None
    agent_path = None
    if is_spawn or is_activity:
        fork_value = argument_map.get("fork_turns")
        if fork_value is None:
            fork_value = _first_value(item_candidates, ("fork_turns", "forkTurns", "fork_mode"))
        agent_type = _safe_text(argument_map.get("agent_type") or argument_map.get("role"))
        if not agent_type:
            agent_type = _safe_text(_first_value(item_candidates, ("agent_type", "agent_role", "role")))
        task_name = _safe_text(argument_map.get("task_name"))
        if not task_name:
            task_name = _safe_text(_first_value(item_candidates, ("task_name", "taskName")))
        agent_path = _safe_relative_path(argument_map.get("agent_path")) if argument_map.get("agent_path") else None
        if not agent_path:
            raw_path = _first_value(item_candidates, ("agent_path", "agentPath", "path"))
            agent_path = _safe_relative_path(raw_path) if raw_path is not None else None

    command = None
    if is_command:
        command = argument_map.get("cmd") or argument_map.get("command") or argument_map.get("shell_command")
        if command is None:
            command = _first_value(item_candidates, ("cmd", "command", "shell_command"))

    resource_values: List[str] = []
    for candidate in item_candidates:
        for key in ("resource", "resources", "paths", "files"):
            if key not in candidate:
                continue
            resource_values.extend(_scalar_values(candidate.get(key)))
    for key in ("resource", "resources", "paths", "files"):
        if key in argument_map:
            resource_values.extend(_scalar_values(argument_map[key]))
    # CommandExecution's parsed command is structured and may contain query,
    # cmd, and name values.  Only its path members are eligible for resource
    # fingerprints; none of the other members is copied or hashed.
    for candidate in item_candidates:
        parsed_cmd = candidate.get("parsed_cmd")
        if isinstance(parsed_cmd, (list, tuple)):
            for part in parsed_cmd:
                if isinstance(part, Mapping) and isinstance(part.get("path"), str):
                    resource_values.append(part["path"])
    resource_hashes: List[str] = []
    parsed_accesses: List[str] = []
    for resource in resource_values:
        fingerprint = resource_fingerprint(resource)
        if fingerprint and fingerprint not in resource_hashes:
            resource_hashes.append(fingerprint)
    for candidate in item_candidates:
        parsed_cmd = candidate.get("parsed_cmd")
        if not isinstance(parsed_cmd, (list, tuple)):
            continue
        for part in parsed_cmd:
            if not isinstance(part, Mapping):
                continue
            part_type = _safe_text(part.get("type"))
            if part_type is not None and part_type.lower() in {"read", "search", "list_files"}:
                parsed_accesses.append("read")
            elif part_type is not None:
                parsed_accesses.append("unknown")

    resource_access = "unknown"
    if parsed_accesses and all(access == "read" for access in parsed_accesses):
        resource_access = "read"

    exit_code = _safe_int(
        _first_value(item_candidates, ("exit_code", "exitCode", "return_code", "returnCode"))
    )
    if exit_code is None:
        exit_code = _safe_int(argument_map.get("exit_code"))

    activity_call_id = call_id if is_activity else None
    return {
        "call_id": call_id,
        "name": name,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration,
        "agent_type": agent_type,
        "fork_turns": normalize_fork_turns(fork_value),
        "task_name": task_name,
        "agent_path": agent_path,
        "command_fingerprint": command_fingerprint(command),
        "command_category": classify_command(command),
        "resource_fingerprints": tuple(resource_hashes),
        "resource_access": resource_access,
        "exit_code": exit_code,
        "success": None,
        "tool_kind": "subagent_activity" if is_activity else (item_type or kind),
        "activity_call_id": activity_call_id,
    }


def _extract_patch_apply_details(
    candidates: Sequence[Mapping[str, Any]], timestamp: Any
) -> Optional[Dict[str, Any]]:
    """Project only safe metadata from a patch_apply_end event."""

    call_id = _safe_identifier(_first_value(candidates, ("call_id", "callId")))
    status = _safe_text(_first_value(candidates, ("status", "state")))
    success = _safe_bool(_first_value(candidates, ("success",)))
    changes: Any = _first_value(candidates, ("changes",))
    resources: List[str] = []
    if isinstance(changes, Mapping):
        # Only mapping keys are file paths.  Do not inspect the values, which
        # may contain unified diffs or other raw tool content.
        for path in changes.keys():
            if isinstance(path, str):
                resources.append(path)
    fingerprints: List[str] = []
    for path in resources:
        fingerprint = resource_fingerprint(path)
        if fingerprint and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    if not call_id and not status and success is None and not fingerprints:
        return None
    return {
        "call_id": call_id,
        "name": "apply_patch",
        "status": status,
        "started_at": None,
        "ended_at": _safe_timestamp(timestamp),
        "duration_seconds": None,
        "agent_type": None,
        "fork_turns": "unknown",
        "task_name": None,
        "agent_path": None,
        "command_fingerprint": None,
        "command_category": None,
        "resource_fingerprints": tuple(fingerprints),
        "resource_access": "write",
        "exit_code": None,
        "success": success,
        "tool_kind": "patch_apply_end",
        "activity_call_id": None,
    }


def _span_from_details(details: Mapping[str, Any]) -> Optional[ToolSpan]:
    if not details:
        return None
    return ToolSpan(**dict(details))


def _merge_span(target: ToolSpan, update: ToolSpan) -> None:
    for name in (
        "call_id",
        "name",
        "status",
        "started_at",
        "ended_at",
        "duration_seconds",
        "agent_type",
        "task_name",
        "agent_path",
        "command_fingerprint",
        "command_category",
        "resource_access",
        "exit_code",
        "success",
        "tool_kind",
        "activity_call_id",
    ):
        value = getattr(update, name)
        if value is not None and (value != "unknown" or getattr(target, name) in (None, "unknown")):
            setattr(target, name, value)
    if update.fork_turns != "unknown":
        target.fork_turns = update.fork_turns
    if update.resource_fingerprints:
        merged = list(target.resource_fingerprints)
        for value in update.resource_fingerprints:
            if value not in merged:
                merged.append(value)
        target.resource_fingerprints = tuple(merged)
    if update.resource_access == "write" or target.resource_access == "unknown":
        target.resource_access = update.resource_access


def _match_fork_mode(parent: AgentTrace, child: AgentTrace) -> None:
    candidates = [span for span in parent.tool_spans if (span.name or "").lower() in {"spawn_agent", "spawnagent"}]
    if not candidates:
        return
    child_ids = set(child.activity_call_ids)
    child_names = set(child.activity_task_names)
    child_paths = set(child.activity_agent_paths)
    if child.task_name:
        child_names.add(child.task_name)
    if child.agent_path:
        child_paths.add(child.agent_path)
    for span in candidates:
        if span.call_id and span.call_id in child_ids:
            child.fork_mode = span.fork_turns
            return
        if span.task_name and span.task_name in child_names:
            child.fork_mode = span.fork_turns
            return
        if span.agent_path and span.agent_path in child_paths:
            child.fork_mode = span.fork_turns
            return


def _span_candidate_value(candidates: Sequence[Mapping[str, Any]], keys: Iterable[str]) -> Any:
    return _first_value(candidates, tuple(keys))


def _argument_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _duration_from(
    candidates: Sequence[Mapping[str, Any]], started_at: Any, ended_at: Any
) -> Optional[float]:
    value = _first_value(candidates, ("duration_seconds", "durationSeconds", "duration_ms", "durationMs", "duration"))
    if value is not None:
        if isinstance(value, Mapping):
            seconds = _safe_float(value.get("secs") or value.get("seconds"))
            nanos = _safe_float(value.get("nanos") or value.get("nanoseconds"))
            if seconds is not None or nanos is not None:
                return max(0.0, (seconds or 0.0) + (nanos or 0.0) / 1_000_000_000.0)
        parsed = _safe_float(value)
        if parsed is not None:
            if any(key in candidate for candidate in candidates for key in ("duration_ms", "durationMs")):
                parsed /= 1000.0
            return max(0.0, parsed)
    start = _timestamp_number(started_at)
    end = _timestamp_number(ended_at)
    if start is not None and end is not None and end >= start:
        return end - start
    return None


def _event_time_bounds(events: Sequence[_Event]) -> Tuple[Optional[Any], Optional[Any]]:
    """Return min/max safe event timestamps, ignoring unparseable values.

    Event timestamps are kept only as scalar values by the parser.  Comparing
    their parsed numeric form gives a stable chronology for ISO strings and
    epoch numbers while returning the original safe scalar for the public
    projection.  Filesystem timestamps are deliberately not consulted.
    """

    observed: List[Tuple[float, int, Any]] = []
    for index, event in enumerate(events):
        value = _safe_timestamp(event.timestamp)
        number = _timestamp_number(value)
        if number is not None:
            observed.append((number, index, value))
    if not observed:
        return None, None
    observed.sort(key=lambda item: (item[0], item[1]))
    return observed[0][2], observed[-1][2]


def _timestamp_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, OverflowError):
        return None


def _safe_timestamp(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        return _safe_text(value)[:128] if _safe_text(value) else None
    return None


def _safe_timestamp_ms(value: Any) -> Any:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return parsed / 1000.0


def _safe_event_name(value: Any) -> Optional[str]:
    text = _safe_text(value)
    return text[:80] if text else None


def _safe_identifier(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = _safe_text(value)
    return text or None


def _safe_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = _redact_path_tokens(str(value).strip())
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return text[:512] if text else None


def _safe_relative_path(value: Any) -> Optional[str]:
    return _model_safe_relative_path(value) or None


def _safe_project_name(value: Any) -> Optional[str]:
    """Return only the final cwd directory name, never its parent path."""
    return _model_safe_project_name(value)


def _safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_int(value: Any) -> int:
    parsed = _safe_int(value)
    return max(0, parsed or 0)


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1", "available"}:
            return True
        if text in {"false", "no", "0", "unavailable"}:
            return False
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return None


def _first_value(candidates: Sequence[Mapping[str, Any]], keys: Iterable[str]) -> Any:
    wanted = tuple(keys)
    for candidate in candidates:
        for key in wanted:
            if key in candidate and candidate[key] is not None:
                return candidate[key]
    return None


def _first_present(candidate: Mapping[str, Any], aliases: Iterable[str]) -> bool:
    return any(key in candidate and candidate[key] is not None for key in aliases)


def _mapping_value(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    for key in aliases:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _first_mapping(candidates: Sequence[Mapping[str, Any]], keys: Iterable[str]) -> Optional[Mapping[str, Any]]:
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, Mapping):
                return value
    return None


def _scalar_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        result: List[str] = []
        for item in value:
            result.extend(_scalar_values(item))
        return result
    if isinstance(value, Mapping):
        result = []
        for item in value.values():
            result.extend(_scalar_values(item))
        return result
    return []


def _append_unique(values: List[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _choose_root_file(directory: Path) -> Optional[Path]:
    preferred = directory / "root.jsonl"
    if preferred.is_file():
        return preferred
    candidates = _jsonl_files(directory)
    if not candidates:
        return None
    # A session named after the directory is a useful fixture convention.
    named = directory / (directory.name + ".jsonl")
    if named.is_file():
        return named
    return candidates[0]


def _jsonl_files(directory: Path) -> List[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    return sorted(
        (candidate for candidate in directory.rglob("*.jsonl") if candidate.is_file()),
        key=lambda candidate: str(candidate),
    )


def _find_sessions_root(root_file: Path) -> Optional[Path]:
    resolved = root_file.resolve()
    parts = resolved.parts
    for index, part in enumerate(parts):
        if part == "sessions":
            return Path(*parts[: index + 1])
    return None


__all__ = [
    "classify_command",
    "command_fingerprint",
    "fingerprint_command",
    "load_run",
    "normalize_command",
    "parse_session",
    "resource_fingerprint",
]
