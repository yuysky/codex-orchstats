"""Local session usage aggregation for ``orchstats usage``.

Only file metadata is used to select the requested time window.  Session
contents are immediately reduced by :func:`orchstats.parser.parse_session` to
the safe ``AgentTrace`` projection before aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .analysis import QuotaSummary
from .models import AgentTrace, RateLimitSnapshot, TokenUsage
from .parser import parse_session


DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
SUPPORTED_WINDOWS = ("7d", "24h")
ACCOUNT_SCOPE_LIMITATION = "quota observations are account-level/shared and not per-agent billing"


@dataclass
class UsageReport:
    """Safe aggregate of local session files in a requested time window."""

    session_count: int = 0
    total_token_usage: TokenUsage = field(default_factory=TokenUsage)
    quota: QuotaSummary = field(default_factory=QuotaSummary)
    limitations: List[str] = field(default_factory=list)
    since: Optional[str] = None

    @property
    def sessions(self) -> int:
        """Compatibility alias for callers that use the shorter field name."""

        return self.session_count

    @property
    def tokens(self) -> TokenUsage:
        """Compatibility alias for callers that use the shorter field name."""

        return self.total_token_usage

    @property
    def token_usage(self) -> TokenUsage:
        """Compatibility alias matching the per-session model naming."""

        return self.total_token_usage

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_count": max(0, int(self.session_count)),
            "total_token_usage": _token_dict(self.total_token_usage),
            "quota": self.quota.to_dict(),
            "limitations": [str(value) for value in self.limitations],
            "scope": "account-level/shared; not per-agent billing",
            "since": self.since,
        }


@dataclass
class _QuotaObservation:
    snapshot: RateLimitSnapshot
    order: int
    key: str


def build_usage_report(
    sessions_root: Optional[Any] = None,
    since: str = "7d",
    *,
    now: Optional[float] = None,
) -> UsageReport:
    """Parse recent JSONL files and aggregate token/quota observations.

    ``now`` is injectable for deterministic tests and otherwise uses the
    current local clock.  The caller never receives the selected file paths.
    """

    root = Path(sessions_root) if sessions_root is not None else DEFAULT_SESSIONS_ROOT
    window_seconds = _window_seconds(since)
    current_time = float(now) if now is not None else _time_now()
    cutoff = current_time - window_seconds

    paths = _recent_jsonl_files(root, cutoff)
    report = UsageReport(since=since)
    if not paths:
        report.limitations.extend(
            [
                "no local session files found in the requested window",
                "quota observations unavailable",
                ACCOUNT_SCOPE_LIMITATION,
            ]
        )
        return report

    observations: List[_QuotaObservation] = []
    failed = 0
    for order, path in enumerate(paths):
        try:
            trace = parse_session(path)
        except (OSError, TypeError, ValueError, UnicodeError):
            failed += 1
            continue
        report.session_count += 1
        report.total_token_usage = report.total_token_usage + _trace_tokens(trace)
        observations.extend(_trace_quota_observations(trace, order))

    if failed:
        report.limitations.append("some local session files were unavailable")
    quota, quota_limitations = _summarize_quota(observations)
    report.quota = quota
    report.limitations.extend(quota_limitations)
    report.limitations.append(ACCOUNT_SCOPE_LIMITATION)
    report.limitations = _unique_text(report.limitations)
    return report


def recent_jsonl_files(
    sessions_root: Optional[Any] = None,
    since: str = "7d",
    *,
    now: Optional[float] = None,
) -> List[Path]:
    """Expose the local selection helper for watch/tests without raw output."""

    root = Path(sessions_root) if sessions_root is not None else DEFAULT_SESSIONS_ROOT
    cutoff = (float(now) if now is not None else _time_now()) - _window_seconds(since)
    return _recent_jsonl_files(root, cutoff)


def _recent_jsonl_files(root: Path, cutoff: float) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    result: List[Tuple[float, str, Path]] = []
    try:
        candidates = root.rglob("*.jsonl")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified >= cutoff:
                result.append((modified, str(path), path))
    except OSError:
        return []
    # Stable path ordering makes synthetic fixtures deterministic while the
    # mtime remains the only window-selection criterion.
    result.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in result]


def _trace_tokens(trace: AgentTrace) -> TokenUsage:
    value = getattr(trace, "token_usage", None)
    if isinstance(value, TokenUsage):
        return TokenUsage(**value.to_dict())
    return TokenUsage(
        **{
            name: getattr(value, name, 0) if value is not None else 0
            for name in TokenUsage._FIELDS
        }
    )


def _trace_quota_observations(trace: AgentTrace, order: int) -> List[_QuotaObservation]:
    first = getattr(trace, "rate_limits_first", {})
    last = getattr(trace, "rate_limits_last", {})
    if not isinstance(first, Mapping):
        first = {}
    if not isinstance(last, Mapping):
        last = {}
    result: List[_QuotaObservation] = []
    seen: set = set()
    for endpoint, values in ((0, first), (1, last)):
        for name, snapshot in sorted(values.items(), key=lambda item: str(item[0])):
            if not isinstance(snapshot, RateLimitSnapshot):
                continue
            key = _quota_key(name)
            if key is None:
                continue
            # A one-snapshot session has the same object represented as both
            # first and last.  Do not turn that into a fake zero delta.
            marker = (key, repr(snapshot.to_dict()))
            if marker in seen:
                continue
            seen.add(marker)
            result.append(_QuotaObservation(snapshot=snapshot, order=order * 2 + endpoint, key=key))
    return result


def _summarize_quota(
    observations: Sequence[_QuotaObservation],
) -> Tuple[QuotaSummary, List[str]]:
    if not observations:
        return QuotaSummary(), ["quota observations unavailable"]

    grouped: Dict[str, List[_QuotaObservation]] = {}
    for item in observations:
        grouped.setdefault(item.key, []).append(item)
    key = sorted(grouped, key=lambda value: (-len(grouped[value]), value))[0]
    stream = grouped[key]
    timed: List[Tuple[float, _QuotaObservation]] = []
    untimed = 0
    for item in stream:
        value = _timestamp_number(getattr(item.snapshot, "observed_at", None))
        if value is None:
            untimed += 1
        else:
            timed.append((value, item))
    timed.sort(key=lambda pair: (pair[0], pair[1].order))
    limitations: List[str] = []
    if untimed:
        limitations.append("some quota snapshot timestamps unavailable")

    if timed:
        earliest = timed[0][1].snapshot
        latest = timed[-1][1].snapshot
    else:
        stream_sorted = sorted(stream, key=lambda item: item.order)
        earliest = stream_sorted[0].snapshot
        latest = stream_sorted[-1].snapshot
        if len(stream_sorted) > 1:
            limitations.append("quota snapshot timestamps unavailable; endpoint order is a stable fallback")

    delta: Optional[float] = None
    if len(timed) >= 2:
        first_value = _float_or_none(getattr(earliest, "used_percent", None))
        latest_value = _float_or_none(getattr(latest, "used_percent", None))
        if first_value is not None and latest_value is not None:
            delta = latest_value - first_value
    elif len(stream) >= 2:
        limitations.append("observed quota delta unavailable without comparable timestamps")

    current = _float_or_none(getattr(latest, "used_percent", None))
    evidence = "unavailable"
    if current is not None:
        evidence = "derived" if delta is not None else "observed"
    return (
        QuotaSummary(
            plan_type=_safe_optional_text(getattr(latest, "plan_type", None)),
            current_used_percent=current,
            observed_delta_percent=delta,
            window_minutes=_nonnegative_int_or_none(getattr(latest, "window_minutes", None)),
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
    if lowered in {"primary", "codex", "codex-primary", "primary-codex"}:
        return "primary"
    if "primary" in lowered or "codex" in lowered:
        return "primary"
    return None


def _token_dict(value: Any) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for name in TokenUsage._FIELDS:
        raw = value.get(name, 0) if isinstance(value, Mapping) else getattr(value, name, 0)
        try:
            result[name] = max(0, int(raw))
        except (TypeError, ValueError):
            result[name] = 0
    return result


def _window_seconds(value: str) -> int:
    if value == "7d":
        return 7 * 24 * 60 * 60
    if value == "24h":
        return 24 * 60 * 60
    raise ValueError("unsupported usage window")


def _time_now() -> float:
    return datetime.now().timestamp()


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


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _safe_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)[:512]
    if "SECRET_SENTINEL" in text:
        text = text.replace("SECRET_SENTINEL", "<redacted>")
    if text.startswith("/") or text.startswith("~"):
        return "<redacted>"
    return text


def _safe_timestamp_output(value: Any) -> Optional[Any]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    text = value[:128]
    return text if _timestamp_number(text) is not None else None


def _unique_text(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


__all__ = [
    "DEFAULT_SESSIONS_ROOT",
    "SUPPORTED_WINDOWS",
    "UsageReport",
    "build_usage_report",
    "recent_jsonl_files",
]
