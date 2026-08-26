"""Small, privacy-safe renderers for the orchstats command line interface.

The parser and analysis modules own the data model.  This module only turns
their safe projections into JSON, text, or Markdown.  In particular, it does
not accept raw JSONL records and therefore has no reason to print commands,
paths, or trace identifiers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .analysis import RunAnalysis
from .diagnostics import Diagnostic
from .models import TokenUsage


PUBLIC_SCOPE = "account-level/shared; not per-agent billing"
PUBLIC_ANALYSIS_KEYS = (
    "schema_version",
    "agents",
    "total_token_usage",
    "quota",
    "diagnostics",
    "limitations",
)


def public_analysis_dict(analysis: RunAnalysis) -> Dict[str, Any]:
    """Return the stable public JSON projection of an analysis.

    ``RunAnalysis.to_dict`` is already safe today.  Rebuilding the projection
    here keeps the CLI contract explicit if the internal dataclasses grow more
    fields later.
    """

    raw = analysis.to_dict()
    agents: List[Dict[str, Any]] = []
    for item in raw.get("agents", ()) or ():
        if not isinstance(item, Mapping):
            continue
        agents.append(
            {
                "label": _public_text(item.get("label")),
                "role": _public_text(item.get("role")),
                "model": _public_text(item.get("model")),
                "effort": _public_text(item.get("effort")),
                "fork_mode": _public_text(item.get("fork_mode")),
                "token_usage": _public_token_usage(item.get("token_usage")),
                "tool_count": _nonnegative_int(item.get("tool_count")),
            }
        )

    quota_raw = raw.get("quota")
    quota = _public_quota(quota_raw if isinstance(quota_raw, Mapping) else {})
    diagnostics = [_public_diagnostic(item) for item in raw.get("diagnostics", ()) or ()]
    limitations = [
        _public_text(item) or ""
        for item in raw.get("limitations", ()) or ()
    ]
    return {
        "schema_version": _public_text(raw.get("schema_version")) or "0.1",
        "agents": agents,
        "total_token_usage": _public_token_usage(raw.get("total_token_usage")),
        "quota": quota,
        "diagnostics": diagnostics,
        "limitations": limitations,
    }


def public_diagnostics(analysis_or_diagnostics: Any) -> List[Dict[str, Any]]:
    """Return only diagnostics in the public projection."""

    if isinstance(analysis_or_diagnostics, RunAnalysis):
        values = analysis_or_diagnostics.diagnostics
    else:
        values = analysis_or_diagnostics or ()
    return [_public_diagnostic(item) for item in values]


def dumps_json(value: Any) -> str:
    """Serialize a report using the CLI's deterministic JSON style."""

    import json

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_analysis(analysis: RunAnalysis, output_format: str = "text") -> str:
    """Render an analysis in ``text``, ``json``, or ``markdown`` format."""

    public = public_analysis_dict(analysis)
    if output_format == "json":
        return dumps_json(public)
    if output_format == "markdown":
        return _render_analysis_markdown(public)
    return _render_analysis_text(public)


def render_lint(analysis: RunAnalysis, output_format: str = "text") -> str:
    """Render the diagnostics-only view used by ``orchstats lint``."""

    diagnostics = public_diagnostics(analysis)
    if output_format == "json":
        return dumps_json({"diagnostics": diagnostics})
    if output_format == "markdown":
        return _render_diagnostics_markdown(diagnostics)
    return _render_diagnostics_text(diagnostics)


def render_usage(report: Any, output_format: str = "text") -> str:
    """Render a :class:`~orchstats.usage.UsageReport` instance."""

    public = report.to_dict()
    if output_format == "json":
        return dumps_json(public)
    if output_format == "markdown":
        return _render_usage_markdown(public)
    return _render_usage_text(public)


def render_watch(analysis: Optional[RunAnalysis]) -> str:
    """Render the intentionally compact local watch snapshot."""

    if analysis is None:
        return "Watch snapshot: unavailable (no local JSONL session found).\n"
    public = public_analysis_dict(analysis)
    total = public["total_token_usage"]
    quota = public["quota"]
    lines = [
        "Watch snapshot",
        "Agents: %d" % len(public["agents"]),
        "Total token usage: %s" % _compact_tokens(total),
        "Quota (%s):" % PUBLIC_SCOPE,
        "  current used: %s" % _percent(quota.get("current_used_percent")),
        "  observed delta: %s" % _delta(quota.get("observed_delta_percent")),
        "  window: %s" % _window(quota.get("window_minutes")),
        "  reset: %s" % _display_or_unavailable(quota.get("resets_at")),
        "  plan_type: %s" % _display_or_unavailable(quota.get("plan_type")),
        "  has_credits: %s" % _bool_or_unavailable(quota.get("has_credits")),
        "",
    ]
    return "\n".join(lines)


def _render_analysis_text(public: Mapping[str, Any]) -> str:
    lines = [
        "Orchestration analysis",
        "Schema version: %s" % public.get("schema_version", "0.1"),
        "",
        "Agents: %d" % len(public.get("agents", ()) or ()),
    ]
    for item in public.get("agents", ()) or ():
        lines.append(
            "  %s role=%s model=%s effort=%s fork=%s tools=%s tokens=%s"
            % (
                _display_or_unavailable(item.get("label")),
                _display_or_unavailable(item.get("role")),
                _display_or_unavailable(item.get("model")),
                _display_or_unavailable(item.get("effort")),
                _display_or_unavailable(item.get("fork_mode")),
                item.get("tool_count", 0),
                _compact_tokens(item.get("token_usage", {})),
            )
        )
    lines.extend(
        [
            "",
            "Total token usage:",
            *_token_lines(public.get("total_token_usage", {}), "  "),
            "",
            "Quota (%s):" % PUBLIC_SCOPE,
            *_quota_lines(public.get("quota", {}), "  "),
            "",
            "Diagnostics:",
        ]
    )
    lines.extend(_diagnostic_lines(public.get("diagnostics", ()) or (), "  "))
    lines.extend(["", "Limitations:"])
    lines.extend(_limitation_lines(public.get("limitations", ()) or (), "  "))
    return "\n".join(lines) + "\n"


def _render_diagnostics_text(diagnostics: Sequence[Mapping[str, Any]]) -> str:
    lines = ["Diagnostics:"]
    lines.extend(_diagnostic_lines(diagnostics, "  "))
    return "\n".join(lines) + "\n"


def _render_usage_text(public: Mapping[str, Any]) -> str:
    lines = [
        "Usage report (%s)" % PUBLIC_SCOPE,
        "Sessions: %s" % public.get("session_count", 0),
    ]
    if public.get("since"):
        lines.append("Window: %s" % public["since"])
    lines.extend(
        [
            "",
            "Token usage:",
            *_token_lines(public.get("total_token_usage", {}), "  "),
            "",
            "Quota (%s):" % PUBLIC_SCOPE,
            *_quota_lines(public.get("quota", {}), "  "),
            "",
            "Limitations:",
        ]
    )
    lines.extend(_limitation_lines(public.get("limitations", ()) or (), "  "))
    return "\n".join(lines) + "\n"


def _render_analysis_markdown(public: Mapping[str, Any]) -> str:
    lines = [
        "# Orchestration analysis",
        "",
        "Schema version: `%s`" % public.get("schema_version", "0.1"),
        "",
        "## Agents",
        "",
        "| Agent | Role | Model | Effort | Fork | Tools | Total tokens |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for item in public.get("agents", ()) or ():
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                _md(item.get("label")),
                _md(item.get("role")),
                _md(item.get("model")),
                _md(item.get("effort")),
                _md(item.get("fork_mode")),
                item.get("tool_count", 0),
                item.get("token_usage", {}).get("total_tokens", 0),
            )
        )
    lines.extend(["", "## Total token usage", ""])
    lines.extend(_markdown_token_table(public.get("total_token_usage", {})))
    lines.extend(["", "## Quota (%s)" % PUBLIC_SCOPE, ""])
    lines.extend(_markdown_quota_table(public.get("quota", {})))
    lines.extend(["", "## Diagnostics", ""])
    lines.extend(_diagnostic_markdown_lines(public.get("diagnostics", ()) or ()))
    lines.extend(["", "## Limitations", ""])
    lines.extend(_limitation_lines(public.get("limitations", ()) or (), "- "))
    return "\n".join(lines) + "\n"


def _render_diagnostics_markdown(diagnostics: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# Diagnostics", ""]
    lines.extend(_diagnostic_markdown_lines(diagnostics))
    return "\n".join(lines) + "\n"


def _render_usage_markdown(public: Mapping[str, Any]) -> str:
    lines = [
        "# Usage report",
        "",
        "Scope: **%s**" % PUBLIC_SCOPE,
        "",
        "Sessions: %s" % public.get("session_count", 0),
    ]
    if public.get("since"):
        lines.append("Window: %s" % public["since"])
    lines.extend(["", "## Token usage", ""])
    lines.extend(_markdown_token_table(public.get("total_token_usage", {})))
    lines.extend(["", "## Quota (%s)" % PUBLIC_SCOPE, ""])
    lines.extend(_markdown_quota_table(public.get("quota", {})))
    lines.extend(["", "## Limitations", ""])
    lines.extend(_limitation_lines(public.get("limitations", ()) or (), "- "))
    return "\n".join(lines) + "\n"


def _token_lines(tokens: Any, prefix: str) -> List[str]:
    token_dict = _public_token_usage(tokens)
    return ["%s%s: %s" % (prefix, key, token_dict[key]) for key in TokenUsage._FIELDS]


def _markdown_token_table(tokens: Any) -> List[str]:
    token_dict = _public_token_usage(tokens)
    lines = ["| Counter | Value |", "| --- | ---: |"]
    lines.extend("| %s | %s |" % (key, token_dict[key]) for key in TokenUsage._FIELDS)
    return lines


def _quota_lines(quota: Any, prefix: str) -> List[str]:
    value = quota if isinstance(quota, Mapping) else {}
    return [
        "%scurrent used: %s" % (prefix, _percent(value.get("current_used_percent"))),
        "%sobserved delta: %s" % (prefix, _delta(value.get("observed_delta_percent"))),
        "%swindow: %s" % (prefix, _window(value.get("window_minutes"))),
        "%sreset: %s" % (prefix, _display_or_unavailable(value.get("resets_at"))),
        "%splan_type: %s" % (prefix, _display_or_unavailable(value.get("plan_type"))),
        "%shas_credits: %s" % (prefix, _bool_or_unavailable(value.get("has_credits"))),
    ]


def _markdown_quota_table(quota: Any) -> List[str]:
    value = quota if isinstance(quota, Mapping) else {}
    rows = [
        ("Current used", _percent(value.get("current_used_percent"))),
        ("Observed delta", _delta(value.get("observed_delta_percent"))),
        ("Window", _window(value.get("window_minutes"))),
        ("Reset", _display_or_unavailable(value.get("resets_at"))),
        ("Plan type", _display_or_unavailable(value.get("plan_type"))),
        ("Has credits", _bool_or_unavailable(value.get("has_credits"))),
    ]
    return ["| Field | Value |", "| --- | --- |"] + [
        "| %s | %s |" % (_md(key), _md(val)) for key, val in rows
    ]


def _diagnostic_lines(diagnostics: Sequence[Mapping[str, Any]], prefix: str) -> List[str]:
    if not diagnostics:
        return [prefix + "none"]
    lines = []
    for item in diagnostics:
        labels = item.get("agent_labels") or []
        suffix = " (%s)" % ", ".join(str(label) for label in labels) if labels else ""
        lines.append(
            "%s[%s] %s: %s%s"
            % (
                prefix,
                _display_or_unavailable(item.get("severity")),
                _display_or_unavailable(item.get("code")),
                _display_or_unavailable(item.get("message")),
                suffix,
            )
        )
    return lines


def _diagnostic_markdown_lines(diagnostics: Sequence[Mapping[str, Any]]) -> List[str]:
    if not diagnostics:
        return ["No diagnostics."]
    return [
        "- **%s** `%s`: %s%s"
        % (
            _md(item.get("severity")),
            _md(item.get("code")),
            _md(item.get("message")),
            " (" + ", ".join(_md(label) for label in item.get("agent_labels") or []) + ")"
            if item.get("agent_labels")
            else "",
        )
        for item in diagnostics
    ]


def _limitation_lines(limitations: Sequence[Any], prefix: str) -> List[str]:
    if not limitations:
        return [prefix + "none"]
    return [prefix + (_public_text(item) or "unavailable") for item in limitations]


def _public_token_usage(value: Any) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for name in TokenUsage._FIELDS:
        if isinstance(value, Mapping):
            raw = value.get(name, 0)
        else:
            raw = getattr(value, name, 0)
        result[name] = _nonnegative_int(raw)
    return result


def _public_quota(value: Mapping[str, Any]) -> Dict[str, Any]:
    has_credits = value.get("has_credits")
    return {
        "plan_type": _public_text(value.get("plan_type")),
        "current_used_percent": _float_or_none(value.get("current_used_percent")),
        "observed_delta_percent": _float_or_none(value.get("observed_delta_percent")),
        "window_minutes": _nonnegative_int_or_none(value.get("window_minutes")),
        "resets_at": _public_scalar(value.get("resets_at")),
        "has_credits": has_credits if isinstance(has_credits, bool) else None,
        "observed_at": _public_scalar(value.get("observed_at")),
        "evidence_level": _public_text(value.get("evidence_level")),
    }


def _public_diagnostic(value: Any) -> Dict[str, Any]:
    if isinstance(value, Diagnostic):
        raw = value.to_dict()
    elif isinstance(value, Mapping):
        raw = value
    else:
        raw = {}
    labels = raw.get("agent_labels") or []
    return {
        "code": _public_text(raw.get("code")) or "unknown",
        "severity": _public_text(raw.get("severity")) or "INFO",
        "evidence_level": _public_text(raw.get("evidence_level")) or "unavailable",
        "message": _public_text(raw.get("message")) or "unavailable",
        "agent_labels": [_public_text(label) or "agent-unknown" for label in labels],
        "count": _nonnegative_int(raw.get("count")),
    }


def _public_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)[:512]
    if "SECRET_SENTINEL" in text:
        text = text.replace("SECRET_SENTINEL", "<redacted>")
    if text.startswith("/") or text.startswith("~"):
        return "<redacted>"
    return text


def _public_scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return _public_text(value)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _nonnegative_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_tokens(value: Any) -> str:
    tokens = _public_token_usage(value)
    return "total=%s input=%s output=%s reasoning=%s" % (
        tokens["total_tokens"],
        tokens["input_tokens"],
        tokens["output_tokens"],
        tokens["reasoning_output_tokens"],
    )


def _percent(value: Any) -> str:
    parsed = _float_or_none(value)
    return "unavailable" if parsed is None else "%.1f%%" % parsed


def _delta(value: Any) -> str:
    parsed = _float_or_none(value)
    return "unavailable" if parsed is None else "%+.1f percentage points" % parsed


def _window(value: Any) -> str:
    parsed = _nonnegative_int_or_none(value)
    return "unavailable" if parsed is None else "%s minutes" % parsed


def _display_or_unavailable(value: Any) -> str:
    text = _public_text(value)
    return text if text not in (None, "") else "unavailable"


def _bool_or_unavailable(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else "unavailable"


def _md(value: Any) -> str:
    return (_display_or_unavailable(value).replace("|", "\\|").replace("\n", " "))


__all__ = [
    "PUBLIC_ANALYSIS_KEYS",
    "PUBLIC_SCOPE",
    "dumps_json",
    "public_analysis_dict",
    "public_diagnostics",
    "render_analysis",
    "render_lint",
    "render_usage",
    "render_watch",
]
