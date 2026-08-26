"""Small, privacy-preserving data model for Codex orchestration traces.

The parser deliberately turns session records into these models at the
boundary.  None of the models has a field for prompts, reasoning, tool
outputs, commands, or account balances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional, Tuple, Union


class EvidenceLevel(str, Enum):
    """How directly a value is supported by local evidence."""

    OBSERVED = "observed"
    DERIVED = "derived"
    HEURISTIC = "heuristic"
    UNAVAILABLE = "unavailable"


Number = Union[int, float]


@dataclass
class TokenUsage:
    """Token counters represented by a token-count cumulative snapshot.

    A token-count event is cumulative within a segment, but the upstream
    counter can reset.  ``non_negative_delta`` therefore compares counters
    field by field and treats a decrease as a new segment whose current value
    is added in full.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    _FIELDS: ClassVar[Tuple[str, ...]] = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            value = getattr(self, name)
            # Counts are integral in the trace schema.  Be lenient about
            # numeric strings and floats emitted by synthetic/export tools,
            # but never let a negative count enter the model.
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
            setattr(self, name, max(0, value))

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self._FIELDS
            }
        )

    def __radd__(self, other: Any) -> "TokenUsage":
        if other == 0:
            return self
        return self.__add__(other)

    def non_negative_delta(self, previous: Optional["TokenUsage"] = None) -> "TokenUsage":
        """Return the positive delta from ``previous``.

        If a counter decreased, the current value is treated as the first
        value of a new segment.  This is intentionally independent per field:
        one counter may reset while the others continue.
        """

        if previous is None:
            return TokenUsage(**{name: getattr(self, name) for name in self._FIELDS})
        values: Dict[str, int] = {}
        for name in self._FIELDS:
            current = getattr(self, name)
            before = getattr(previous, name)
            values[name] = current if current < before else current - before
        return TokenUsage(**values)

    # ``delta`` is a convenient public alias used by consumers that do not
    # need to distinguish the reset-aware implementation by name.
    def delta(self, previous: Optional["TokenUsage"] = None) -> "TokenUsage":
        return self.non_negative_delta(previous)

    def __sub__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return self.non_negative_delta(other)

    def to_dict(self) -> Dict[str, int]:
        return {name: getattr(self, name) for name in self._FIELDS}


@dataclass
class RateLimitSnapshot:
    """Safe rate-limit metadata.

    In particular, this type intentionally has no ``balance`` field.  Credit
    availability is represented only as the boolean ``has_credits``.
    """

    used_percent: Optional[float] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[Union[str, int, float]] = None
    observed_at: Optional[Union[str, int, float]] = None
    plan_type: Optional[str] = None
    has_credits: Optional[bool] = None
    unlimited: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.used_percent is not None:
            try:
                self.used_percent = float(self.used_percent)
                if not math.isfinite(self.used_percent):
                    self.used_percent = None
            except (TypeError, ValueError):
                self.used_percent = None
        if self.window_minutes is not None:
            try:
                self.window_minutes = int(self.window_minutes)
            except (TypeError, ValueError):
                self.window_minutes = None
        for name in ("resets_at", "observed_at"):
            value = getattr(self, name)
            if isinstance(value, str):
                setattr(self, name, _safe_scalar_string(value)[:128])
            elif value is not None and not isinstance(value, (int, float)):
                setattr(self, name, None)
        if self.plan_type is not None:
            self.plan_type = _safe_scalar_string(self.plan_type)
        if self.has_credits is not None:
            self.has_credits = bool(self.has_credits)
        if self.unlimited is not None:
            self.unlimited = bool(self.unlimited)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "observed_at": self.observed_at,
            "plan_type": self.plan_type,
            "has_credits": self.has_credits,
            "unlimited": self.unlimited,
        }


@dataclass
class ToolSpan:
    """Safe metadata for one function/custom tool or command invocation."""

    call_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[Union[str, int, float]] = None
    ended_at: Optional[Union[str, int, float]] = None
    duration_seconds: Optional[float] = None
    agent_type: Optional[str] = None
    fork_turns: str = "unknown"
    task_name: Optional[str] = None
    agent_path: Optional[str] = None
    command_fingerprint: Optional[str] = None
    command_category: Optional[str] = None
    resource_fingerprints: Tuple[str, ...] = ()
    resource_access: str = "unknown"
    exit_code: Optional[int] = None
    success: Optional[bool] = None
    tool_kind: Optional[str] = None
    activity_call_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.started_at = _safe_timestamp_value(self.started_at)
        self.ended_at = _safe_timestamp_value(self.ended_at)
        if self.call_id is not None:
            self.call_id = _safe_scalar_string(self.call_id)
        if self.name is not None:
            self.name = _safe_scalar_string(self.name)
        if self.status is not None:
            self.status = _safe_scalar_string(self.status)
        if self.agent_type is not None:
            self.agent_type = _safe_scalar_string(self.agent_type)
        if self.task_name is not None:
            self.task_name = _safe_scalar_string(self.task_name)
        if self.agent_path is not None:
            self.agent_path = _safe_relative_path(self.agent_path)
        if self.command_fingerprint is not None:
            self.command_fingerprint = _safe_fingerprint(self.command_fingerprint)
        if self.command_category is not None:
            self.command_category = _safe_scalar_string(self.command_category)
        if self.resource_access not in {"read", "write", "unknown"}:
            self.resource_access = "unknown"
        if self.success is not None:
            self.success = bool(self.success)
        if self.tool_kind is not None:
            self.tool_kind = _safe_scalar_string(self.tool_kind)
        if self.activity_call_id is not None:
            self.activity_call_id = _safe_scalar_string(self.activity_call_id)
        if self.duration_seconds is not None:
            try:
                self.duration_seconds = float(self.duration_seconds)
            except (TypeError, ValueError):
                self.duration_seconds = None
        if self.exit_code is not None:
            try:
                self.exit_code = int(self.exit_code)
            except (TypeError, ValueError):
                self.exit_code = None
        if isinstance(self.resource_fingerprints, str):
            self.resource_fingerprints = (self.resource_fingerprints,)
        elif not isinstance(self.resource_fingerprints, tuple):
            self.resource_fingerprints = tuple(self.resource_fingerprints or ())
        self.resource_fingerprints = tuple(
            fingerprint
            for value in self.resource_fingerprints
            for fingerprint in (_safe_fingerprint(value),)
            if fingerprint is not None
        )
        self.fork_turns = normalize_fork_turns(self.fork_turns)

    @property
    def duration(self) -> Optional[float]:
        """Short alias for consumers that call the field simply duration."""

        return self.duration_seconds

    @property
    def duration_ms(self) -> Optional[float]:
        return self.duration_seconds * 1000.0 if self.duration_seconds is not None else None

    @property
    def command_hash(self) -> Optional[str]:
        return self.command_fingerprint

    @property
    def command_sha256(self) -> Optional[str]:
        return self.command_fingerprint

    @property
    def resources(self) -> Tuple[str, ...]:
        return self.resource_fingerprints

    @property
    def start_time(self) -> Optional[Union[str, int, float]]:
        return self.started_at

    @property
    def end_time(self) -> Optional[Union[str, int, float]]:
        return self.ended_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "agent_type": self.agent_type,
            "fork_turns": self.fork_turns,
            "task_name": self.task_name,
            "agent_path": self.agent_path,
            "command_fingerprint": self.command_fingerprint,
            "command_category": self.command_category,
            "resource_fingerprints": list(self.resource_fingerprints),
            "resource_access": self.resource_access,
            "exit_code": self.exit_code,
            "success": self.success,
            "tool_kind": self.tool_kind,
            "activity_call_id": self.activity_call_id,
        }


@dataclass
class ParseWarning:
    """A non-fatal issue encountered while reading a JSONL trace."""

    kind: str
    message: str
    line_number: Optional[int] = None
    event_type: Optional[str] = None

    @property
    def code(self) -> str:
        return self.kind

    @property
    def line(self) -> Optional[int]:
        return self.line_number

    def __post_init__(self) -> None:
        self.kind = _safe_scalar_string(self.kind)
        # Messages are generated by the parser and never include source
        # content.  Keep the model safe even when callers construct one.
        self.message = _safe_scalar_string(self.message)
        if self.event_type is not None:
            self.event_type = _safe_scalar_string(self.event_type)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "line_number": self.line_number,
            "event_type": self.event_type,
        }


@dataclass
class AgentTrace:
    """The safe, derived representation of one session JSONL file."""

    session_id: Optional[str] = None
    parent_thread_id: Optional[str] = None
    role: Optional[str] = None
    # Final directory name derived from the observed session cwd.  The raw
    # cwd is never retained, so this remains useful for local grouping without
    # carrying an absolute filesystem path beyond the parser boundary.
    project_name: Optional[str] = None
    path: Optional[str] = None
    depth: Optional[int] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    rate_limits_first: Dict[str, RateLimitSnapshot] = field(default_factory=dict)
    rate_limits_last: Dict[str, RateLimitSnapshot] = field(default_factory=dict)
    tool_spans: List[ToolSpan] = field(default_factory=list)
    warnings: List[ParseWarning] = field(default_factory=list)
    fork_mode: str = "unknown"
    task_name: Optional[str] = None
    agent_path: Optional[str] = None
    activity_call_ids: Tuple[str, ...] = ()
    activity_task_names: Tuple[str, ...] = ()
    activity_agent_paths: Tuple[str, ...] = ()
    # ``identity_observed`` records whether the id came from an explicit
    # nested ``session_meta`` payload.  The historical index must not use a
    # filename/stem fallback as an identity, while the older ``load_run``
    # compatibility path may still do so internally.
    identity_observed: bool = False
    # ``parent_present`` distinguishes an explicit ``parent_thread_id: null``
    # root from a session whose parent metadata was absent entirely.
    parent_present: bool = False
    # These bounds are derived only from safe event timestamps.  They are
    # intentionally separate from filesystem metadata such as mtime.
    observed_start_at: Optional[Union[str, int, float]] = None
    observed_end_at: Optional[Union[str, int, float]] = None

    def __post_init__(self) -> None:
        """Normalize values when a safe trace is constructed directly.

        The parser is the normal boundary, but tests and local callers also
        construct these dataclasses.  Applying the same projection here keeps
        a direct model instance from becoming a side channel for paths,
        unbounded text, or malformed fingerprints.
        """

        self.session_id = _safe_identifier(self.session_id)
        self.parent_thread_id = _safe_identifier(self.parent_thread_id)
        self.role = _safe_optional_scalar(self.role)
        self.project_name = _safe_project_name(self.project_name)
        self.path = _safe_relative_path(self.path)
        self.depth = _safe_nonnegative_int_or_none(self.depth)
        self.model = _safe_optional_scalar(self.model)
        self.effort = _safe_optional_scalar(self.effort)
        if not isinstance(self.token_usage, TokenUsage):
            if isinstance(self.token_usage, Mapping):
                self.token_usage = TokenUsage(
                    **{
                        name: self.token_usage.get(name, 0)
                        for name in TokenUsage._FIELDS
                    }
                )
            else:
                self.token_usage = TokenUsage()

        self.rate_limits_first = _safe_rate_map(self.rate_limits_first)
        self.rate_limits_last = _safe_rate_map(self.rate_limits_last)
        spans: List[ToolSpan] = []
        for value in self.tool_spans or ():
            span = _coerce_tool_span(value)
            if span is not None:
                spans.append(span)
        self.tool_spans = spans
        parsed_warnings: List[ParseWarning] = []
        for value in self.warnings or ():
            warning = _coerce_warning(value)
            if warning is not None:
                parsed_warnings.append(warning)
        self.warnings = parsed_warnings
        self.fork_mode = normalize_fork_turns(self.fork_mode)
        self.task_name = _safe_optional_scalar(self.task_name)
        self.agent_path = _safe_relative_path(self.agent_path)
        self.activity_call_ids = _safe_string_tuple(self.activity_call_ids)
        self.activity_task_names = _safe_string_tuple(self.activity_task_names)
        self.activity_agent_paths = _safe_path_tuple(self.activity_agent_paths)
        self.identity_observed = bool(self.identity_observed)
        self.parent_present = bool(self.parent_present)
        self.observed_start_at = _safe_timestamp_value(self.observed_start_at)
        self.observed_end_at = _safe_timestamp_value(self.observed_end_at)

    # Compatibility alias: consumers interested in quota deltas generally
    # want the latest observed snapshot, while the first/last maps retain both
    # endpoints for later analysis.
    @property
    def rate_limits(self) -> Dict[str, RateLimitSnapshot]:
        return self.rate_limits_last

    @property
    def rate_limit_first(self) -> Dict[str, RateLimitSnapshot]:
        return self.rate_limits_first

    @property
    def rate_limit_last(self) -> Dict[str, RateLimitSnapshot]:
        return self.rate_limits_last

    @property
    def id(self) -> Optional[str]:
        return self.session_id

    @property
    def total_token_usage(self) -> TokenUsage:
        return self.token_usage

    @property
    def tokens(self) -> TokenUsage:
        return self.token_usage

    @property
    def fork_turns(self) -> str:
        return self.fork_mode

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parent_thread_id": self.parent_thread_id,
            "identity_observed": bool(self.identity_observed),
            "parent_present": bool(self.parent_present),
            "observed_start_at": self.observed_start_at,
            "observed_end_at": self.observed_end_at,
            "role": self.role,
            "project_name": self.project_name,
            "path": self.path,
            "depth": self.depth,
            "model": self.model,
            "effort": self.effort,
            "token_usage": self.token_usage.to_dict(),
            "rate_limits_first": {
                key: value.to_dict() for key, value in self.rate_limits_first.items()
            },
            "rate_limits_last": {
                key: value.to_dict() for key, value in self.rate_limits_last.items()
            },
            "tool_spans": [span.to_dict() for span in self.tool_spans],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "fork_mode": self.fork_mode,
            "task_name": self.task_name,
            "agent_path": self.agent_path,
            "activity_call_ids": list(self.activity_call_ids),
            "activity_task_names": list(self.activity_task_names),
            "activity_agent_paths": list(self.activity_agent_paths),
        }


@dataclass
class RunTrace:
    """A root session and the discovered descendant session tree."""

    root: AgentTrace
    sessions: Dict[str, AgentTrace] = field(default_factory=dict)
    children: Dict[str, List[AgentTrace]] = field(default_factory=dict)
    warnings: List[ParseWarning] = field(default_factory=list)

    @property
    def root_trace(self) -> AgentTrace:
        return self.root

    @property
    def root_session(self) -> AgentTrace:
        return self.root

    @property
    def traces(self) -> List[AgentTrace]:
        return list(self.sessions.values())

    def descendants(self, session_id: Optional[str]) -> List[AgentTrace]:
        result: List[AgentTrace] = []
        for child in self.children.get(session_id or "", ()):
            result.append(child)
            result.extend(self.descendants(child.session_id))
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "sessions": {
                key: value.to_dict() for key, value in self.sessions.items()
            },
            "children": {
                key: [value.session_id for value in values]
                for key, values in self.children.items()
            },
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def normalize_fork_turns(value: Any) -> str:
    """Normalize the supported fork modes to ``none/all/recent_n/unknown``."""

    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, int):
        return "recent_%d" % value if 0 <= value <= 999999 else "unknown"
    text = str(value).strip().lower()
    if text in {"none", "all"}:
        return text
    if text.startswith("recent_"):
        suffix = text[len("recent_") :]
        if suffix.isdigit() and len(suffix) <= 6 and int(suffix) <= 999999:
            return "recent_%d" % int(suffix)
    if text.startswith("recent-"):
        suffix = text[len("recent-") :]
        if suffix.isdigit() and len(suffix) <= 6 and int(suffix) <= 999999:
            return "recent_%d" % int(suffix)
    if text.startswith("recent:"):
        suffix = text[len("recent:") :]
        if suffix.isdigit() and len(suffix) <= 6 and int(suffix) <= 999999:
            return "recent_%d" % int(suffix)
    if text.startswith("recent "):
        suffix = text[len("recent ") :]
        if suffix.isdigit() and len(suffix) <= 6 and int(suffix) <= 999999:
            return "recent_%d" % int(suffix)
    if text.isdigit() and len(text) <= 6 and int(text) <= 999999:
        return "recent_%d" % int(text)
    return "unknown"


def _safe_scalar_string(value: Any) -> str:
    """Convert a scalar to a short, non-structured string."""

    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    else:
        text = str(value)
    text = _redact_path_tokens(text)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return text[:512]


def _safe_relative_path(value: Any) -> str:
    """Preserve relative agent paths but redact filesystem-like paths."""

    text = _safe_scalar_string(value)
    if _looks_absolute_path(text):
        return "<redacted>"
    return _redact_path_tokens(text)[:512]


_PATH_TOKEN_END = r"[^\s\"'`,;|&<>()]+"


def _looks_absolute_path(value: Any) -> bool:
    """Return whether a scalar is an absolute POSIX/Windows/file URI path."""

    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(
        text == "~"
        or text.startswith("/")
        or text.startswith("~")
        or re.match(r"(?i)^file://", text)
        or re.match(r"^[A-Za-z]:[\\/]", text)
        or text.startswith("\\\\")
        or text.startswith("//")
    )


def _redact_path_tokens(value: Any, replacement: str = "<redacted>") -> str:
    """Replace absolute path tokens without interpreting shell syntax."""

    text = "" if value is None else str(value)
    if not text:
        return text
    # URI forms come first so their ``//`` portion is not handled separately.
    text = re.sub(r"(?i)file://" + _PATH_TOKEN_END, replacement, text)
    text = re.sub(
        r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/])" + _PATH_TOKEN_END,
        replacement,
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_:/])(?:\\\\|//)" + _PATH_TOKEN_END,
        replacement,
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_])~(?:[\\/]|[A-Za-z0-9_.-]+[\\/])" + _PATH_TOKEN_END,
        replacement,
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_:/])/(?!/)" + _PATH_TOKEN_END,
        replacement,
        text,
    )
    if text.strip() == "~":
        return replacement
    return text


def _safe_optional_scalar(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = _safe_scalar_string(value)
    return text or None


def _safe_identifier(value: Any) -> Optional[str]:
    return _safe_optional_scalar(value)


def _safe_fingerprint(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        return None
    return text


def _safe_nonnegative_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_timestamp_value(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        return _safe_scalar_string(value)[:128]
    return None


def _safe_rate_map(value: Any) -> Dict[str, RateLimitSnapshot]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, RateLimitSnapshot] = {}
    for key, snapshot in value.items():
        name = _safe_optional_scalar(key)
        if not name:
            continue
        if isinstance(snapshot, RateLimitSnapshot):
            result[name] = snapshot
        elif isinstance(snapshot, Mapping):
            result[name] = RateLimitSnapshot(
                used_percent=snapshot.get("used_percent"),
                window_minutes=snapshot.get("window_minutes"),
                resets_at=snapshot.get("resets_at"),
                observed_at=snapshot.get("observed_at"),
                plan_type=snapshot.get("plan_type"),
                has_credits=snapshot.get("has_credits"),
                unlimited=snapshot.get("unlimited"),
            )
    return result


def _coerce_tool_span(value: Any) -> Optional[ToolSpan]:
    if isinstance(value, ToolSpan):
        return value
    if not isinstance(value, Mapping):
        return None
    names = {
        "call_id",
        "name",
        "status",
        "started_at",
        "ended_at",
        "duration_seconds",
        "agent_type",
        "fork_turns",
        "task_name",
        "agent_path",
        "command_fingerprint",
        "command_category",
        "resource_fingerprints",
        "resource_access",
        "exit_code",
        "success",
        "tool_kind",
        "activity_call_id",
    }
    return ToolSpan(**{name: value[name] for name in names if name in value})


def _coerce_warning(value: Any) -> Optional[ParseWarning]:
    if isinstance(value, ParseWarning):
        return value
    if not isinstance(value, Mapping):
        return None
    return ParseWarning(
        kind=value.get("kind") or "unknown",
        message=value.get("message") or "unavailable",
        line_number=value.get("line_number"),
        event_type=value.get("event_type"),
    )


def _safe_string_tuple(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(
        text
        for item in value
        for text in (_safe_optional_scalar(item),)
        if text
    )


def _safe_path_tuple(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(
        text
        for item in value
        for text in (_safe_relative_path(item),)
        if text
    )


def _safe_project_name(value: Any) -> Optional[str]:
    """Keep only a cwd's final directory name, never its parent path."""

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    if not text or text == "~":
        return None
    text = re.sub(r"(?i)^file://", "", text)
    trimmed = text.rstrip("/\\")
    if not trimmed or re.fullmatch(r"[A-Za-z]:", trimmed):
        return None
    leaf = re.split(r"[/\\]+", trimmed)[-1].strip()
    leaf = re.sub(r"[\x00-\x1f\x7f]", "", leaf).strip()
    if not leaf or leaf in {".", ".."}:
        return None
    return _redact_path_tokens(leaf)[:96] or None


__all__ = [
    "AgentTrace",
    "EvidenceLevel",
    "ParseWarning",
    "RateLimitSnapshot",
    "RunTrace",
    "TokenUsage",
    "ToolSpan",
    "normalize_fork_turns",
]
