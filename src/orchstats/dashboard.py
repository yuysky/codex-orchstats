"""Render a self-contained, local dashboard for safe orchestration reports.

The dashboard deliberately accepts only the already-normalized report mapping
used by the public reporting layer.  It never reads a trace, makes a network
request, or copies unknown fields into the document.  Values are normalized a
second time at this boundary so a report can safely be passed to an HTML
template without turning the template into a new privacy boundary.
"""

from __future__ import annotations

import json
import re
import webbrowser
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .pricing import pricing_catalog


_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_SEVERITY_ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "WARN": 2, "WARNING": 2, "HIGH": 3, "CRITICAL": 4, "ERROR": 4}
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s(])(?:/(?:Users|private|home|tmp|var|opt|etc|Volumes)/|~/|[A-Za-z]:[\\/])")


def render_dashboard(payload: Mapping[str, Any]) -> str:
    """Return a single UTF-8-ready HTML dashboard for a safe report mapping.

    The returned string contains all styles, behavior, and data.  The JSON
    data block escapes ``<``, ``>``, ``&``, and slashes so even a hostile value
    cannot terminate the script element.
    """

    data = _normalize_payload(payload)
    data_json = _safe_json(data)
    catalog_json = _safe_json(_normalize_pricing_catalog(pricing_catalog()))
    return _HTML_DOCUMENT.replace("__ORCHSTATS_DATA__", data_json, 1).replace("__ORCHSTATS_PRICING__", catalog_json, 1)


def default_dashboard_output(now: Optional[datetime] = None) -> Path:
    """Return a timestamped dashboard path under the local reports folder."""

    moment = now if isinstance(now, datetime) else datetime.now()
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / "reports" / ("orchstats-dashboard-%s.html" % stamp)


def open_dashboard(path: Path, opener=webbrowser.open) -> bool:
    """Open a dashboard path with an injected browser opener.

    ``opener`` is injectable to keep this helper deterministic in tests.  A
    browser refusal or an OS/browser error is reported as ``False`` rather
    than being allowed to break report generation.
    """

    try:
        target = Path(path).expanduser().resolve().as_uri()
        return bool(opener(target))
    except Exception:
        return False


def _normalize_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(payload)
    raw_runs = root.get("runs")
    runs: List[Dict[str, Any]] = []
    if isinstance(raw_runs, Sequence) and not isinstance(raw_runs, (str, bytes, bytearray)):
        for index, item in enumerate(raw_runs):
            if isinstance(item, Mapping):
                runs.append(_normalize_run(item, index + 1))

    summary = _summary_for_runs(runs, root.get("summary"))
    scan = _normalize_scan(root.get("scan"), len(runs))
    limitations = _string_list(root.get("limitations"))
    since = _safe_text(root.get("since"), "7d")
    if since not in ("24h", "7d", "30d", "all"):
        since = "7d"
    return {
        "schema_version": "dashboard-history-0.1",
        "generated_at": _safe_text(root.get("generated_at"), "unavailable"),
        "since": since,
        "scan": scan,
        "summary": summary,
        "runs": runs,
        "limitations": limitations,
    }


def _normalize_pricing_catalog(value: Any) -> Dict[str, Any]:
    """Keep only safe, numeric USD-per-million rates for the browser.

    ``pricing_catalog`` is deliberately a small boundary between the Python
    pricing table and the self-contained report.  Accept the direct
    ``{model: rates}`` shape as well as a ``{"models": ...}`` envelope so the
    renderer remains compatible while the pricing table evolves.  Unknown or
    malformed entries are omitted rather than guessed.
    """

    raw = _mapping(value)
    has_envelope = isinstance(raw.get("models"), Mapping)
    models = raw.get("models") if has_envelope else raw
    result: Dict[str, Any] = {}
    aliases = {
        "uncached": ("uncached", "input", "input_per_million", "uncached_input", "uncached_input_per_million"),
        "cached": ("cached", "cached_input", "cached_input_per_million"),
        "cache_write": ("cache_write", "cache_write_input", "cache_write_input_per_million", "cache_write_per_million"),
        "output": ("output", "output_per_million"),
        "reasoning": ("reasoning", "reasoning_output", "reasoning_per_million", "reasoning_output_per_million"),
    }
    for model, value in models.items() if isinstance(models, Mapping) else ():
        model_name = _safe_text(model)
        rates = _mapping(value)
        if not model_name or not rates:
            continue
        normalized: Dict[str, float] = {}
        for name, candidates in aliases.items():
            amount: Optional[float] = None
            for candidate in candidates:
                number = _safe_number(rates.get(candidate))
                if number is not None and number >= 0:
                    amount = number
                    break
            if amount is None:
                continue
            # Per-token values are accepted for callers that make the unit
            # explicit; the canonical browser contract is per million tokens.
            if any(candidate.endswith("_per_token") for candidate in candidates):
                amount *= 1000000
            normalized[name] = amount
        # The common catalog shape uses rates named ``*_per_token``.  Handle
        # those keys separately without treating arbitrary tiny USD values as
        # a different unit.
        for name, base in (("uncached", "input"), ("cached", "cached_input"), ("cache_write", "cache_write_input"), ("output", "output"), ("reasoning", "reasoning_output")):
            per_token = _safe_number(rates.get(base + "_per_token"))
            if per_token is not None and per_token >= 0:
                normalized[name] = per_token * 1000000
        if normalized:
            result[model_name] = normalized

    # Keep the catalog metadata (including its as-of/source note) when the
    # pricing module provides the documented envelope.  Model keys and rates
    # have already been reduced to the safe browser contract above.
    if has_envelope:
        envelope: Dict[str, Any] = {"models": result}
        for key in ("schema_version", "as_of", "currency", "unit", "billing_mode", "context"):
            text = _safe_text(raw.get(key))
            if text:
                envelope[key] = text
        raw_aliases = _mapping(raw.get("aliases"))
        safe_aliases: Dict[str, str] = {}
        for alias, canonical in raw_aliases.items():
            alias_text = _safe_text(alias)
            canonical_text = _safe_text(canonical)
            if alias_text and canonical_text:
                safe_aliases[alias_text] = canonical_text
                if canonical_text in result and alias_text not in result:
                    result[alias_text] = dict(result[canonical_text])
        if safe_aliases:
            envelope["aliases"] = safe_aliases
        return envelope
    return result


def _normalize_scan(value: Any, run_count: int) -> Dict[str, Any]:
    raw = _mapping(value)
    # Only labels useful to a local report are retained.  In particular, a
    # scan's source path or command is intentionally not copied to HTML.
    result: Dict[str, Any] = {}
    for key in (
        "files_seen",
        "files_parsed",
        "invalid_identity_count",
        "duplicate_id_count",
        "orphan_count",
        "cycle_count",
        "unreadable_count",
        "unknown_time_count",
    ):
        result[key] = _safe_int(raw.get(key))
    return result


def _normalize_run(value: Mapping[str, Any], index: int) -> Dict[str, Any]:
    analysis = _normalize_analysis(value.get("analysis"))
    agents = analysis["agents"]
    tree = _normalize_tree(value.get("agent_tree"), agents)
    quality_flags = _string_list(value.get("quality_flags"))
    diagnostics = analysis["diagnostics"]
    label = _safe_run_label(value.get("label"), index)
    status = _safe_text(value.get("status"), "observed")
    if status not in ("observed", "in_progress", "partial"):
        status = "observed"
    return {
        "label": label,
        "project": _normalize_project(value.get("project")),
        "status": status,
        "quality_flags": quality_flags,
        "start_at": _safe_scalar(value.get("start_at")),
        "end_at": _safe_scalar(value.get("end_at")),
        "time_evidence": _normalize_time_evidence(value.get("time_evidence")),
        "agent_tree": tree,
        "analysis": analysis,
    }


def _normalize_project(value: Any) -> Dict[str, str]:
    raw = _mapping(value)
    candidate = raw.get("label") if raw else value
    label = _safe_project_label(candidate)
    evidence = _safe_text(raw.get("evidence_level"), "unavailable") if raw else "unavailable"
    if evidence not in ("derived", "unavailable"):
        evidence = "unavailable"
    if not label:
        label = "Unassigned"
        evidence = "unavailable"
    return {"label": label, "evidence_level": evidence}


def _normalize_time_evidence(value: Any) -> str:
    text = _safe_text(value, "unavailable")
    return text if text in ("derived", "unavailable") else "unavailable"


def _normalize_analysis(value: Any) -> Dict[str, Any]:
    raw = _mapping(value)
    raw_agents = raw.get("agents")
    agents: List[Dict[str, Any]] = []
    if isinstance(raw_agents, Sequence) and not isinstance(raw_agents, (str, bytes, bytearray)):
        for index, item in enumerate(raw_agents):
            if not isinstance(item, Mapping):
                continue
            agents.append(
                {
                    "label": _safe_agent_label(item.get("label"), len(agents) + 1),
                    "role": _safe_text(item.get("role")),
                    "model": _safe_text(item.get("model")),
                    "effort": _safe_text(item.get("effort")),
                    "fork_mode": _safe_text(item.get("fork_mode"), "unknown"),
                    "token_usage": _normalize_tokens(item.get("token_usage")),
                    "tool_count": _safe_int(item.get("tool_count")),
                }
            )

    raw_diagnostics = raw.get("diagnostics")
    diagnostics: List[Dict[str, Any]] = []
    if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, (str, bytes, bytearray)):
        for item in raw_diagnostics:
            if isinstance(item, Mapping):
                diagnostics.append(_normalize_diagnostic(item))

    raw_limitations = _string_list(raw.get("limitations"))
    return {
        "schema_version": _safe_text(raw.get("schema_version"), "0.1"),
        "agents": agents,
        "total_token_usage": _normalize_tokens(raw.get("total_token_usage")),
        "quota": _normalize_quota(raw.get("quota")),
        "diagnostics": diagnostics,
        "limitations": raw_limitations,
    }


def _normalize_tokens(value: Any) -> Dict[str, int]:
    raw = _mapping(value)
    return {name: _safe_int(raw.get(name)) for name in _TOKEN_FIELDS}


def _normalize_quota(value: Any) -> Dict[str, Any]:
    raw = _mapping(value)
    result: Dict[str, Any] = {}
    result["plan_type"] = _safe_text(raw.get("plan_type"))
    for key in ("current_used_percent", "observed_delta_percent"):
        number = _safe_number(raw.get(key))
        result[key] = number
    result["window_minutes"] = _safe_int_or_none(raw.get("window_minutes"))
    result["resets_at"] = _safe_scalar(raw.get("resets_at"))
    result["has_credits"] = raw.get("has_credits") if isinstance(raw.get("has_credits"), bool) else None
    result["observed_at"] = _safe_scalar(raw.get("observed_at"))
    result["evidence_level"] = _safe_text(raw.get("evidence_level"), "unavailable")
    return result


def _normalize_diagnostic(value: Mapping[str, Any]) -> Dict[str, Any]:
    labels_raw = value.get("agent_labels")
    labels: List[str] = []
    if isinstance(labels_raw, Sequence) and not isinstance(labels_raw, (str, bytes, bytearray)):
        for item in labels_raw:
            if len(labels) >= 32:
                break
            labels.append(_safe_agent_label(item, len(labels) + 1))
    severity = _safe_text(value.get("severity"), "INFO").upper()
    if severity not in _SEVERITY_ORDER:
        severity = "INFO"
    return {
        "code": _safe_text(value.get("code"), "unknown"),
        "severity": severity,
        "evidence_level": _safe_text(value.get("evidence_level"), "unavailable"),
        "message": _safe_text(value.get("message"), "unavailable"),
        "agent_labels": labels,
        "count": _safe_int(value.get("count")),
    }


def _normalize_tree(value: Any, agents: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the flat dashboard-history agent tree contract."""

    items = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else ()
    result: List[Dict[str, Any]] = []
    known_labels: List[str] = []
    for index, item in enumerate(items[:128], 1):
        if not isinstance(item, Mapping):
            continue
        label = _safe_agent_label(item.get("label"), len(result) + 1)
        known_labels.append(label)
        result.append(
            {
                "label": label,
                "parent_label": item.get("parent_label"),
                "depth": _safe_int(item.get("depth")),
            }
        )
    if not result:
        result = [
            {
                "label": _safe_agent_label(agent.get("label"), index + 1),
                "parent_label": None,
                "depth": 0,
            }
            for index, agent in enumerate(agents)
        ]
        known_labels = [item["label"] for item in result]
    for item in result:
        raw_parent = item["parent_label"]
        parent = _safe_agent_label(raw_parent, 1) if raw_parent is not None else None
        item["parent_label"] = parent if parent in known_labels and parent != item["label"] else None
    return result


def _summary_for_runs(runs: Sequence[Mapping[str, Any]], supplied: Any) -> Dict[str, Any]:
    total_tokens = 0
    agent_count = 0
    tool_count = 0
    high_count = 0
    latest_quota: Optional[Mapping[str, Any]] = None
    latest_key: Tuple[int, str] = (-1, "")

    for run_index, run in enumerate(runs):
        analysis = _mapping(run.get("analysis"))
        total_tokens += _safe_int(_mapping(analysis.get("total_token_usage")).get("total_tokens"))
        agents = analysis.get("agents")
        if isinstance(agents, Sequence) and not isinstance(agents, (str, bytes, bytearray)):
            agent_count += len(agents)
            for agent in agents:
                if isinstance(agent, Mapping):
                    tool_count += _safe_int(agent.get("tool_count"))
        diagnostics = analysis.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, (str, bytes, bytearray)):
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, Mapping):
                    continue
                if str(diagnostic.get("severity", "")).upper() in ("HIGH", "CRITICAL", "ERROR"):
                    high_count += _diagnostic_weight(diagnostic)
        quota = _mapping(analysis.get("quota"))
        observed = _safe_scalar(quota.get("observed_at"))
        observed_sort = (run_index, str(observed) if observed is not None else "")
        if latest_quota is None or observed_sort >= latest_key:
            latest_key = observed_sort
            latest_quota = quota

    supplied_map = _mapping(supplied)
    supplied_total = supplied_map.get("total_token_usage")
    if isinstance(supplied_total, Mapping):
        supplied_total_tokens = supplied_total.get("total_tokens")
    else:
        supplied_total_tokens = supplied_map.get("total_tokens")
    supplied_run_count = supplied_map.get("run_count")
    supplied_agent_count = supplied_map.get("agent_count")
    supplied_tool_count = supplied_map.get("tool_count")
    supplied_high_count = supplied_map.get("high_diagnostic_count")
    if supplied_total_tokens is not None:
        total_tokens = _safe_int(supplied_total_tokens)
    if supplied_run_count is not None:
        run_count = _safe_int(supplied_run_count)
    else:
        run_count = len(runs)
    if supplied_agent_count is not None:
        agent_count = _safe_int(supplied_agent_count)
    if supplied_tool_count is not None:
        tool_count = _safe_int(supplied_tool_count)
    if supplied_high_count is not None:
        high_count = _safe_int(supplied_high_count)

    supplied_quota = supplied_map.get("latest_quota")
    if isinstance(supplied_quota, Mapping):
        latest_quota = _normalize_quota(supplied_quota)
    normalized_total = _normalize_tokens(supplied_total) if isinstance(supplied_total, Mapping) else _normalize_tokens({"total_tokens": total_tokens})
    normalized_quota = _normalize_quota(latest_quota or {})
    return {
        "run_count": run_count,
        "agent_count": agent_count,
        "tool_count": tool_count,
        "total_token_usage": normalized_total,
        "high_diagnostic_count": high_count,
        "latest_quota": normalized_quota,
    }


def _diagnostic_weight(value: Mapping[str, Any]) -> int:
    count = _safe_int(value.get("count"))
    return count if count > 0 else 1


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: List[str] = []
    for item in value[:64]:
        text = _safe_text(item)
        if text:
            result.append(text)
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_json(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    # Escaping slash is not strictly required once ``<`` is escaped, but it
    # makes the script-breakout invariant obvious and easy to audit.
    return (
        rendered.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("/", "\\u002f")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _safe_text(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    if value is None:
        return fallback
    if isinstance(value, (Mapping, list, tuple, set)):
        return fallback
    text = str(value).strip()[:512]
    if not text:
        return fallback
    if re.search(r"<\s*/?\s*script\b", text, re.IGNORECASE):
        return "<redacted>"
    if "SECRET_SENTINEL" in text:
        text = text.replace("SECRET_SENTINEL", "<redacted>")
    if text.startswith(("/", "~")) or _ABSOLUTE_PATH_RE.search(text):
        return "<redacted>"
    return text


def _safe_agent_label(value: Any, index: int) -> str:
    text = _safe_text(value)
    if not text:
        return "agent-%03d" % max(1, index)
    # Public analysis labels are intentionally pseudonymous.  Preserve the
    # established ``agent-###`` shape and redact labels that look like source
    # session IDs rather than inventing a new identifier in the HTML.
    if re.fullmatch(r"agent-[0-9]{1,6}", text):
        return text
    if text.lower().startswith("agent-") and len(text) <= 64 and re.fullmatch(r"agent-[a-z0-9_-]+", text.lower()):
        return text
    return "agent-%03d" % max(1, index)


def _safe_run_label(value: Any, index: int) -> str:
    text = _safe_text(value)
    if text and re.fullmatch(r"run-[0-9]{1,6}", text):
        return text
    return "run-%03d" % max(1, index)


def _safe_project_label(value: Any) -> Optional[str]:
    text = _safe_text(value)
    if not text or text == "<redacted>" or "/" in text or "\\" in text:
        return None
    return text[:96]


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return _safe_text(value)


def _safe_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


_HTML_DOCUMENT = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local private report · orchstats</title>
<style>
:root {
  color-scheme: light;
  --paper: #f3f7fc;
  --surface: rgba(252, 253, 255, .92);
  --surface-solid: #fcfdff;
  --surface-muted: rgba(235, 241, 249, .76);
  --ink: #172235;
  --muted: #657389;
  --line: rgba(115, 137, 166, .22);
  --line-strong: rgba(94, 120, 153, .38);
  --orange: #ff8264;
  --orange-soft: rgba(255, 130, 100, .14);
  --green: #21a993;
  --green-soft: rgba(33, 169, 147, .13);
  --blue: #4a7bff;
  --blue-soft: rgba(74, 123, 255, .13);
  --purple: #8a6de9;
  --point-normal: #263f94;
  --point-partial: #f0a126;
  --point-outlier: #c23a70;
  --red: #d75d72;
  --red-soft: rgba(215, 93, 114, .13);
  --glass: rgba(248, 251, 255, .67);
  --shadow: 0 18px 50px rgba(45, 68, 103, .09), 0 2px 9px rgba(45, 68, 103, .035);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-synthesis: none;
}
* { box-sizing: border-box; }
html { background: var(--paper); }
body { position: relative; min-height: 100vh; margin: 0; color: var(--ink); background: radial-gradient(circle at 10% 7%, rgba(124, 183, 255, .22), transparent 31%), radial-gradient(circle at 88% 13%, rgba(166, 126, 244, .16), transparent 29%), radial-gradient(circle at 58% 85%, rgba(61, 210, 181, .13), transparent 32%), linear-gradient(145deg, #f8fbff 0%, #eef4fb 48%, #f7f5ff 100%); background-attachment: fixed; line-height: 1.45; }
button, input, select { font: inherit; }
button { color: inherit; }
.app-layout { display: grid; grid-template-columns: 232px minmax(0, 1fr); gap: 28px; align-items: start; width: min(1530px, calc(100% - 40px)); margin: 0 auto; }
.compact-nav { display: none; }
.sidebar { position: sticky; top: 0; z-index: 3; align-self: start; display: grid; gap: 18px; min-width: 0; min-height: 100vh; padding: 30px 18px; color: var(--ink); background: linear-gradient(180deg, rgba(248, 251, 255, .64), rgba(240, 246, 253, .32)); border: 0; border-right: 1px solid rgba(115, 137, 166, .26); border-radius: 0; box-shadow: none; -webkit-backdrop-filter: blur(22px) saturate(145%); backdrop-filter: blur(22px) saturate(145%); }
.sidebar-brand { font-size: 15px; font-weight: 750; letter-spacing: .09em; text-transform: uppercase; }
.sidebar-subtitle { margin-top: -12px; color: var(--muted); font-size: 11px; }
.sidebar-privacy { padding: 9px 10px; color: #167862; background: rgba(230, 252, 246, .72); border: 1px solid rgba(52, 175, 147, .24); border-radius: 8px; font-size: 10px; font-weight: 750; letter-spacing: .07em; text-transform: uppercase; }
.sidebar-privacy::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; background: var(--green); }
.sidebar-nav { display: grid; gap: 4px; }
.sidebar-nav a { display: block; padding: 8px 9px; color: var(--muted); border-left: 2px solid transparent; font-size: 12px; text-decoration: none; }
.sidebar-nav a:hover, .sidebar-nav a:focus, .sidebar-nav a[aria-current="location"] { color: var(--ink); background: var(--blue-soft); border-left-color: var(--blue); outline: none; }
.sidebar-meta { display: grid; gap: 7px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 10px; }
.sidebar-meta span { display: grid; gap: 2px; }
.sidebar-meta strong { color: var(--ink); font-size: 11px; font-weight: 650; overflow-wrap: anywhere; }
.shell { position: relative; min-width: 0; padding: 14px 0 56px; }
section[id] { scroll-margin-top: 18px; }
.hero { max-width: 850px; margin-bottom: 27px; }
.eyebrow { margin: 0 0 9px; color: var(--orange); font-size: 11px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; }
h1, h2, h3, h4 { line-height: 1.15; margin: 0; letter-spacing: -.025em; }
h1 { font-size: clamp(32px, 5vw, 54px); }
h2 { font-size: 22px; }
h3 { font-size: 16px; }
h4 { font-size: 13px; letter-spacing: .01em; }
.lede { max-width: 670px; margin: 14px 0 0; color: var(--muted); font-size: 16px; }
.meta { display: flex; flex-wrap: wrap; gap: 7px 20px; margin-top: 17px; color: var(--muted); font-size: 12px; }
.meta strong { color: var(--ink); font-weight: 600; }
.scope-note { margin-top: 18px; padding: 11px 13px; color: #765c43; background: #f7eadb; border: 1px solid #ead3bb; border-radius: 10px; font-size: 12px; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 25px 0 17px; }
.kpi, .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 15px; box-shadow: var(--shadow); }
.kpi { min-height: 102px; padding: 15px; display: flex; flex-direction: column; justify-content: space-between; }
.kpi-label { color: var(--muted); font-size: 11px; }
.kpi-value { overflow: hidden; text-overflow: ellipsis; font-size: clamp(22px, 2.6vw, 31px); font-weight: 700; letter-spacing: -.045em; font-variant-numeric: tabular-nums; }
.kpi-value[title] { cursor: help; }
.kpi-note { color: var(--muted); font-size: 10px; }
.kpi.cost .kpi-value { color: var(--orange); }
.kpi.quota .kpi-value { color: var(--blue); }
.section-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; margin-bottom: 14px; }
.section-note { color: var(--muted); font-size: 12px; }
.panel { padding: 21px; margin: 14px 0; }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.chart-card { min-width: 0; padding: 16px; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 12px; }
.chart-card.wide { grid-column: span 2; }
.chart-title { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.chart-caption { margin: 5px 0 10px; color: var(--muted); font-size: 11px; }
.chart-plot { min-width: 0; min-height: 222px; }
.chart-plot svg { display: block; width: 100%; height: 222px; overflow: visible; }
.chart-gridline { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 5; }
.chart-axis-label { fill: var(--muted); font-size: 10px; }
.chart-total { fill: none; stroke: var(--ink); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.chart-cost { fill: none; stroke: var(--green); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.chart-quota { fill: none; stroke: var(--blue); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.chart-model { fill: none; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; stroke-dasharray: 5 4; opacity: .95; }
.chart-point { stroke: rgba(255, 255, 255, .96); stroke-width: 2.6; filter: drop-shadow(0 2px 4px rgba(28, 43, 72, .24)); }
.chart-empty { fill: var(--muted); font-size: 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 8px; color: var(--muted); font-size: 10px; }
.legend-item { display: inline-flex; align-items: center; gap: 5px; min-width: 0; }
.legend-swatch { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.model-share { display: grid; grid-template-columns: minmax(118px, .7fr) minmax(0, 1.3fr); align-items: center; gap: 16px; min-height: 222px; }
.model-share svg { display: block; width: 100%; height: 190px; }
.donut-track { fill: none; stroke: #e3ded5; stroke-width: 18; }
.donut-segment { fill: none; stroke-width: 18; transform: rotate(-90deg); transform-origin: 50% 50%; }
.donut-center { fill: var(--ink); font-size: 15px; font-weight: 700; text-anchor: middle; }
.donut-center-note { fill: var(--muted); font-size: 9px; text-anchor: middle; }
.model-legend { display: grid; gap: 8px; min-width: 0; }
.model-legend-row { display: grid; grid-template-columns: 8px minmax(60px, 1fr) auto; gap: 7px; align-items: center; min-width: 0; font-size: 11px; }
.model-legend-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-legend-values { color: var(--muted); text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.quota-key { display: flex; gap: 12px; align-items: center; color: var(--muted); font-size: 10px; }
.quota-key span::before { content: ""; display: inline-block; width: 9px; height: 2px; margin: 0 5px 3px 0; background: var(--blue); }
.quota-key .break::before { background: var(--red); }
.filters { display: flex; flex-wrap: wrap; gap: 9px; margin: 0 0 14px; }
.filter { display: grid; gap: 4px; min-width: 145px; color: var(--muted); font-size: 10px; }
.filter.keyword { flex: 1 1 220px; }
.filter select, .filter input { width: 100%; color: var(--ink); background: var(--surface); border: 1px solid var(--line-strong); border-radius: 8px; padding: 7px 9px; outline: none; }
.filter select:focus, .filter input:focus { border-color: var(--orange); box-shadow: 0 0 0 3px var(--orange-soft); }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 11px; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { padding: 10px 11px; color: var(--muted); background: var(--surface-muted); font-size: 10px; font-weight: 600; text-align: left; white-space: nowrap; }
td { padding: 10px 11px; border-top: 1px solid var(--line); vertical-align: middle; }
tbody tr { cursor: pointer; outline: none; }
tbody tr:hover, tbody tr:focus { background: #fff5eb; }
tbody tr:focus { box-shadow: inset 0 0 0 2px var(--orange); }
.run-primary { display: grid; gap: 2px; min-width: 120px; }
.run-date { color: var(--muted); font-size: 10px; }
.run-label { font-weight: 650; }
.status, .severity { display: inline-flex; align-items: center; width: fit-content; border-radius: 999px; padding: 3px 7px; background: var(--surface-muted); color: var(--muted); font-size: 10px; }
.stacked { display: flex; width: 150px; max-width: 22vw; min-width: 85px; height: 9px; overflow: hidden; border-radius: 999px; background: #e3ded5; }
.stacked-segment { min-width: 2px; height: 100%; }
.stacked-segment:first-child { border-radius: 999px 0 0 999px; }
.stacked-segment:last-child { border-radius: 0 999px 999px 0; }
.numeric { white-space: nowrap; font-variant-numeric: tabular-nums; }
.cost-text.partial::after { content: " · partial"; color: var(--orange); font-size: 10px; }
.cost-text.unavailable::after { content: " · unavailable"; color: var(--muted); font-size: 10px; }
.empty-state { padding: 36px 18px; text-align: center; color: var(--muted); border: 1px dashed var(--line-strong); border-radius: 11px; }
.empty-state h3 { color: var(--ink); margin-bottom: 7px; }
.report-coverage { display: grid; gap: 12px; margin-top: 16px; padding: 14px 15px; background: rgba(248, 251, 255, .72); border: 1px solid var(--line); border-radius: 12px; }
.report-coverage-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.report-coverage-head strong { font-size: 12px; }
.report-coverage-head span { color: var(--muted); font-size: 10px; }
.report-scan-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }
.report-scan-stat { min-width: 0; padding: 8px 9px; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 8px; }
.report-scan-stat span, .report-scan-stat strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.report-scan-stat span { color: var(--muted); font-size: 9px; }
.report-scan-stat strong { margin-top: 3px; color: var(--ink); font-size: 13px; font-variant-numeric: tabular-nums; }
.report-empty-states { display: grid; gap: 8px; }
.report-empty-state { padding: 11px 12px; color: #765c43; background: #f7eadb; border: 1px solid #ead3bb; border-radius: 9px; font-size: 11px; }
.report-empty-state strong { display: block; margin-bottom: 3px; color: var(--ink); font-size: 12px; }
.report-empty-state p { margin: 0; }
.report-limitations { padding-top: 10px; border-top: 1px solid var(--line); }
.report-limitations h3 { margin-bottom: 6px; font-size: 11px; }
.report-limitations ul { display: grid; gap: 4px; margin: 0; padding-left: 18px; color: var(--muted); font-size: 10px; }
.report-limitations li { padding-left: 2px; }
.muted { color: var(--muted); }
.keyboard-note { margin: 11px 0 0; font-size: 11px; }
.run-hover-card { position: fixed; z-index: 30; width: min(370px, calc(100vw - 24px)); padding: 13px; color: var(--ink); background: rgba(249, 252, 255, .94); border: 1px solid rgba(94, 120, 153, .32); border-radius: 12px; box-shadow: 0 18px 48px rgba(34, 53, 82, .18), inset 0 1px 0 rgba(255, 255, 255, .9); pointer-events: none; -webkit-backdrop-filter: blur(22px) saturate(165%); backdrop-filter: blur(22px) saturate(165%); }
.run-hover-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; padding-bottom: 9px; border-bottom: 1px solid var(--line); }
.run-hover-head strong { font-size: 13px; }
.run-hover-head span { max-width: 58%; overflow: hidden; color: var(--muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.run-hover-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-top: 9px; }
.run-hover-stat { min-width: 0; padding: 7px; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 8px; }
.run-hover-stat span, .run-hover-breakdown h4 { display: block; color: var(--muted); font-size: 8px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
.run-hover-stat strong { display: block; margin-top: 3px; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.run-hover-breakdowns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 10px; }
.run-hover-breakdown { min-width: 0; }
.run-hover-breakdown h4 { margin-bottom: 5px; }
.run-hover-line { display: flex; justify-content: space-between; gap: 8px; margin-top: 3px; color: var(--muted); font-size: 9px; }
.run-hover-line span:first-child { overflow: hidden; color: var(--ink); text-overflow: ellipsis; white-space: nowrap; }
.run-hover-line strong { flex: 0 0 auto; font-weight: 650; font-variant-numeric: tabular-nums; }
.run-hover-help { margin: 9px 0 0; color: var(--muted); font-size: 8px; }
dialog { width: min(980px, calc(100% - 24px)); max-height: calc(100vh - 24px); padding: 0; color: var(--ink); background: var(--surface); border: 1px solid var(--line-strong); border-radius: 16px; box-shadow: 0 25px 80px rgba(42, 32, 22, .2); }
dialog::backdrop { background: rgba(39, 32, 25, .32); }
dialog[data-open="true"] { display: block; position: fixed; inset: 12px auto auto 50%; transform: translateX(-50%); overflow: auto; }
.dialog-shell { padding: 21px; }
.dialog-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.dialog-head p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.close-button { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; border: 1px solid var(--line-strong); border-radius: 50%; background: var(--surface); cursor: pointer; }
.close-button:hover, .close-button:focus { border-color: var(--orange); background: var(--orange-soft); }
.detail-section { margin-top: 18px; }
.detail-section > h3 { margin-bottom: 10px; }
.model-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; }
.model-summary-card { min-width: 0; padding: 11px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-muted); }
.model-summary-card h4 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-summary-card strong { display: block; margin-top: 6px; font-size: 19px; font-variant-numeric: tabular-nums; }
.model-summary-card p { margin: 3px 0 0; color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.detail-card { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-muted); }
.detail-card h3 { margin-bottom: 10px; }
.detail-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
.detail-meta div { min-width: 0; }
.detail-meta dt { color: var(--muted); font-size: 10px; }
.detail-meta dd { margin: 2px 0 0; overflow-wrap: anywhere; font-size: 12px; }
.tree, .tree ul { margin: 0; padding-left: 17px; list-style: none; }
.tree > ul { padding-left: 0; }
.tree li { position: relative; padding: 3px 0 3px 14px; font-size: 12px; }
.tree li::before { content: ""; position: absolute; left: 0; top: 11px; width: 8px; border-top: 1px solid var(--line-strong); }
.tree li::after { content: ""; position: absolute; left: 0; top: -3px; bottom: -4px; border-left: 1px solid var(--line-strong); }
.tree > ul > li::after { display: none; }
.agent-table { overflow: hidden; }
.agent-head, .agent-row { display: grid; grid-template-columns: minmax(82px, 1fr) minmax(82px, 1fr) minmax(66px, .75fr) minmax(48px, .55fr) minmax(135px, 1.2fr); gap: 7px; align-items: center; }
.agent-head { padding: 0 8px 6px; color: var(--muted); font-size: 10px; }
.agent-row { padding: 8px; border-top: 1px solid var(--line); font-size: 11px; }
.agent-row > div { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-token-card { display: grid; grid-template-columns: auto 1fr; gap: 7px; align-items: center; min-width: 0; padding: 5px 7px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); }
.agent-token-card strong { overflow: hidden; text-overflow: ellipsis; font-size: 12px; font-variant-numeric: tabular-nums; }
.agent-token-card span { color: var(--muted); font-size: 9px; }
.token-breakdown { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.token-breakdown span { padding: 4px 6px; color: var(--muted); background: var(--surface); border: 1px solid var(--line); border-radius: 6px; font-size: 9px; font-variant-numeric: tabular-nums; }
details { border-top: 1px solid var(--line); }
details:first-child { border-top: 0; }
summary { padding: 10px 0; cursor: pointer; color: var(--ink); font-size: 12px; font-weight: 650; }
.item-list { display: grid; gap: 7px; margin: 0 0 11px; padding: 0; list-style: none; }
.item-list li { padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); font-size: 11px; }
.diagnostic-item { display: grid; gap: 3px; }
.diagnostic-head { display: flex; align-items: center; gap: 7px; }
.diagnostic-code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px; }
.diagnostic-message { font-size: 11px; }
.evidence { color: var(--muted); font-size: 10px; }
.scope-note.compact { margin-top: 12px; }
.footer { margin-top: 26px; color: var(--muted); font-size: 11px; }
[hidden] { display: none !important; }
@media (max-width: 1020px) { .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 760px) {
  .shell { width: min(100% - 24px, 620px); }
  .sidebar { gap: 12px; }
  .chart-grid, .detail-grid { grid-template-columns: 1fr; }
  .chart-card.wide { grid-column: auto; }
  .panel { padding: 15px; border-radius: 13px; }
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .kpi { min-height: 94px; padding: 12px; }
  .kpi-value { font-size: 23px; }
  .model-share { grid-template-columns: 1fr; }
  .model-share svg { height: 170px; }
  .agent-head { display: none; }
  .report-scan-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .agent-row { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 10px 0; }
  .agent-row > div:nth-child(1)::before { content: "Agent · "; color: var(--muted); }
  .agent-row > div:nth-child(2)::before { content: "Model · "; color: var(--muted); }
  .agent-row > div:nth-child(3)::before { content: "Role · "; color: var(--muted); }
  .agent-row > div:nth-child(4)::before { content: "Tools · "; color: var(--muted); }
  .agent-token-card { grid-column: span 2; }
}
@media (max-width: 430px) { .kpis { grid-template-columns: 1fr 1fr; } .chart-plot, .chart-plot svg { min-height: 190px; height: 190px; } .stacked { max-width: 27vw; } }

/* Ratio-first daily view.  These rules intentionally override the earlier
   primitives so the report can retain its privacy-safe renderer contract
   while presenting a calmer, less number-heavy information hierarchy. */
.shell { width: auto; }
.hero { max-width: 980px; margin-bottom: 20px; }
.hero h1 { font-size: clamp(34px, 4.7vw, 58px); }
.lede { max-width: 790px; }
.panel { background: rgba(252, 253, 255, .88); border-color: rgba(255, 255, 255, .8); box-shadow: var(--shadow), inset 0 1px 0 rgba(255, 255, 255, .92); }
.today-panel { padding: 0; overflow: hidden; }
.today-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px; padding: 20px 22px 14px; border-bottom: 1px solid var(--line); }
.today-head p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.today-grid { display: grid; grid-template-columns: minmax(0, 1.32fr) minmax(310px, .68fr); }
.today-metric { min-width: 0; padding: 27px 24px 23px; }
.today-label { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 11px; letter-spacing: .03em; text-transform: uppercase; }
.today-value { display: block; margin: 9px 0 3px; font-size: clamp(36px, 5.8vw, 68px); line-height: 1; letter-spacing: -.06em; font-variant-numeric: tabular-nums; }
.today-index .today-value { color: var(--blue); }
.today-note { min-height: 34px; margin: 8px 0 0; color: var(--muted); font-size: 11px; }
.today-support { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-content: stretch; border-left: 1px solid var(--line); background: linear-gradient(145deg, rgba(240, 246, 255, .72), rgba(248, 246, 255, .64)); }
.today-support-stat { min-width: 0; padding: 18px; border-bottom: 1px solid var(--line); }
.today-support-stat:nth-child(odd) { border-right: 1px solid var(--line); }
.today-support-stat:nth-last-child(-n + 2) { border-bottom: 0; }
.today-support-stat span { display: block; color: var(--muted); font-size: 9px; letter-spacing: .04em; text-transform: uppercase; }
.today-support-stat strong { display: block; margin-top: 7px; overflow: hidden; color: var(--ink); font-size: 21px; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.today-support-stat small { display: block; margin-top: 4px; color: var(--muted); font-size: 9px; }
.quota-gauge { height: 8px; margin-top: 17px; overflow: hidden; background: #e3ded5; border-radius: 999px; }
.quota-gauge-fill { width: 0; height: 100%; background: var(--blue); border-radius: inherit; transition: width .18s ease; }
.today-context { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 12px 22px; color: var(--muted); background: rgba(232, 240, 250, .62); border-top: 1px solid var(--line); font-size: 11px; }
.today-context strong { color: var(--ink); font-weight: 650; }
.project-overview { display: grid; gap: 10px; max-height: 560px; overflow: auto; padding: 2px 4px 2px 0; }
.project-row { display: grid; grid-template-columns: minmax(150px, .72fr) minmax(210px, 1.55fr) minmax(135px, .68fr); gap: 14px; align-items: center; width: 100%; padding: 13px 14px; color: var(--ink); background: rgba(244, 248, 254, .72); border: 1px solid var(--line); border-radius: 12px; cursor: pointer; text-align: left; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .84); transition: border-color .16s ease, transform .16s ease, box-shadow .16s ease; }
.project-row:hover, .project-row:focus { border-color: rgba(74, 123, 255, .52); outline: none; transform: translateY(-1px); box-shadow: 0 10px 24px rgba(50, 76, 112, .09), inset 0 1px 0 rgba(255, 255, 255, .92); }
.project-identity, .project-viz, .project-support { min-width: 0; }
.project-name { display: block; overflow: hidden; font-size: 13px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.project-evidence { display: block; margin-top: 3px; color: var(--muted); font-size: 9px; }
.project-share-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 6px; color: var(--muted); font-size: 10px; }
.project-share-head strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.project-share-track { display: block; height: 12px; overflow: hidden; background: rgba(112, 135, 164, .13); border-radius: 999px; }
.project-share-fill { display: block; height: 100%; min-width: 1px; border-radius: inherit; }
.project-model-track { display: flex; height: 6px; margin-top: 6px; overflow: hidden; background: rgba(112, 135, 164, .10); border-radius: 999px; }
.project-model-track span { min-width: 1px; height: 100%; }
.project-support { display: block; color: var(--muted); font-size: 10px; text-align: right; font-variant-numeric: tabular-nums; }
.project-support strong { display: block; color: var(--ink); font-size: 12px; }
.project-support > span { display: block; margin-top: 3px; }
.project-note { margin: 12px 0 0; color: var(--muted); font-size: 10px; }
.ratio-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.ratio-grid .wide { grid-column: span 2; }
.chart-card { background: rgba(250, 252, 255, .88); border-color: rgba(116, 139, 168, .19); box-shadow: inset 0 1px 0 rgba(255, 255, 255, .86); }
.chart-card.emphasis { padding: 18px; background: linear-gradient(145deg, rgba(252, 253, 255, .94), rgba(242, 247, 255, .88)); }
.chart-plot.tall, .chart-plot.tall svg { min-height: 310px; height: 310px; }
.chart-plot.medium, .chart-plot.medium svg { min-height: 246px; height: 246px; }
.chart-plot.alluvial, .chart-plot.alluvial svg { min-height: 286px; height: 286px; }
.control-row { display: flex; flex-wrap: wrap; align-items: end; gap: 8px; }
.control-row .filter { min-width: min(260px, 100%); }
.control-row select, .filter select, .filter input, .button, .close-button, .active-filter { background: rgba(249, 252, 255, .68); box-shadow: inset 0 1px 0 rgba(255, 255, 255, .9), 0 5px 18px rgba(51, 78, 116, .06); -webkit-backdrop-filter: blur(16px) saturate(165%); backdrop-filter: blur(16px) saturate(165%); }
.button { min-height: 34px; padding: 7px 13px; border: 1px solid var(--line-strong); border-radius: 9px; cursor: pointer; font-size: 11px; font-weight: 650; }
.button:hover, .button:focus { border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-soft), inset 0 1px 0 rgba(255, 255, 255, .9); outline: none; }
.button.primary { color: white; background: var(--ink); border-color: var(--ink); }
.button.primary:hover, .button.primary:focus { background: #253652; }
.token-flow { margin-top: 13px; }
.token-flow svg { display: block; width: 100%; min-height: 340px; height: 340px; }
.sankey-link { fill: none; stroke-opacity: .24; transition: stroke-opacity .15s ease; }
.sankey-link:hover, .sankey-link:focus { stroke-opacity: .68; outline: none; }
.sankey-node { stroke: rgba(23, 34, 53, .18); stroke-width: 1; rx: 4; }
.sankey-label { fill: var(--ink); font-size: 10px; font-weight: 650; }
.sankey-value { fill: var(--muted); font-size: 9px; }
.ratio-legend { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 7px; margin-top: 9px; }
.ratio-item, .legend-button { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 7px 9px; color: var(--muted); background: var(--surface-muted); border: 1px solid var(--line); border-radius: 8px; font-size: 10px; }
.ratio-item strong, .legend-button strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.legend-button { width: 100%; cursor: pointer; text-align: left; }
.legend-button:hover, .legend-button:focus { border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-soft); outline: none; }
.legend-name { display: flex; align-items: center; gap: 6px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend-dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; }
.interactive-mark { cursor: pointer; outline: none; }
.interactive-mark:focus { stroke: var(--ink); stroke-width: 2; }
.chart-index-line { fill: none; stroke: var(--blue); stroke-width: 3.2; stroke-linecap: round; stroke-linejoin: round; filter: drop-shadow(0 4px 7px rgba(74, 123, 255, .2)); }
.chart-index-area { fill: url(#index-area-gradient); opacity: .82; }
.chart-index-point { stroke: rgba(255, 255, 255, .98); stroke-width: 3; filter: drop-shadow(0 2px 5px rgba(28, 43, 72, .28)); }
.chart-index-partial { fill: var(--point-partial); stroke: #8a5310; }
.alluvial-ribbon { stroke: none; opacity: .30; transition: opacity .16s ease; }
.alluvial-ribbon:hover { opacity: .58; }
.alluvial-node { stroke: rgba(255, 255, 255, .88); stroke-width: 1; rx: 3; }
.alluvial-date-line { stroke: rgba(93, 118, 153, .18); stroke-width: 1; }
.axis-index { fill: var(--blue); }
.filter-actions { display: flex; align-items: end; gap: 7px; }
.active-filter { display: inline-flex; align-items: center; gap: 7px; margin: 0 0 12px; padding: 6px 9px; color: #765c43; background: #f7eadb; border: 1px solid #ead3bb; border-radius: 999px; font-size: 10px; }
.active-filter button { padding: 0; border: 0; background: transparent; cursor: pointer; font-weight: 700; }
.sort-button { display: inline-flex; gap: 5px; align-items: center; padding: 0; color: inherit; border: 0; background: transparent; cursor: pointer; font: inherit; font-weight: 650; }
.sort-button::after { content: "↕"; opacity: .45; }
.sort-button[data-active="true"]::after { content: attr(data-direction); opacity: 1; color: var(--orange); }
.mix-cell { min-width: 165px; }
.mix-caption { display: block; max-width: 190px; margin-top: 4px; overflow: hidden; color: var(--muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.type-uncached { background: #ff8264; }
.type-cached { background: #21a993; }
.type-write { background: #8a6de9; }
.type-output { background: #4a7bff; }
#run-table { min-width: 1080px; }
.detail-overview { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-top: 16px; overflow: hidden; background: var(--line); border: 1px solid var(--line); border-radius: 11px; }
.detail-stat { min-width: 0; padding: 12px; background: var(--surface-muted); }
.detail-stat span { display: block; color: var(--muted); font-size: 9px; text-transform: uppercase; }
.detail-stat strong { display: block; margin-top: 5px; overflow: hidden; font-size: 18px; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.detail-viz-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.viz-card { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-muted); }
.viz-card h3 { margin-bottom: 5px; }
.share-track { display: flex; width: 100%; height: 22px; margin: 15px 0 9px; overflow: hidden; background: #e3ded5; border-radius: 999px; }
.share-track .stacked-segment { min-width: 0; }
.share-legend { display: grid; gap: 6px; }
.share-row { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 7px; align-items: center; color: var(--muted); font-size: 10px; }
.share-row .name { overflow: hidden; color: var(--ink); text-overflow: ellipsis; white-space: nowrap; }
.agent-bars { display: grid; gap: 9px; max-height: 430px; overflow: auto; padding-right: 4px; }
.agent-viz-row { display: grid; grid-template-columns: minmax(115px, .7fr) minmax(150px, 1.6fr) auto; gap: 10px; align-items: center; font-size: 10px; }
.agent-viz-label { min-width: 0; }
.agent-viz-label strong, .agent-viz-label span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-viz-label span { color: var(--muted); font-size: 9px; }
.agent-viz-track { height: 12px; overflow: hidden; background: #e3ded5; border-radius: 999px; }
.agent-viz-fill { height: 100%; min-width: 1px; border-radius: inherit; }
.agent-viz-value { white-space: nowrap; color: var(--muted); font-variant-numeric: tabular-nums; }
.agent-statistics { display: grid; gap: 9px; }
.agent-stat-row { display: grid; grid-template-columns: minmax(104px, .62fr) minmax(180px, 1.7fr) minmax(150px, .9fr); gap: 14px; align-items: center; padding: 12px 13px; background: rgba(244, 248, 254, .72); border: 1px solid var(--line); border-radius: 11px; }
.agent-stat-role { min-width: 0; }
.agent-stat-role strong, .agent-stat-role span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-stat-role strong { font-size: 13px; }
.agent-stat-role span { margin-top: 3px; color: var(--muted); font-size: 9px; }
.agent-stat-share { min-width: 0; }
.agent-stat-share-head { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; color: var(--muted); font-size: 10px; }
.agent-stat-share-head strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.agent-stat-track, .agent-stat-model-track { display: flex; width: 100%; overflow: hidden; border-radius: 999px; background: rgba(112, 135, 164, .13); }
.agent-stat-track { height: 12px; }
.agent-stat-model-track { height: 6px; margin-top: 7px; }
.agent-stat-track span, .agent-stat-model-track span { min-width: 1px; height: 100%; }
.agent-stat-model-caption { display: block; margin-top: 5px; overflow: hidden; color: var(--muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.agent-stat-metrics { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px 11px; color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.agent-stat-metrics strong { color: var(--ink); }
.flow-project-filter { min-width: min(210px, 100%) !important; }
.token-flow-model-legend { margin-top: 9px; }
.range-toggle { display: inline-flex; gap: 5px; align-items: center; }
.range-toggle .button { min-height: 29px; padding: 5px 9px; font-size: 10px; }
.range-toggle .button[aria-pressed="true"] { color: white; background: var(--ink); border-color: var(--ink); }
.chart-index-outlier { fill: var(--point-outlier); stroke: #751b49; stroke-width: 3; }
.index-stat { padding: 5px 7px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface-muted); font-variant-numeric: tabular-nums; }
.detail-accordion { margin-top: 16px; border: 1px solid var(--line); border-radius: 11px; padding: 0 13px; }
dialog { background: rgba(249, 252, 255, .82); border-color: rgba(255, 255, 255, .82); box-shadow: 0 30px 95px rgba(33, 49, 79, .25), inset 0 1px 0 rgba(255, 255, 255, .9); -webkit-backdrop-filter: blur(28px) saturate(175%); backdrop-filter: blur(28px) saturate(175%); }
dialog::backdrop { background: rgba(31, 42, 61, .28); -webkit-backdrop-filter: blur(7px); backdrop-filter: blur(7px); }
.dialog-head { position: sticky; top: -21px; z-index: 2; margin: -21px -21px 0; padding: 21px 21px 14px; background: rgba(248, 251, 255, .78); -webkit-backdrop-filter: blur(20px) saturate(170%); backdrop-filter: blur(20px) saturate(170%); }
tbody tr:hover, tbody tr:focus { background: rgba(74, 123, 255, .07); }
tbody tr:focus { box-shadow: inset 0 0 0 2px var(--blue); }
@media (max-width: 800px) {
  .today-grid, .ratio-grid, .detail-viz-grid { grid-template-columns: 1fr; }
  .today-support { border-left: 0; border-top: 1px solid var(--line); }
  .ratio-grid .wide { grid-column: auto; }
  .detail-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .token-flow svg { min-height: 300px; height: 300px; }
}
@media (max-width: 520px) {
  .shell { width: min(100% - 22px, 480px); }
  .today-value { font-size: 43px; }
  .today-head, .today-metric { padding-left: 15px; padding-right: 15px; }
  .today-context { padding-left: 15px; padding-right: 15px; }
  .agent-viz-row { grid-template-columns: minmax(90px, .8fr) 1.4fr; }
  .agent-viz-value { grid-column: 2; }
}
@supports not ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px))) {
  .compact-section select, .control-row select, .filter select, .filter input, .button, .close-button, .active-filter, dialog, .dialog-head { background: #f8fbff; }
}
@media (prefers-reduced-transparency: reduce) {
  .sidebar { background: rgba(248, 251, 255, .94); -webkit-backdrop-filter: none; backdrop-filter: none; }
  .compact-section select, .control-row select, .filter select, .filter input, .button, .close-button, .active-filter, dialog, .dialog-head { background: #f8fbff; -webkit-backdrop-filter: none; backdrop-filter: none; }
}
@media (max-width: 760px) {
  .app-layout { display: block; width: min(100% - 24px, 620px); }
  .sidebar { display: none; }
  .compact-nav { display: grid; grid-template-columns: auto auto minmax(150px, 1fr); gap: 10px; align-items: center; padding: 11px 0 12px; border-bottom: 1px solid var(--line); }
  .compact-brand { color: var(--ink); font-size: 12px; font-weight: 780; letter-spacing: .10em; text-transform: uppercase; }
  .compact-privacy { display: inline-flex; align-items: center; gap: 5px; color: #167862; font-size: 9px; font-weight: 700; white-space: nowrap; }
  .compact-privacy::before { content: ""; width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: var(--green); }
  .compact-section { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 7px; align-items: center; min-width: 0; color: var(--muted); font-size: 9px; text-transform: uppercase; letter-spacing: .06em; }
  .compact-section select { min-width: 0; width: 100%; padding: 7px 28px 7px 9px; color: var(--ink); background: rgba(249, 252, 255, .72); border: 1px solid var(--line-strong); border-radius: 8px; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .9); -webkit-backdrop-filter: blur(14px) saturate(150%); backdrop-filter: blur(14px) saturate(150%); outline: none; text-transform: none; letter-spacing: 0; }
  .compact-section select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-soft); }
  .shell { width: auto; padding-top: 14px; }
  .project-row { grid-template-columns: 1fr; gap: 9px; }
  .project-support { text-align: left; }
  .agent-stat-row { grid-template-columns: 1fr; gap: 9px; }
  .agent-stat-metrics { justify-content: flex-start; }
}
@media (max-width: 470px) {
  .compact-nav { grid-template-columns: minmax(0, 1fr) auto; }
  .compact-section { grid-column: 1 / -1; }
}
@media (hover: none) { .run-hover-card { display: none !important; } }
</style>
</head>
<body>
<div class="app-layout">
  <header class="compact-nav" id="compact-navigation" aria-label="Compact dashboard navigation">
    <div class="compact-brand">orchstats</div>
    <div class="compact-privacy">Local private</div>
    <label class="compact-section"><span>Section</span><select id="section-switcher" aria-label="Jump to dashboard section"><option value="#overview-panel">Overview</option><option value="#composition-panel">Composition</option><option value="#trends-panel">Trends</option><option value="#projects-panel">Projects</option><option value="#agents-panel">Agents</option><option value="#runs-panel">Runs</option></select></label>
  </header>
  <aside class="sidebar" id="dashboard-sidebar" aria-label="Dashboard navigation">
    <div class="sidebar-brand">orchstats</div>
    <div class="sidebar-subtitle">Local private</div>
    <div class="sidebar-privacy">Local private report</div>
    <nav class="sidebar-nav" aria-label="Sections">
      <a href="#overview-panel">Overview</a>
      <a href="#composition-panel">Composition</a>
      <a href="#trends-panel">Trends</a>
      <a href="#projects-panel">Projects</a>
      <a href="#agents-panel">Agents</a>
      <a href="#runs-panel">Runs</a>
    </nav>
    <div class="sidebar-meta">
      <span>Generated<strong id="sidebar-generated-at">Unavailable</strong></span>
      <span>Window<strong id="sidebar-since">Unavailable</strong></span>
    </div>
    <div class="sidebar-subtitle">Do not share · local-only HTML</div>
  </aside>
  <div class="shell">
  <main>
    <section class="hero" id="overview-panel" aria-labelledby="page-title">
      <p class="eyebrow">Private by default</p>
      <h1 id="page-title">See how each day is composed.</h1>
      <p class="lede">A proportion-first view of project allocation, model mix, token composition, and the API-equivalent cost observed for each positive quota percentage point.</p>
      <p class="scope-note"><strong>Do not share.</strong> This is a local private report. API-equivalent cost is a Standard short-context reference estimate, not a Codex bill. The cost/quota index divides same-day priced cost by positive run-observed quota deltas; it is a heuristic cross-signal proxy, not the price or dollar value of plan quota. Runs are assigned to the local calendar day of their latest observed end time. Shared plan quota is account-level, excludes credit balances, and is never attributed to a model or agent. Reasoning is included within output and is not counted twice.</p>
      <div class="meta"><span>Generated <strong id="generated-at">Unavailable</strong></span><span>Window <strong id="since">Unavailable</strong></span><span>Schema <strong id="schema-version">Unavailable</strong></span></div>
      <div class="report-coverage" id="report-coverage" aria-live="polite">
        <div class="report-coverage-head"><div><strong>Source scan</strong><span id="report-scan-summary">Reading local JSONL coverage…</span></div><span id="report-scan-level" class="status">—</span></div>
        <div id="report-scan-grid" class="report-scan-grid"></div>
        <div id="report-empty-states" class="report-empty-states">
          <article id="report-empty-no-files" class="report-empty-state" hidden><strong>No session files found.</strong><p>Use <code>orchstats dashboard --demo</code> for synthetic history, or pass <code>--sessions-root &lt;directory&gt;</code> to inspect local sessions.</p></article>
          <article id="report-empty-no-valid-runs" class="report-empty-state" hidden><strong>No valid runs found.</strong><p>Files were found, but no root/descendant run passed the identity, linkage, or time-window checks. Try <code>--since all</code> or compare with <code>orchstats dashboard --demo</code>.</p></article>
          <article id="report-partial-files" class="report-empty-state" hidden><strong>Some session files were excluded.</strong><p>Review the scan counts and global limitations below; fix unreadable, truncated, identity, linkage, or timestamp issues, or use <code>--demo</code> to compare a clean input.</p></article>
        </div>
        <div id="report-limitations-panel" class="report-limitations"><h3>Global limitations</h3><ul id="report-limitations"><li>Reading report limitations…</li></ul></div>
      </div>
    </section>

    <section class="panel today-panel" id="today-panel" aria-labelledby="today-title">
      <div class="today-head"><div><p class="eyebrow">Current local day</p><h2 id="today-title">Daily cost / quota index</h2><p>One calculated value: same-day API-equivalent cost divided by positive observed quota change.</p></div><strong id="today-date" class="section-note">—</strong></div>
      <div class="today-grid">
        <article class="today-metric today-index"><div class="today-label"><span>Estimated cost per observed quota point</span><span>heuristic</span></div><strong id="today-index" class="today-value">—</strong><p id="today-index-note" class="today-note">A valid point needs priced activity and a positive run-observed quota delta.</p></article>
        <aside class="today-support" aria-label="Today supporting observations"><div class="today-support-stat"><span>Estimated cost</span><strong id="today-cost">—</strong><small>API equivalent</small></div><div class="today-support-stat"><span>Positive quota delta</span><strong id="today-quota-delta">—</strong><small>credits excluded</small></div><div class="today-support-stat"><span>Current quota used</span><strong id="today-quota">—</strong><small id="today-quota-note">latest shared observation</small></div><div class="today-support-stat"><span>Delta samples</span><strong id="today-quota-samples">0</strong><small>positive run observations</small></div></aside>
      </div>
      <div class="today-context"><span>Runs <strong id="today-runs">0</strong></span><span>Reported tokens <strong id="today-tokens">0</strong></span><span>Leading project <strong id="today-leading-project">—</strong></span><span>Leading model <strong id="today-leading-model">—</strong></span><span>Pricing coverage <strong id="today-coverage">—</strong></span></div>
    </section>

    <section class="panel" id="composition-panel" aria-labelledby="flow-title">
      <div class="section-head"><div><h2 id="flow-title">Token composition</h2><span class="section-note">proportions use mutually exclusive token partitions</span></div></div>
      <article class="chart-card emphasis">
        <div class="chart-title"><div><h3>Input → model → token-type flow</h3><p class="chart-caption">Overall view flows through models into the four exclusive token partitions. Changing project scope redraws the proportional views immediately.</p></div><div class="control-row"><label class="filter flow-project-filter">Project scope<select id="flow-project-filter"><option value="">All projects</option></select></label><label class="filter">Model scope<select id="flow-model-filter"><option value="">Overall</option></select></label></div></div>
        <div class="token-flow"><svg id="token-flow-chart" role="img" aria-label="Token composition Sankey diagram" viewBox="0 0 760 340"></svg></div>
        <div id="token-flow-model-legend" class="ratio-legend token-flow-model-legend" aria-label="Token flow model proportions"></div>
        <div id="token-flow-legend" class="ratio-legend"></div>
        <p id="token-flow-note" class="chart-caption">Reasoning is annotated inside output. Reported total tokens remain separate because counters are not always an exact accounting identity.</p>
      </article>
    </section>

    <section class="panel" id="trends-panel" aria-labelledby="trends-title">
      <div class="section-head"><h2 id="trends-title">Daily proportions and movement</h2><span class="section-note">activate a date or model to filter the run table</span></div>
      <div class="ratio-grid">
        <article class="chart-card wide"><div class="chart-title"><div><h3>Estimated cost per observed quota point</h3><span class="section-note">one calculated series</span></div><div class="range-toggle" role="group" aria-label="Cost quota index range"><button id="cost-quota-detail" class="button" type="button" aria-pressed="true">Detail</button><button id="cost-quota-full-range" class="button" type="button" aria-pressed="false">Full range</button></div></div><p class="chart-caption">Daily API-equivalent USD ÷ summed positive run-observed quota delta. Detail uses a P90 Y ceiling for geometry only; outlier exact values remain in tooltips and the legend. Invalid dates are omitted and adjacent valid points connect directly; this is not a dollar value for plan quota.</p><div class="chart-plot medium"><svg id="daily-pulse-chart" role="img" aria-label="Daily API-equivalent cost per observed quota percentage point" viewBox="0 0 760 246"></svg></div><div id="daily-pulse-legend" class="legend"></div></article>
        <article class="chart-card wide"><div class="chart-title"><h3>Daily model-share Sankey</h3><span class="section-note">time alluvial · each date = 100%</span></div><p class="chart-caption">Ribbons connect the same model across adjacent valid dates. They show share evolution, not token transfer; activate a dated node to jump to matching runs.</p><div class="chart-plot alluvial"><svg id="daily-model-share-chart" role="img" aria-label="Daily model share evolution Sankey diagram" viewBox="0 0 720 286"></svg></div><div id="daily-model-share-legend" class="ratio-legend"></div></article>
        <article class="chart-card wide"><div class="chart-title"><h3>Daily token-type Sankey</h3><span class="section-note">time alluvial · third-column change</span></div><p class="chart-caption">The Sankey’s uncached input, cached input, cache write, and output column is normalized per date. Ribbons show share evolution, not token transfer; missing dates are skipped.</p><div class="chart-plot alluvial"><svg id="daily-type-share-chart" role="img" aria-label="Daily token type share evolution Sankey diagram" viewBox="0 0 720 286"></svg></div><div id="daily-type-share-legend" class="ratio-legend"></div></article>
      </div>
    </section>

    <section class="panel" id="projects-panel" aria-labelledby="projects-title">
      <div class="section-head"><div><h2 id="projects-title">Project allocation</h2><span class="section-note">root workspace name · token-weighted</span></div><strong id="project-count-note" class="section-note">0 projects</strong></div>
      <article class="chart-card emphasis">
        <div class="chart-title"><div><h3>Share by project</h3><p class="chart-caption">The primary measure is each project's share of reported agent tokens. The thinner track shows model proportions within that project.</p></div></div>
        <div id="project-overview" class="project-overview" aria-live="polite"></div>
        <p class="project-note">Select a project to sync composition scope and filter its runs. Pricing remains global; shared quota is deliberately not attributed to projects. Only the final root cwd directory name is retained.</p>
      </article>
    </section>

    <section class="panel" id="agents-panel" aria-labelledby="agents-title">
      <div class="section-head"><div><h2 id="agents-title">Agent statistics</h2><span class="section-note">global role aggregation · all projects</span></div><span class="section-note">tokens first</span></div>
      <article class="chart-card emphasis">
        <p class="chart-caption">Agents are grouped by explicit role. When role is absent, a depth-0 tree entry is Root; remaining instances are Unassigned. Anonymous labels are not used as groups.</p>
        <div id="agent-statistics" class="agent-statistics" aria-live="polite"></div>
        <p class="project-note">Each role shows reported token share, internal model proportions, instance count, and tool count. Cost and shared quota are intentionally omitted from this global role view.</p>
      </article>
    </section>

    <section class="panel" id="runs-panel" aria-labelledby="runs-title">
      <div class="section-head"><h2 id="runs-title">Runs</h2><span id="run-count-note" class="section-note">0 shown</span></div>
      <div id="run-filter-form" class="filters" role="search" aria-label="Filter runs">
        <label class="filter">Project<select id="project-filter"><option value="">All projects</option></select></label>
        <label class="filter">Model<select id="model-filter"><option value="">All models</option></select></label>
        <label class="filter">Status<select id="status-filter"><option value="">All statuses</option></select></label>
        <label class="filter">Severity<select id="severity-filter"><option value="">All severities</option></select></label>
        <label class="filter keyword">Keyword<input id="keyword-filter" type="search" autocomplete="off" placeholder="label, diagnostic…"></label>
        <div class="filter-actions"><button id="reset-run-filters" class="button" type="button">Reset</button></div>
      </div>
      <div id="active-day-filter" class="active-filter" hidden><span id="active-day-label">Day</span><button id="clear-day-filter" type="button" aria-label="Clear date filter">×</button></div>
      <div id="runs-empty" class="empty-state" hidden><h3>No local runs in this selection</h3><p>Clear a chart selection or adjust the filters.</p></div>
      <div class="table-wrap" id="runs-table-wrap">
        <table id="run-table"><thead><tr><th scope="col"><button class="sort-button" type="button" data-sort="date">Date / run</button></th><th scope="col">Project</th><th scope="col">Model share</th><th scope="col">Token-type share</th><th scope="col"><button class="sort-button" type="button" data-sort="cost">Est. cost</button></th><th scope="col"><button class="sort-button" type="button" data-sort="quota">Quota used</button></th><th scope="col">Status</th></tr></thead><tbody id="run-rows"></tbody></table>
      </div>
      <div id="run-hover-card" class="run-hover-card" role="tooltip" hidden></div>
      <p class="muted keyboard-note" id="keyboard-note">Hover or focus a row for its data summary. Click it, or press Enter or Space, to open visual detail. Anonymous #run links support direct navigation and browser Back/Forward.</p>
    </section>
  </main>
  <footer class="footer">Generated from deterministic local analysis · no runtime API, AI, network, or external assets</footer>
</div>
</div>

<dialog id="run-dialog" aria-labelledby="dialog-title">
  <div class="dialog-shell">
    <header class="dialog-head"><div><p class="eyebrow">Visual run detail</p><h2 id="dialog-title">No run selected</h2><p id="dialog-subtitle">Select a run from the table.</p></div><button type="button" class="close-button" id="dialog-close" aria-label="Close run details">×</button></header>
    <section class="detail-overview" aria-label="Run summary"><article class="detail-stat"><span>API equivalent</span><strong id="detail-cost">—</strong></article><article class="detail-stat"><span>Quota used</span><strong id="detail-quota-value">—</strong></article><article class="detail-stat"><span>Reported tokens</span><strong id="detail-token-total">—</strong></article><article class="detail-stat"><span>Agents</span><strong id="detail-agent-count">—</strong></article></section>
    <section class="detail-section detail-viz-grid"><article class="viz-card"><h3>Model share</h3><p class="chart-caption">Grouped by model within this run.</p><div id="detail-model-share" class="share-track"></div><div id="detail-model-legend" class="share-legend"></div></article><article class="viz-card"><h3>Token-type share</h3><p class="chart-caption">Exclusive input/cache/output partitions.</p><div id="detail-token-share" class="share-track"></div><div id="detail-token-legend" class="share-legend"></div></article></section>
    <section class="detail-section"><div class="section-head"><h3>Agent share</h3><span class="section-note">sorted by reported tokens</span></div><div id="detail-agent-bars" class="agent-bars"></div></section>
    <section class="detail-accordion"><details id="detail-context-details"><summary>Run context and quota evidence</summary><dl class="detail-meta"><div><dt>Project</dt><dd id="detail-project">—</dd></div><div><dt>Project evidence</dt><dd id="detail-project-evidence">—</dd></div><div><dt>Status</dt><dd id="detail-status">—</dd></div><div><dt>Severity</dt><dd id="detail-severity">—</dd></div><div><dt>Date</dt><dd id="detail-date">—</dd></div><div><dt>Start</dt><dd id="detail-start">—</dd></div><div><dt>End</dt><dd id="detail-end">—</dd></div><div><dt>Time evidence</dt><dd id="detail-time-evidence">—</dd></div><div><dt>Quality flags</dt><dd id="detail-quality">—</dd></div><div><dt>Quota observation</dt><dd id="detail-quota">Unavailable</dd></div></dl><p class="scope-note compact">Quota is account-level/shared, excludes credit balances, and is not attributed to any project, agent, or model.</p></details><details id="detail-tree-details"><summary>Anonymous agent tree</summary><div id="agent-tree" class="tree"></div></details><details id="detail-diagnostics-details"><summary>Diagnostics and evidence</summary><ul id="detail-diagnostics" class="item-list"></ul></details><details id="detail-limitations-details"><summary>Limitations</summary><ul id="detail-limitations" class="item-list"></ul></details></section>
  </div>
</dialog>
<noscript><p style="padding:20px">This dashboard needs JavaScript enabled to display the local report projection.</p></noscript>
<script id="dashboard-data" type="application/json">__ORCHSTATS_DATA__</script>
<script id="pricing-data" type="application/json">__ORCHSTATS_PRICING__</script>
<script id="dashboard-behavior">
(function () {
  "use strict";
  var report = parseJSON("dashboard-data", {});
  var catalog = parseJSON("pricing-data", {});
  var runs = Array.isArray(report.runs) ? report.runs : [];
  var selectedIndex = null;
  var filteredIndexes = [];
  var colors = ["#4a7bff", "#21a993", "#8a6de9", "#ff8264", "#f2b84b", "#38a0d8", "#d45c9a", "#63728a"];
  var modelOrder = [];
  runs.forEach(function (run) { list(run && run.analysis && run.analysis.agents).forEach(function (agent) { var name = String((agent && agent.model) || "unavailable"); if (modelOrder.indexOf(name) === -1) modelOrder.push(name); }); });
  modelOrder.sort();
  var projectOrder = [];
  runs.forEach(function (run) { var project = run && run.project && run.project.label ? String(run.project.label) : "Unassigned"; if (projectOrder.indexOf(project) === -1) projectOrder.push(project); });
  projectOrder.sort();

  function el(id) { return document.getElementById(id); }
  function parseJSON(id, fallback) { var node = el(id); try { return JSON.parse(node ? node.textContent : "{}"); } catch (error) { return fallback; } }
  function list(value) { return Array.isArray(value) ? value : []; }
  function number(value) { return typeof value === "number" && isFinite(value) ? value : 0; }
  function positive(value) { return Math.max(0, Math.round(number(value))); }
  function tokens(value) { var source = value && typeof value === "object" ? value : {}; var result = {}; ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"].forEach(function (field) { result[field] = positive(source[field]); }); return result; }
  function fmtInt(value) { return positive(value).toLocaleString("en-US"); }
  function compact(value) { var n = positive(value); if (n >= 1000000000) return (n / 1000000000).toFixed(2) + "B"; if (n >= 1000000) return (n / 1000000).toFixed(2) + "M"; if (n >= 1000) return (n / 1000).toFixed(2) + "K"; return fmtInt(n); }
  function fmtPct(value) { return typeof value === "number" && isFinite(value) ? value.toFixed(2) + "%" : "—"; }
  function fmtMoney(value) { return typeof value === "number" && isFinite(value) ? "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—"; }
  function fmtQuotaPoints(value) { return typeof value === "number" && isFinite(value) ? value.toFixed(2) + " pp" : "—"; }
  function fmtCostQuotaIndex(value) { return typeof value === "number" && isFinite(value) ? fmtMoney(value) + " / 1%" : "—"; }
  function windowLabel(minutes) { var value = positive(minutes); if (!value) return "observed window"; if (value === 10080) return "7d weekly window"; if (value % 10080 === 0) return (value / 10080) + "w window"; if (value % 1440 === 0) return (value / 1440) + "d window"; if (value % 60 === 0) return (value / 60) + "h window"; return value + "m window"; }
  function full(value) { return value === null || value === undefined || value === "" ? "—" : String(value); }
  function diagnostics(run) { return list(run && run.analysis && run.analysis.diagnostics); }
  function severity(run) { var items = diagnostics(run); if (!items.length) return list(run && run.quality_flags).length ? "MEDIUM" : "INFO"; return items.reduce(function (highest, item) { var current = String((item && item.severity) || "INFO").toUpperCase(); return severityWeight(current) > severityWeight(highest) ? current : highest; }, "INFO"); }
  function severityWeight(value) { return ({ INFO: 0, LOW: 1, MEDIUM: 2, WARN: 2, WARNING: 2, HIGH: 3, CRITICAL: 4, ERROR: 4 })[value] || 0; }
  function agentList(run) { return list(run && run.analysis && run.analysis.agents); }
  function rootTreeAgentV2(run, label) { return list(run && run.agent_tree).some(function (item) { return item && String(item.label || "") === String(label || "") && number(item.depth) === 0; }); }
  function agentRoleV2(run, agent) {
    var explicit = String((agent && agent.role) || "").trim();
    if (explicit) {
      var normalized = explicit.toLowerCase();
      if (normalized === "root") return "Root";
      if (normalized === "explorer") return "Explorer";
      if (normalized === "worker") return "Worker";
      if (normalized === "reviewer") return "Reviewer";
      if (normalized === "unassigned") return "Unassigned";
      return explicit;
    }
    return rootTreeAgentV2(run, agent && agent.label) ? "Root" : "Unassigned";
  }
  function agentRoleGroupsV2(scopeRuns) {
    var groups = {};
    list(scopeRuns).forEach(function (run) { agentList(run).forEach(function (agent) {
      var role = agentRoleV2(run, agent);
      if (!groups[role]) groups[role] = { role: role, instances: 0, tokens: 0, tools: 0, models: {} };
      var group = groups[role]; var usage = tokens(agent.token_usage); var model = modelName(agent);
      group.instances += 1; group.tokens += usage.total_tokens; group.tools += positive(agent.tool_count); group.models[model] = (group.models[model] || 0) + usage.total_tokens;
    }); });
    return groups;
  }
  function runTokens(run) { return tokens(run && run.analysis && run.analysis.total_token_usage); }
  function runSearchText(run) { var fields = [run.label, run.status, severity(run), projectLabelV2(run)].concat(list(run.quality_flags)); agentList(run).forEach(function (agent) { fields.push(agent.label, agent.model, agent.role, agent.effort, agent.fork_mode); }); diagnostics(run).forEach(function (item) { fields.push(item.code, item.message, item.severity, item.evidence_level); }); return fields.filter(function (value) { return value !== null && value !== undefined; }).join(" ").toLowerCase(); }
  function modelName(agent) { return String((agent && agent.model) || "unavailable"); }
  function modelColor(model) { var index = modelOrder.indexOf(String(model)); return colors[(index < 0 ? modelOrder.length : index) % colors.length]; }
  function projectColor(project) { var index = projectOrder.indexOf(String(project)); return colors[(index < 0 ? projectOrder.length : index) % colors.length]; }
  function catalogModels() { return catalog && typeof catalog === "object" && catalog.models && typeof catalog.models === "object" ? catalog.models : (catalog && typeof catalog === "object" ? catalog : {}); }
  function rateFor(model) { var value = catalogModels()[model]; return value && typeof value === "object" ? value : null; }
  function costForTokens(model, value) {
    var usage = tokens(value); var rate = rateFor(model); var parts = { uncached: Math.max(usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens, 0), cached: usage.cached_input_tokens, cache_write: usage.cache_write_input_tokens, output: usage.output_tokens }; var names = Object.keys(parts); var cost = 0; var pricedTokens = 0; var ratedParts = 0;
    names.forEach(function (name) { var rateValue = rate && number(rate[name]); if (rateValue > 0 || (rateValue === 0 && rate && Object.prototype.hasOwnProperty.call(rate, name))) { cost += parts[name] * rateValue / 1000000; pricedTokens += parts[name]; ratedParts += 1; } });
    var nonZeroParts = names.filter(function (name) { return parts[name] > 0; }).length;
    return { cost: cost, pricedTokens: pricedTokens, totalTokens: usage.total_tokens, available: Boolean(rate && ratedParts), partial: Boolean(rate && ratedParts && ratedParts < nonZeroParts), unavailable: !rate || !ratedParts };
  }
  function groupsFor(scopeRuns) { var groups = {}; scopeRuns.forEach(function (run) { agentList(run).forEach(function (agent) { var key = modelName(agent); var usage = tokens(agent.token_usage); var priced = costForTokens(key, usage); if (!groups[key]) groups[key] = { model: key, tokens: 0, cost: 0, pricedTokens: 0, partial: false, available: false }; groups[key].tokens += usage.total_tokens; groups[key].cost += priced.cost; groups[key].pricedTokens += priced.pricedTokens; groups[key].partial = groups[key].partial || priced.partial; groups[key].available = groups[key].available || priced.available; }); }); return groups; }
  function runCost(run) { var total = { cost: 0, pricedTokens: 0, totalTokens: 0, available: false, partial: false, unavailable: false }; agentList(run).forEach(function (agent) { var result = costForTokens(modelName(agent), agent.token_usage); total.cost += result.cost; total.pricedTokens += result.pricedTokens; total.totalTokens += result.totalTokens; total.available = total.available || result.available; total.partial = total.partial || result.partial || result.unavailable; total.unavailable = total.unavailable || result.unavailable; }); return total; }
  function coverage() { var groups = groupsFor(runs); var keys = Object.keys(groups); var priced = keys.filter(function (key) { return groups[key].available; }); var totalTokens = keys.reduce(function (sum, key) { return sum + groups[key].tokens; }, 0); var pricedTokens = keys.reduce(function (sum, key) { return sum + groups[key].pricedTokens; }, 0); return { models: keys.length, pricedModels: priced.length, tokenShare: totalTokens ? pricedTokens / totalTokens : 0 }; }
  function dayKey(run) { var parsed = Date.parse(run && run.end_at); if (isNaN(parsed)) return "unavailable"; var date = new Date(parsed); function pad(value) { return value < 10 ? "0" + value : String(value); } return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()); }
  function dayLabel(key) { if (key === "unavailable") return "Unknown date"; var parts = key.split("-"); var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])); return date.toLocaleDateString("en-US", { month: "short", day: "numeric" }); }
  function dateLabel(run) { return dayLabel(dayKey(run)); }
  function dailyData() { var days = {}; runs.forEach(function (run) { var key = dayKey(run); if (!days[key]) days[key] = { key: key, total: 0, cost: 0, models: {} }; var day = days[key]; day.total += runTokens(run).total_tokens; day.cost += runCost(run).cost; agentList(run).forEach(function (agent) { var model = modelName(agent); day.models[model] = (day.models[model] || 0) + tokens(agent.token_usage).total_tokens; }); }); return Object.keys(days).sort().map(function (key) { return days[key]; }); }
  function make(tag, className, text) { var node = document.createElement(tag); if (className) node.className = className; if (text !== undefined && text !== null) node.textContent = String(text); return node; }
  function svg(tag, attrs) { var node = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); }); return node; }
  function resetSvg(id, label) { var target = el(id); target.replaceChildren(); target.setAttribute("aria-label", label); target.setAttribute("viewBox", "0 0 720 222"); return target; }
  function addTitle(node, text) { var title = svg("title", {}); title.textContent = text; node.appendChild(title); }
  function drawTrend(id, points, lines, formatter, emptyText) {
    var target = resetSvg(id, emptyText); if (!points.length) { var empty = svg("text", { x: 360, y: 112, "text-anchor": "middle", "class": "chart-empty" }); empty.textContent = emptyText; target.appendChild(empty); return; }
    var left = 45, right = 14, top = 14, bottom = 31, width = 720 - left - right, height = 222 - top - bottom; var maxValue = Math.max.apply(null, lines.reduce(function (all, line) { return all.concat(line.values); }, []).concat([1]));
    [0, .5, 1].forEach(function (ratio) { var y = top + height * (1 - ratio); target.appendChild(svg("line", { x1: left, y1: y, x2: 720 - right, y2: y, "class": "chart-gridline" })); var label = svg("text", { x: left - 7, y: y + 3, "text-anchor": "end", "class": "chart-axis-label" }); label.textContent = formatter(maxValue * ratio); target.appendChild(label); });
    var xAt = function (index) { return points.length === 1 ? left + width / 2 : left + width * index / (points.length - 1); }; var yAt = function (value) { return top + height * (1 - value / maxValue); };
    var step = Math.max(1, Math.ceil(points.length / 6)); points.forEach(function (point, index) { if (index % step === 0 || index === points.length - 1) { var label = svg("text", { x: xAt(index), y: 214, "text-anchor": "middle", "class": "chart-axis-label" }); label.textContent = dayLabel(point.key); target.appendChild(label); } });
    lines.forEach(function (line, lineIndex) { var path = line.values.map(function (value, index) { return (index ? "L" : "M") + xAt(index) + " " + yAt(value); }).join(" "); var pathNode = svg("path", { d: path, "class": lineIndex === 0 ? (id === "daily-cost-chart" ? "chart-cost" : "chart-total") : "chart-model", stroke: line.color }); addTitle(pathNode, line.name + ": " + line.values.map(function (value, index) { return dayLabel(points[index].key) + " " + formatter(value); }).join(", ")); target.appendChild(pathNode); if (lineIndex === 0) line.values.forEach(function (value, index) { var pointNode = svg("circle", { cx: xAt(index), cy: yAt(value), r: 3.2, fill: line.color, "class": "chart-point" }); addTitle(pointNode, line.name + " · " + dayLabel(points[index].key) + " · " + formatter(value)); target.appendChild(pointNode); }); });
  }
  function renderDailyCharts() { var points = dailyData(); var modelKeys = {}; points.forEach(function (point) { Object.keys(point.models).forEach(function (key) { modelKeys[key] = true; }); }); var keys = Object.keys(modelKeys).sort(); var totalLine = { name: "Total", color: "#242725", values: points.map(function (point) { return point.total; }) }; var modelLines = keys.map(function (key) { return { name: key, color: modelColor(key), values: points.map(function (point) { return point.models[key] || 0; }) }; }); drawTrend("daily-tokens-chart", points, [totalLine].concat(modelLines), compact, "No token data."); drawTrend("daily-cost-chart", points, [{ name: "API equivalent", color: "#3e765d", values: points.map(function (point) { return point.cost; }) }], fmtMoney, "No cost data."); var tokenLegend = el("daily-tokens-legend"); tokenLegend.replaceChildren(); [{ name: "Total", color: totalLine.color }].concat(modelLines.map(function (line) { return { name: line.name, color: line.color }; })).forEach(function (item) { var row = make("span", "legend-item"); var swatch = make("span", "legend-swatch"); swatch.style.background = item.color; row.appendChild(swatch); row.appendChild(make("span", "", item.name)); tokenLegend.appendChild(row); }); var coverageNote = coverage(); el("daily-cost-note").textContent = coverageNote.models ? (coverageNote.pricedModels + "/" + coverageNote.models + " models priced · unknown models remain unpriced") : "No model pricing data."; }
  function costStatus(result) { if (!result.available) return "—"; return fmtMoney(result.cost) + (result.partial ? " · partial" : ""); }
  function renderModelShare() { var target = el("model-share-chart"); target.replaceChildren(); target.setAttribute("viewBox", "0 0 220 190"); var legend = el("model-share-legend"); legend.replaceChildren(); var groups = groupsFor(runs); var items = Object.keys(groups).map(function (key) { return groups[key]; }).sort(function (a, b) { return b.tokens - a.tokens || a.model.localeCompare(b.model); }); var total = items.reduce(function (sum, item) { return sum + item.tokens; }, 0); var cx = 110, cy = 91, radius = 54, circumference = 2 * Math.PI * radius; target.appendChild(svg("circle", { cx: cx, cy: cy, r: radius, "class": "donut-track" })); var offset = 0; items.forEach(function (item) { var share = total ? item.tokens / total : 0; var segment = svg("circle", { cx: cx, cy: cy, r: radius, "class": "donut-segment", stroke: modelColor(item.model), "stroke-dasharray": (share * circumference) + " " + circumference, "stroke-dashoffset": -offset }); addTitle(segment, item.model + " · " + fmtPct(share * 100) + " · " + fmtInt(item.tokens) + " tokens · " + costStatus(item)); target.appendChild(segment); offset += share * circumference; }); var center = svg("text", { x: cx, y: cy - 2, "class": "donut-center" }); center.textContent = compact(total); target.appendChild(center); var centerNote = svg("text", { x: cx, y: cy + 14, "class": "donut-center-note" }); centerNote.textContent = "tokens"; target.appendChild(centerNote); if (!items.length) { var empty = svg("text", { x: cx, y: cy + 4, "class": "chart-empty", "text-anchor": "middle" }); empty.textContent = "No data"; target.appendChild(empty); } items.forEach(function (item) { var row = make("div", "model-legend-row"); var swatch = make("span", "legend-swatch"); swatch.style.background = modelColor(item.model); row.appendChild(swatch); var name = make("span", "model-legend-name", item.model); name.title = item.model; row.appendChild(name); var values = make("span", "model-legend-values", fmtPct(total ? item.tokens / total * 100 : 0) + " · " + compact(item.tokens) + " · " + costStatus(item)); values.title = fmtInt(item.tokens) + " tokens · " + costStatus(item); row.appendChild(values); legend.appendChild(row); }); if (!items.length) legend.appendChild(make("span", "muted", "No model data.")); }
  function renderQuota() {
    var target = resetSvg("quota-chart", "Shared quota percentage line"); var points = runs.map(function (run, index) { var quota = run.analysis && run.analysis.quota ? run.analysis.quota : {}; var parsed = Date.parse(quota.observed_at || run.end_at); return { used: typeof quota.current_used_percent === "number" ? quota.current_used_percent : null, delta: typeof quota.observed_delta_percent === "number" ? quota.observed_delta_percent : null, reset: quota.resets_at || null, observed: isNaN(parsed) ? null : parsed, order: index, label: run.label || "run" }; }).filter(function (point) { return point.used !== null; }); points.sort(function (a, b) { if (a.observed !== null && b.observed !== null) return a.observed - b.observed; if (a.observed !== null) return -1; if (b.observed !== null) return 1; return a.order - b.order; }); el("quota-note").textContent = points.length ? points.length + " observations · percentage only" : "No comparable observations."; if (!points.length) { var empty = svg("text", { x: 360, y: 112, "text-anchor": "middle", "class": "chart-empty" }); empty.textContent = "No comparable observations."; target.appendChild(empty); return; }
    var left = 45, right = 14, top = 14, height = 222 - top - 31, width = 720 - left - right; [0, 50, 100].forEach(function (value) { var y = top + height * (1 - value / 100); target.appendChild(svg("line", { x1: left, y1: y, x2: 720 - right, y2: y, "class": "chart-gridline" })); var label = svg("text", { x: left - 7, y: y + 3, "text-anchor": "end", "class": "chart-axis-label" }); label.textContent = value + "%"; target.appendChild(label); }); var xAt = function (index) { return points.length === 1 ? left + width / 2 : left + width * index / (points.length - 1); }; var yAt = function (value) { return top + height * (1 - Math.max(0, Math.min(100, value)) / 100); }; var segments = [[]]; points.forEach(function (point, index) { var previous = index ? points[index - 1] : null; var broke = Boolean(previous && (point.used < previous.used || (point.delta !== null && point.delta < 0) || (previous.reset && point.reset && previous.reset !== point.reset))); if (broke) segments.push([]); segments[segments.length - 1].push({ point: point, index: index }); }); segments.forEach(function (segment) { if (!segment.length) return; var path = segment.map(function (entry, index) { return (index ? "L" : "M") + xAt(entry.index) + " " + yAt(entry.point.used); }).join(" "); var pathNode = svg("path", { d: path, "class": "chart-quota" }); addTitle(pathNode, segment.map(function (entry) { return entry.point.label + " · " + fmtPct(entry.point.used); }).join(", ")); target.appendChild(pathNode); }); points.forEach(function (point, index) { var isBreak = index > 0 && (point.used < points[index - 1].used || (point.delta !== null && point.delta < 0) || (points[index - 1].reset && point.reset && points[index - 1].reset !== point.reset)); var marker = svg("circle", { cx: xAt(index), cy: yAt(point.used), r: isBreak ? 4 : 3, fill: isBreak ? "#b25349" : "#56798a", "class": "chart-point" }); addTitle(marker, point.label + " · " + fmtPct(point.used) + (isBreak ? " · reset / decrease break" : "")); target.appendChild(marker); if (isBreak) target.appendChild(svg("line", { x1: xAt(index), y1: top, x2: xAt(index), y2: top + height, stroke: "#b25349", "stroke-width": 1, "stroke-dasharray": "3 5", opacity: .65 })); }); var step = Math.max(1, Math.ceil(points.length / 6)); points.forEach(function (point, index) { if (index % step === 0 || index === points.length - 1) { var label = svg("text", { x: xAt(index), y: 214, "text-anchor": "middle", "class": "chart-axis-label" }); label.textContent = point.label; target.appendChild(label); } });
  }
  function renderKpis() { var summary = report.summary || {}; var totalCost = runs.reduce(function (sum, run) { return sum + runCost(run).cost; }, 0); var cover = coverage(); var costNode = el("kpi-cost"); var costPartial = cover.pricedModels > 0 && cover.pricedModels < cover.models; costNode.textContent = cover.pricedModels ? fmtMoney(totalCost) + (costPartial ? " · partial" : "") : "—"; costNode.title = cover.pricedModels ? "Priced portion: " + fmtMoney(totalCost) + (costPartial ? "; one or more observed models are unpriced" : "") : "No catalog rates matched the observed models"; el("kpi-tokens").textContent = compact(summary.total_token_usage && summary.total_token_usage.total_tokens); el("kpi-tokens").title = fmtInt(summary.total_token_usage && summary.total_token_usage.total_tokens) + " total tokens"; el("kpi-runs").textContent = fmtInt(summary.run_count); el("kpi-runs").title = fmtInt(summary.run_count) + " runs"; el("kpi-coverage").textContent = cover.models ? fmtPct(cover.pricedModels / cover.models * 100) : "—"; el("kpi-coverage-note").textContent = cover.models ? cover.pricedModels + "/" + cover.models + " models · " + fmtPct(cover.tokenShare * 100) + " tokens" : "no observed models"; var quota = summary.latest_quota || {}; el("kpi-quota").textContent = fmtPct(quota.current_used_percent); el("kpi-quota-note").textContent = windowLabel(quota.window_minutes) + " · no USD"; el("generated-at").textContent = full(report.generated_at); el("since").textContent = full(report.since); el("schema-version").textContent = full(report.schema_version); var asOf = catalog && catalog.as_of ? String(catalog.as_of) : "unavailable"; el("pricing-basis").textContent = "OpenAI Standard short-context rate snapshot · " + asOf + "."; }
  function populateFilters() { var status = el("status-filter"); var severitySelect = el("severity-filter"); var statuses = {}; var severities = {}; runs.forEach(function (run) { statuses[String(run.status || "unknown")] = true; severities[severity(run)] = true; }); Object.keys(statuses).sort().forEach(function (value) { status.appendChild(make("option", "", value)); }); Object.keys(severities).sort(function (a, b) { return severityWeight(b) - severityWeight(a) || a.localeCompare(b); }).forEach(function (value) { severitySelect.appendChild(make("option", "", value)); }); }
  function matches(run) { var status = el("status-filter").value; var selectedSeverity = el("severity-filter").value; var keyword = el("keyword-filter").value.trim().toLowerCase(); return (!status || String(run.status || "unknown") === status) && (!selectedSeverity || severity(run) === selectedSeverity) && (!keyword || runSearchText(run).indexOf(keyword) !== -1); }
  function runModelGroups(run) { var groups = groupsFor([run]); return Object.keys(groups).map(function (key) { return groups[key]; }).sort(function (a, b) { return b.tokens - a.tokens || a.model.localeCompare(b.model); }); }
  function renderStack(run) { var box = make("div", "stacked"); var groups = runModelGroups(run); var total = groups.reduce(function (sum, group) { return sum + group.tokens; }, 0); groups.forEach(function (group) { var segment = make("span", "stacked-segment"); segment.style.width = (total ? group.tokens / total * 100 : 0) + "%"; segment.style.background = modelColor(group.model); segment.title = group.model + " · " + fmtInt(group.tokens) + " tokens"; box.appendChild(segment); }); box.title = groups.map(function (group) { return group.model + ": " + fmtInt(group.tokens); }).join(" · ") || "No model data"; return box; }
  function renderRows() { var body = el("run-rows"); body.replaceChildren(); filteredIndexes = []; runs.forEach(function (run, index) { if (matches(run)) filteredIndexes.push(index); }); el("run-count-note").textContent = filteredIndexes.length + " shown"; el("runs-empty").hidden = filteredIndexes.length > 0; el("runs-table-wrap").hidden = filteredIndexes.length === 0; filteredIndexes.forEach(function (index) { var run = runs[index]; var row = document.createElement("tr"); row.tabIndex = 0; row.dataset.runIndex = String(index); row.setAttribute("aria-label", "Open " + String(run.label || "run")); row.addEventListener("click", function () { openRun(index); }); row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openRun(index); } else if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); var position = filteredIndexes.indexOf(index); var next = event.key === "ArrowDown" ? Math.min(filteredIndexes.length - 1, position + 1) : Math.max(0, position - 1); var nextIndex = filteredIndexes[next]; var nextRow = body.querySelector('[data-run-index="' + nextIndex + '"]'); if (nextRow) nextRow.focus(); } }); var primary = make("div", "run-primary"); primary.appendChild(make("span", "run-date", dateLabel(run))); primary.appendChild(make("span", "run-label", run.label || "run")); var primaryCell = make("td"); primaryCell.appendChild(primary); row.appendChild(primaryCell); row.appendChild(make("td", "status", run.status || "unknown")); var mixCell = make("td"); mixCell.appendChild(renderStack(run)); row.appendChild(mixCell); var tokenCell = make("td", "numeric", compact(runTokens(run).total_tokens)); tokenCell.title = fmtInt(runTokens(run).total_tokens) + " total tokens"; row.appendChild(tokenCell); var cost = runCost(run); var costCell = make("td", "numeric"); var costNode = make("span", "cost-text " + (cost.available ? (cost.partial ? "partial" : "") : "unavailable"), cost.available ? fmtMoney(cost.cost) : "—"); costNode.title = cost.available ? fmtMoney(cost.cost) + " priced portion" : "No matching catalog rate"; costCell.appendChild(costNode); row.appendChild(costCell); body.appendChild(row); }); }
  function renderTree(flatTree, parent) { var items = list(flatTree); var byLabel = {}; var roots = []; items.forEach(function (item) { byLabel[item.label] = { label: item.label, children: [] }; }); items.forEach(function (item) { var node = byLabel[item.label]; var parentNode = item.parent_label ? byLabel[item.parent_label] : null; if (parentNode && parentNode !== node) parentNode.children.push(node); else roots.push(node); }); function append(nodes, target) { var listNode = make("ul"); nodes.forEach(function (node) { var item = make("li", "", node.label || "agent"); if (node.children.length) append(node.children, item); listNode.appendChild(item); }); target.appendChild(listNode); } parent.replaceChildren(); if (roots.length) append(roots, parent); else parent.appendChild(make("p", "muted", "No agent tree.")); }
  function renderModelSummary(run) { var target = el("detail-model-summary"); target.replaceChildren(); var groups = runModelGroups(run); var total = groups.reduce(function (sum, group) { return sum + group.tokens; }, 0); if (!groups.length) { target.appendChild(make("p", "muted", "No model records.")); return; } groups.forEach(function (group) { var card = make("article", "model-summary-card"); var heading = make("h4", "", group.model); heading.title = group.model; card.appendChild(heading); var value = make("strong", "", compact(group.tokens)); value.title = fmtInt(group.tokens) + " tokens"; card.appendChild(value); card.appendChild(make("p", "", fmtPct(total ? group.tokens / total * 100 : 0) + " share · " + fmtInt(group.tokens) + " tokens")); card.appendChild(make("p", "", costStatus(group))); card.style.borderTopColor = modelColor(group.model); target.appendChild(card); }); }
  function renderAgentBreakdown(run) { var target = el("agent-breakdown"); target.replaceChildren(); var agents = agentList(run); if (!agents.length) { target.appendChild(make("p", "muted", "No agent records.")); return; } var head = make("div", "agent-head"); ["Agent", "Model", "Role", "Tools", "Token card"].forEach(function (label) { head.appendChild(make("span", "", label)); }); target.appendChild(head); agents.forEach(function (agent) { var row = make("div", "agent-row"); row.appendChild(make("div", "", full(agent.label))); row.appendChild(make("div", "", full(agent.model))); row.appendChild(make("div", "", full(agent.role))); row.appendChild(make("div", "numeric", fmtInt(agent.tool_count))); var usage = tokens(agent.token_usage); var tokenCard = make("div", "agent-token-card"); tokenCard.appendChild(make("span", "", "tokens")); var tokenValue = make("strong", "", compact(usage.total_tokens)); tokenValue.title = fmtInt(usage.total_tokens) + " total tokens"; tokenCard.appendChild(tokenValue); row.appendChild(tokenCard); var breakdown = make("div", "token-breakdown"); [["in", usage.input_tokens], ["cached", usage.cached_input_tokens], ["write", usage.cache_write_input_tokens], ["out", usage.output_tokens], ["reason", usage.reasoning_output_tokens]].forEach(function (entry) { var part = make("span", "", entry[0] + " " + compact(entry[1])); part.title = entry[0] + ": " + fmtInt(entry[1]); breakdown.appendChild(part); }); row.appendChild(breakdown); target.appendChild(row); }); }
  function renderDiagnostics(run) { var target = el("detail-diagnostics"); target.replaceChildren(); var items = diagnostics(run); if (!items.length) { target.appendChild(make("li", "", "No diagnostics.")); return; } items.forEach(function (item) { var li = make("li", "diagnostic-item"); var head = make("div", "diagnostic-head"); var level = String(item.severity || "INFO").toLowerCase(); head.appendChild(make("span", "severity " + level, String(item.severity || "INFO"))); head.appendChild(make("span", "diagnostic-code", item.code || "unknown")); li.appendChild(head); li.appendChild(make("div", "diagnostic-message", item.message || "unavailable")); var evidence = String(item.evidence_level || "unavailable"); var labels = list(item.agent_labels); li.appendChild(make("div", "evidence", "Evidence: " + evidence + (labels.length ? " · " + labels.join(", ") : ""))); target.appendChild(li); }); }
  function renderDetail(run) { renderModelSummary(run); renderAgentBreakdown(run); renderDiagnostics(run); var limitations = el("detail-limitations"); limitations.replaceChildren(); var allLimitations = list(run.analysis && run.analysis.limitations); if (!allLimitations.length) limitations.appendChild(make("li", "", "No additional limitations.")); else allLimitations.forEach(function (item) { limitations.appendChild(make("li", "", item)); }); renderTree(run.agent_tree, el("agent-tree")); var quota = run.analysis && run.analysis.quota ? run.analysis.quota : {}; var cost = runCost(run); el("dialog-title").textContent = full(run.label); el("dialog-subtitle").textContent = dateLabel(run) + " · " + full(run.status) + " · " + (cost.available ? costStatus(cost) : "API equivalent unavailable"); el("detail-status").textContent = full(run.status); el("detail-severity").textContent = severity(run); el("detail-date").textContent = dateLabel(run); el("detail-start").textContent = full(run.start_at); el("detail-end").textContent = full(run.end_at); el("detail-time-evidence").textContent = full(run.time_evidence); el("detail-quality").textContent = list(run.quality_flags).length ? list(run.quality_flags).join(" · ") : "None"; el("detail-cost").textContent = cost.available ? costStatus(cost) : "Unavailable"; el("detail-quota").textContent = ["Current used: " + fmtPct(quota.current_used_percent), "Observed delta: " + fmtPct(quota.observed_delta_percent), "Window: " + full(quota.window_minutes) + " min", "Observed at: " + full(quota.observed_at), "Reset: " + full(quota.resets_at), "Evidence: " + full(quota.evidence_level)].join(" · "); }
  function selectRow(index) { selectedIndex = index; Array.prototype.forEach.call(document.querySelectorAll("#run-rows tr"), function (row) { row.setAttribute("aria-selected", row.dataset.runIndex === String(index) ? "true" : "false"); }); }
  function openRun(index) { if (!runs[index]) return; selectRow(index); renderDetail(runs[index]); var dialog = el("run-dialog"); if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("data-open", "true"); }
  function closeRun() { var dialog = el("run-dialog"); if (typeof dialog.close === "function" && dialog.open) dialog.close(); dialog.removeAttribute("data-open"); }
  /* Ratio-first dashboard behavior.  The history contract stays unchanged;
     this layer derives daily and proportional views from the safe projection. */
  var tokenTypesV2 = [
    { key: "uncached", label: "Uncached input", color: "#ff8264" },
    { key: "cached", label: "Cached input", color: "#21a993" },
    { key: "cache_write", label: "Cache write", color: "#8a6de9" },
    { key: "output", label: "Output", color: "#4a7bff" }
  ];
  var activeDayFilterV2 = "";
  var appliedFiltersV2 = { project: "", model: "", status: "", severity: "", keyword: "" };
  var sortStateV2 = { key: "date", direction: "desc" };
  var compositionProjectV2 = "";
  var costQuotaRangeV2 = "detail";

  function safeDateV2(value) {
    var stamp;
    if (typeof value === "number" && isFinite(value)) stamp = value < 1000000000000 ? value * 1000 : value;
    else stamp = Date.parse(value);
    return isNaN(stamp) ? null : new Date(stamp);
  }
  function localDayV2(value) {
    var date = safeDateV2(value);
    if (!date) return "unavailable";
    function pad(part) { return part < 10 ? "0" + part : String(part); }
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  }
  function dayKeyV2(run) { return localDayV2(run && run.end_at); }
  function projectLabelV2(run) { return run && run.project && run.project.label ? String(run.project.label) : "Unassigned"; }
  function projectEvidenceV2(run) { return run && run.project && run.project.evidence_level ? String(run.project.evidence_level) : "unavailable"; }
  function dayLabelV2(key) {
    if (!key || key === "unavailable") return "Unknown date";
    var parts = key.split("-");
    var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  function dateValueV2(run) { var date = safeDateV2(run && run.end_at); return date ? date.getTime() : -1; }
  function usagePartsV2(value) {
    var usage = tokens(value);
    return {
      uncached: Math.max(usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens, 0),
      cached: usage.cached_input_tokens,
      cache_write: usage.cache_write_input_tokens,
      output: usage.output_tokens,
      reasoning: usage.reasoning_output_tokens,
      reported: usage.total_tokens
    };
  }
  function emptyUsageV2() { return { input_tokens: 0, cached_input_tokens: 0, cache_write_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 0 }; }
  function addUsageV2(target, value) {
    var usage = tokens(value);
    Object.keys(target).forEach(function (key) { target[key] += usage[key] || 0; });
    return target;
  }
  function aggregateUsageV2(scopeRuns, model) {
    var result = emptyUsageV2();
    scopeRuns.forEach(function (run) {
      agentList(run).forEach(function (agent) {
        if (!model || modelName(agent) === model) addUsageV2(result, agent.token_usage);
      });
    });
    return result;
  }
  function partTotalV2(parts) { return tokenTypesV2.reduce(function (sum, item) { return sum + number(parts[item.key]); }, 0); }
  function compositionRunsV2() {
    return compositionProjectV2 ? runs.filter(function (run) { return projectLabelV2(run) === compositionProjectV2; }) : runs;
  }
  function allModelsV2(scopeRuns) {
    var source = scopeRuns || runs;
    var seen = {};
    source.forEach(function (run) { agentList(run).forEach(function (agent) { seen[modelName(agent)] = true; }); });
    return Object.keys(seen).sort();
  }
  function allProjectsV2() {
    var seen = {};
    runs.forEach(function (run) { seen[projectLabelV2(run)] = true; });
    return Object.keys(seen).sort(function (a, b) { if (a === "Unassigned") return 1; if (b === "Unassigned") return -1; return a.localeCompare(b); });
  }
  function coverageForV2(scopeRuns) {
    var groups = groupsFor(scopeRuns);
    var keys = Object.keys(groups);
    var total = keys.reduce(function (sum, key) { return sum + groups[key].tokens; }, 0);
    var priced = keys.reduce(function (sum, key) { return sum + (groups[key].available ? groups[key].tokens : 0); }, 0);
    return { models: keys.length, pricedModels: keys.filter(function (key) { return groups[key].available; }).length, tokenShare: total ? priced / total : 0 };
  }
  function syncFlowModelFilterV2() {
    var select = el("flow-model-filter"); var selected = select.value; var models = allModelsV2(compositionRunsV2());
    select.replaceChildren(make("option", "", "Overall")); select.options[0].value = "";
    models.forEach(function (model) { var option = make("option", "", model); option.value = model; select.appendChild(option); });
    if (selected && models.indexOf(selected) !== -1) select.value = selected;
    else select.value = "";
    return select.value;
  }
  function renderCompositionV2() {
    var selectedModel = syncFlowModelFilterV2();
    renderSankeyV2(selectedModel);
    renderAlluvialV2("model");
    renderAlluvialV2("type");
  }
  function projectGroupsV2(scopeRuns) {
    var groups = {};
    scopeRuns.forEach(function (run) {
      var key = projectLabelV2(run);
      if (!groups[key]) groups[key] = { project: key, evidence: projectEvidenceV2(run), runs: 0, tokens: 0, models: {} };
      var group = groups[key];
      group.runs += 1; group.tokens += runTokens(run).total_tokens;
      agentList(run).forEach(function (agent) { var model = modelName(agent); group.models[model] = (group.models[model] || 0) + tokens(agent.token_usage).total_tokens; });
    });
    return Object.keys(groups).map(function (key) { return groups[key]; });
  }
  function renderProjectOverviewV2() {
    var target = el("project-overview"); target.replaceChildren();
    var items = projectGroupsV2(runs).sort(function (a, b) { return b.tokens - a.tokens || b.runs - a.runs || a.project.localeCompare(b.project); });
    var total = items.reduce(function (sum, item) { return sum + item.tokens; }, 0);
    el("project-count-note").textContent = fmtInt(items.length) + " project" + (items.length === 1 ? "" : "s") + " · " + fmtInt(runs.length) + " runs";
    if (!items.length) { target.appendChild(make("p", "muted", "No project evidence in this selection.")); return; }
    items.forEach(function (item) {
      var share = total ? item.tokens / total * 100 : 0;
      var row = make("button", "project-row"); row.type = "button"; row.setAttribute("aria-label", "Filter runs to project " + item.project);
      row.addEventListener("click", function () { applyProjectFilterV2(item.project); });
      var identity = make("span", "project-identity"); identity.appendChild(make("span", "project-name", item.project)); identity.appendChild(make("span", "project-evidence", item.evidence === "derived" ? "root cwd name · derived" : "project evidence unavailable")); row.appendChild(identity);
      var visual = make("span", "project-viz"); var shareHead = make("span", "project-share-head"); shareHead.appendChild(make("span", "", "share of reported tokens")); shareHead.appendChild(make("strong", "", fmtPct(share))); visual.appendChild(shareHead);
      var shareTrack = make("span", "project-share-track"); var shareFill = make("span", "project-share-fill"); shareFill.style.width = share + "%"; shareFill.style.background = projectColor(item.project); shareTrack.appendChild(shareFill); visual.appendChild(shareTrack);
      var modelTrack = make("span", "project-model-track"); var modelTotal = Object.keys(item.models).reduce(function (sum, model) { return sum + item.models[model]; }, 0); Object.keys(item.models).sort(function (a, b) { return item.models[b] - item.models[a] || a.localeCompare(b); }).forEach(function (model) { var segment = make("span"); segment.style.width = (modelTotal ? item.models[model] / modelTotal * 100 : 0) + "%"; segment.style.background = modelColor(model); segment.title = model + " · " + fmtPct(modelTotal ? item.models[model] / modelTotal * 100 : 0); modelTrack.appendChild(segment); }); visual.appendChild(modelTrack); row.appendChild(visual);
      var support = make("span", "project-support"); support.appendChild(make("strong", "", fmtInt(item.runs) + " run" + (item.runs === 1 ? "" : "s"))); support.appendChild(make("span", "", compact(item.tokens) + " tokens")); row.appendChild(support);
      row.title = item.project + " · " + fmtPct(share) + " · " + fmtInt(item.tokens) + " reported tokens"; target.appendChild(row);
    });
  }
  function renderAgentStatisticsV2() {
    var target = el("agent-statistics"); target.replaceChildren();
    var groups = agentRoleGroupsV2(runs);
    var items = Object.keys(groups).map(function (key) { return groups[key]; }).sort(function (a, b) { return b.tokens - a.tokens || a.role.localeCompare(b.role); });
    var total = items.reduce(function (sum, item) { return sum + item.tokens; }, 0);
    if (!items.length) { target.appendChild(make("div", "empty-state", "No agent statistics available.")); return; }
    items.forEach(function (item) {
      var share = total ? item.tokens / total * 100 : 0;
      var row = make("article", "agent-stat-row");
      var identity = make("div", "agent-stat-role"); identity.appendChild(make("strong", "", item.role)); identity.appendChild(make("span", "", "role group · anonymous labels omitted")); row.appendChild(identity);
      var visual = make("div", "agent-stat-share"); var head = make("div", "agent-stat-share-head"); head.appendChild(make("span", "", "token share")); head.appendChild(make("strong", "", fmtPct(share))); visual.appendChild(head);
      var track = make("div", "agent-stat-track"); var fill = make("span"); fill.style.width = share + "%"; fill.style.background = modelColor(Object.keys(item.models).sort(function (a, b) { return item.models[b] - item.models[a] || a.localeCompare(b); })[0] || "unavailable"); track.appendChild(fill); visual.appendChild(track);
      var modelTrack = make("div", "agent-stat-model-track"); var modelTotal = Object.keys(item.models).reduce(function (sum, model) { return sum + item.models[model]; }, 0); Object.keys(item.models).sort(function (a, b) { return item.models[b] - item.models[a] || a.localeCompare(b); }).forEach(function (model) { var segment = make("span"); segment.style.width = (modelTotal ? item.models[model] / modelTotal * 100 : 0) + "%"; segment.style.background = modelColor(model); segment.title = model + " · " + fmtPct(modelTotal ? item.models[model] / modelTotal * 100 : 0); modelTrack.appendChild(segment); }); visual.appendChild(modelTrack);
      var modelCaption = Object.keys(item.models).sort(function (a, b) { return item.models[b] - item.models[a] || a.localeCompare(b); }).map(function (model) { return model + " " + fmtPct(modelTotal ? item.models[model] / modelTotal * 100 : 0); }).join(" · "); visual.appendChild(make("span", "agent-stat-model-caption", "model proportions · " + (modelCaption || "—"))); row.appendChild(visual);
      var metrics = make("div", "agent-stat-metrics"); metrics.appendChild(make("span", "", "instances " + fmtInt(item.instances))); metrics.appendChild(make("span", "", "tools " + fmtInt(item.tools))); row.appendChild(metrics);
      target.appendChild(row);
    });
  }
  function quotaPointV2(run, index) {
    var quota = run && run.analysis && run.analysis.quota ? run.analysis.quota : {};
    if (typeof quota.current_used_percent !== "number" || !isFinite(quota.current_used_percent)) return null;
    var observedDate = safeDateV2(quota.observed_at) || safeDateV2(run.end_at);
    return {
      run: run,
      index: index,
      used: quota.current_used_percent,
      delta: typeof quota.observed_delta_percent === "number" ? quota.observed_delta_percent : null,
      reset: quota.resets_at || null,
      window: quota.window_minutes,
      observed: observedDate ? observedDate.getTime() : index,
      day: observedDate ? localDayV2(observedDate.getTime()) : dayKeyV2(run)
    };
  }
  function quotaPointsV2(scopeRuns) {
    var source = scopeRuns || runs;
    return source.map(quotaPointV2).filter(function (point) { return point !== null; }).sort(function (a, b) { return a.observed - b.observed || a.index - b.index; });
  }
  function makeDayV2(key) { return { key: key, runs: [], cost: 0, costAvailable: false, costPartial: false, quotaDelta: 0, quotaSamples: 0, models: {}, usage: emptyUsageV2(), quota: null }; }
  function dailyDataV2(scopeRuns) {
    var source = scopeRuns || runs;
    var days = {};
    source.forEach(function (run) {
      var key = dayKeyV2(run);
      if (!days[key]) days[key] = makeDayV2(key);
      var day = days[key];
      var priced = runCost(run);
      day.runs.push(run);
      day.cost += priced.cost;
      day.costAvailable = day.costAvailable || priced.available;
      day.costPartial = day.costPartial || priced.partial || priced.unavailable;
      var quota = run && run.analysis && run.analysis.quota ? run.analysis.quota : {};
      if (typeof quota.observed_delta_percent === "number" && isFinite(quota.observed_delta_percent) && quota.observed_delta_percent > 0) { day.quotaDelta += quota.observed_delta_percent; day.quotaSamples += 1; }
      agentList(run).forEach(function (agent) {
        var model = modelName(agent);
        day.models[model] = (day.models[model] || 0) + tokens(agent.token_usage).total_tokens;
        addUsageV2(day.usage, agent.token_usage);
      });
    });
    quotaPointsV2(source).forEach(function (point) {
      if (!days[point.day]) days[point.day] = makeDayV2(point.day);
      if (!days[point.day].quota || point.observed >= days[point.day].quota.observed) days[point.day].quota = point;
    });
    return Object.keys(days).sort(function (a, b) { if (a === "unavailable") return 1; if (b === "unavailable") return -1; return a.localeCompare(b); }).map(function (key) { return days[key]; });
  }
  function costQuotaIndexV2(day) {
    if (!day || day.key === "unavailable" || !day.costAvailable || !(day.quotaDelta > 0)) return null;
    return { key: day.key, value: day.cost / day.quotaDelta, cost: day.cost, delta: day.quotaDelta, samples: day.quotaSamples, partial: day.costPartial, day: day };
  }
  function interactiveMarkV2(node, label, handler) {
    node.setAttribute("tabindex", "0");
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", label);
    node.classList.add("interactive-mark");
    node.addEventListener("click", handler);
    node.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); handler(); } });
  }
  var reportScanFieldsV2 = [
    { key: "files_seen", label: "Files seen" },
    { key: "files_parsed", label: "Files parsed" },
    { key: "invalid_identity_count", label: "Invalid identity" },
    { key: "duplicate_id_count", label: "Duplicate IDs" },
    { key: "orphan_count", label: "Orphan sessions" },
    { key: "cycle_count", label: "Cycles" },
    { key: "unreadable_count", label: "Unreadable files" },
    { key: "unknown_time_count", label: "Unknown timestamps" }
  ];
  function reportScanNumberV2(scan, key) { return positive(scan && scan[key]); }
  function reportScanHasIssuesV2(scan) {
    var partialRun = runs.some(function (run) { return String(run && run.status || "") === "partial" || list(run && run.quality_flags).some(function (flag) { return String(flag).toLowerCase() === "truncated" || String(flag).toLowerCase().indexOf("invalid") === 0; }); });
    return partialRun || reportScanNumberV2(scan, "files_parsed") < reportScanNumberV2(scan, "files_seen") || reportScanFieldsV2.slice(2).some(function (field) { return reportScanNumberV2(scan, field.key) > 0; });
  }
  function renderReportCoverageV2() {
    var scan = report && report.scan && typeof report.scan === "object" ? report.scan : {};
    var filesSeen = reportScanNumberV2(scan, "files_seen");
    var filesParsed = reportScanNumberV2(scan, "files_parsed");
    var hasIssues = reportScanHasIssuesV2(scan);
    var noFiles = filesSeen === 0;
    var noRuns = filesSeen > 0 && runs.length === 0;
    var coverage = el("report-coverage");
    if (coverage) coverage.dataset.state = noFiles ? "no-files" : (noRuns ? "no-valid-runs" : (hasIssues ? "partial" : "complete"));
    var summary = el("report-scan-summary");
    if (summary) summary.textContent = noFiles ? "No local JSONL files found" : (fmtInt(filesParsed) + " of " + fmtInt(filesSeen) + " files parsed · " + fmtInt(runs.length) + " valid runs");
    var level = el("report-scan-level");
    if (level) level.textContent = noFiles ? "No files" : (noRuns ? "No valid runs" : (hasIssues ? "Partial coverage" : "Complete coverage"));
    var grid = el("report-scan-grid");
    if (grid) {
      grid.replaceChildren();
      reportScanFieldsV2.forEach(function (field) {
        var card = make("div", "report-scan-stat");
        card.appendChild(make("span", "", field.label));
        card.appendChild(make("strong", "", fmtInt(reportScanNumberV2(scan, field.key))));
        grid.appendChild(card);
      });
    }
    var noFilesNode = el("report-empty-no-files");
    var noRunsNode = el("report-empty-no-valid-runs");
    var partialNode = el("report-partial-files");
    if (noFilesNode) noFilesNode.hidden = !noFiles;
    if (noRunsNode) noRunsNode.hidden = !noRuns;
    if (partialNode) partialNode.hidden = !(filesSeen > 0 && hasIssues);
    var limitations = el("report-limitations");
    if (limitations) {
      limitations.replaceChildren();
      var values = list(report && report.limitations);
      if (!values.length) limitations.appendChild(make("li", "", "No global limitations reported."));
      else values.forEach(function (item) { limitations.appendChild(make("li", "", item)); });
    }
  }
  function renderMetaAndTodayV2(days) {
    el("generated-at").textContent = full(report.generated_at);
    el("since").textContent = full(report.since);
    el("sidebar-generated-at").textContent = full(report.generated_at);
    el("sidebar-since").textContent = full(report.since);
    el("schema-version").textContent = full(report.schema_version);
    var generatedDay = localDayV2(report.generated_at);
    var todayRuns = runs.filter(function (run) { return dayKeyV2(run) === generatedDay; });
    var today = list(days).filter(function (day) { return day.key === generatedDay; })[0] || makeDayV2(generatedDay);
    var index = costQuotaIndexV2(today);
    var cover = coverageForV2(todayRuns);
    var groups = groupsFor(todayRuns);
    var leading = Object.keys(groups).sort(function (a, b) { return groups[b].tokens - groups[a].tokens || a.localeCompare(b); })[0];
    var groupedTokens = Object.keys(groups).reduce(function (sum, key) { return sum + groups[key].tokens; }, 0);
    var projects = projectGroupsV2(todayRuns);
    var leadingProject = projects.sort(function (a, b) { return b.tokens - a.tokens || a.project.localeCompare(b.project); })[0];
    var projectTokens = projects.reduce(function (sum, project) { return sum + project.tokens; }, 0);
    var reported = todayRuns.reduce(function (sum, run) { return sum + runTokens(run).total_tokens; }, 0);
    var currentQuota = null;
    var points = quotaPointsV2();
    points.forEach(function (point) { if (point.day === generatedDay) currentQuota = point; });
    var quotaIsToday = Boolean(currentQuota);
    if (!currentQuota && points.length) currentQuota = points[points.length - 1];
    el("today-date").textContent = generatedDay === "unavailable" ? "Unknown local day" : dayLabelV2(generatedDay);
    el("today-index").textContent = index ? fmtCostQuotaIndex(index.value) : "—";
    el("today-index-note").textContent = index ? (fmtMoney(index.cost) + " ÷ " + fmtQuotaPoints(index.delta) + " observed quota change" + (index.partial ? " · partial pricing" : "")) : (!todayRuns.length ? "No runs ended on this local day." : (!today.costAvailable ? "No exact public rate matched today's observed models." : "No positive run-observed quota delta today; the index is omitted."));
    el("today-cost").textContent = today.costAvailable ? fmtMoney(today.cost) : "—";
    el("today-quota-delta").textContent = today.quotaSamples ? fmtQuotaPoints(today.quotaDelta) : "—";
    el("today-quota-samples").textContent = fmtInt(today.quotaSamples);
    el("today-quota").textContent = currentQuota ? fmtPct(currentQuota.used) : "—";
    el("today-quota-note").textContent = currentQuota ? ((quotaIsToday ? "today" : dayLabelV2(currentQuota.day)) + " · " + windowLabel(currentQuota.window)) : "no observation";
    el("today-runs").textContent = fmtInt(todayRuns.length);
    el("today-tokens").textContent = compact(reported);
    el("today-tokens").title = fmtInt(reported) + " reported tokens";
    el("today-leading-project").textContent = leadingProject ? leadingProject.project + " · " + fmtPct(leadingProject.tokens / Math.max(1, projectTokens) * 100) : "—";
    el("today-leading-model").textContent = leading ? leading + " · " + fmtPct(groups[leading].tokens / Math.max(1, groupedTokens) * 100) : "—";
    el("today-coverage").textContent = cover.models ? fmtPct(cover.tokenShare * 100) : "—";
  }
  function populateFiltersV2() {
    var models = allModelsV2();
    models.forEach(function (model) { var option = make("option", "", model); option.value = model; el("model-filter").appendChild(option); });
    allProjectsV2().forEach(function (project) {
      var runOption = make("option", "", project); runOption.value = project; el("project-filter").appendChild(runOption);
      var flowOption = make("option", "", project); flowOption.value = project; el("flow-project-filter").appendChild(flowOption);
    });
    syncFlowModelFilterV2();
    var statuses = {};
    var severities = {};
    runs.forEach(function (run) { statuses[String(run.status || "unknown")] = true; severities[severity(run)] = true; });
    Object.keys(statuses).sort().forEach(function (value) { var option = make("option", "", value); option.value = value; el("status-filter").appendChild(option); });
    Object.keys(severities).sort(function (a, b) { return severityWeight(b) - severityWeight(a) || a.localeCompare(b); }).forEach(function (value) { var option = make("option", "", value); option.value = value; el("severity-filter").appendChild(option); });
  }
  function sankeyPathV2(x1, y1, x2, y2) { var middle = (x1 + x2) / 2; return "M" + x1 + " " + y1 + " C" + middle + " " + y1 + " " + middle + " " + y2 + " " + x2 + " " + y2; }
  function truncateV2(value, length) { var text = String(value); return text.length > length ? text.slice(0, length - 1) + "…" : text; }
  function renderSankeyV2(selectedModel) {
    var target = el("token-flow-chart");
    target.replaceChildren();
    target.setAttribute("viewBox", "0 0 760 340");
    var legend = el("token-flow-legend");
    legend.replaceChildren();
    var modelLegend = el("token-flow-model-legend");
    modelLegend.replaceChildren();
    var scopeRuns = compositionRunsV2();
    var grouped = {};
    scopeRuns.forEach(function (run) { agentList(run).forEach(function (agent) {
      var model = modelName(agent);
      if (selectedModel && model !== selectedModel) return;
      if (!grouped[model]) grouped[model] = emptyUsageV2();
      addUsageV2(grouped[model], agent.token_usage);
    }); });
    var allModels = Object.keys(grouped).map(function (model) { var parts = usagePartsV2(grouped[model]); return { model: model, parts: parts, total: partTotalV2(parts) }; }).filter(function (item) { return item.total > 0; }).sort(function (a, b) { return b.total - a.total || a.model.localeCompare(b.model); });
    var models = selectedModel ? allModels.filter(function (item) { return item.model === selectedModel; }) : allModels;
    var grand = models.reduce(function (sum, item) { return sum + item.total; }, 0);
    if (!grand) {
      var empty = svg("text", { x: 380, y: 170, "text-anchor": "middle", "class": "chart-empty" });
      empty.textContent = "No token composition available.";
      target.appendChild(empty);
      el("token-flow-note").textContent = "No mutually exclusive token partitions are available for this selection.";
      return;
    }
    var top = 40, height = 235, modelGap = 10, typeGap = 10;
    var scale = Math.min((height - modelGap * Math.max(0, models.length - 1)) / grand, (height - typeGap * (tokenTypesV2.length - 1)) / grand);
    var flowHeight = grand * scale;
    var sourceY = top + (height - flowHeight) / 2;
    var modelY = top;
    var modelNodes = [];
    models.forEach(function (item) { var nodeHeight = item.total * scale; modelNodes.push({ item: item, y: modelY, height: nodeHeight, sourceOffset: 0 }); modelY += nodeHeight + modelGap; });
    var typeTotals = {};
    tokenTypesV2.forEach(function (type) { typeTotals[type.key] = models.reduce(function (sum, model) { return sum + model.parts[type.key]; }, 0); });
    var typeY = top;
    var typeNodes = {};
    tokenTypesV2.forEach(function (type) { var nodeHeight = typeTotals[type.key] * scale; typeNodes[type.key] = { type: type, y: typeY, height: nodeHeight, offset: 0 }; typeY += nodeHeight + typeGap; });
    var sourceOffset = 0;
    modelNodes.forEach(function (entry) {
      var width = entry.height;
      var link = svg("path", { d: sankeyPathV2(27, sourceY + sourceOffset + entry.height / 2, 255, entry.y + entry.height / 2), stroke: modelColor(entry.item.model), "stroke-width": width, "class": "sankey-link" });
      addTitle(link, entry.item.model + " · " + fmtPct(entry.item.total / grand * 100));
      target.appendChild(link);
      sourceOffset += entry.height;
    });
    modelNodes.forEach(function (entry) {
      var localOffset = 0;
      tokenTypesV2.forEach(function (type) {
        var value = entry.item.parts[type.key];
        if (!value) return;
        var typeNode = typeNodes[type.key];
        var width = value * scale;
        var link = svg("path", { d: sankeyPathV2(269, entry.y + localOffset + width / 2, 605, typeNode.y + typeNode.offset + width / 2), stroke: type.color, "stroke-width": width, "class": "sankey-link" });
        addTitle(link, entry.item.model + " → " + type.label + " · " + fmtPct(value / grand * 100));
        target.appendChild(link);
        localOffset += width;
        typeNode.offset += width;
      });
    });
    var source = svg("rect", { x: 13, y: sourceY, width: 14, height: flowHeight, fill: "#242725", "class": "sankey-node" });
    target.appendChild(source);
    var sourceLabel = svg("text", { x: 13, y: Math.max(14, sourceY - 9), "class": "sankey-label" }); sourceLabel.textContent = selectedModel ? "Selected activity" : "Overall activity"; target.appendChild(sourceLabel);
    modelNodes.forEach(function (entry) {
      var node = svg("rect", { x: 255, y: entry.y, width: 14, height: entry.height, fill: modelColor(entry.item.model), "class": "sankey-node" });
      interactiveMarkV2(node, "Filter runs to " + entry.item.model, function () { applyChartFilterV2("", entry.item.model); });
      addTitle(node, entry.item.model + " · " + fmtPct(entry.item.total / grand * 100));
      target.appendChild(node);
      if (entry.height >= 20) {
        var label = svg("text", { x: 276, y: entry.y + entry.height / 2 - 2, "class": "sankey-label" }); label.textContent = truncateV2(entry.item.model, 20); target.appendChild(label);
        var value = svg("text", { x: 276, y: entry.y + entry.height / 2 + 10, "class": "sankey-value" }); value.textContent = fmtPct(entry.item.total / grand * 100); target.appendChild(value);
      }
    });
    tokenTypesV2.forEach(function (type) {
      var entry = typeNodes[type.key];
      var node = svg("rect", { x: 605, y: entry.y, width: 14, height: entry.height, fill: type.color, "class": "sankey-node" });
      addTitle(node, type.label + " · " + fmtPct(typeTotals[type.key] / grand * 100));
      node.setAttribute("aria-label", type.label + " · " + fmtPct(typeTotals[type.key] / grand * 100));
      target.appendChild(node);
      if (entry.height >= 20) {
        var label = svg("text", { x: 626, y: entry.y + entry.height / 2 - 2, "class": "sankey-label" }); label.textContent = type.label; target.appendChild(label);
        var value = svg("text", { x: 626, y: entry.y + entry.height / 2 + 10, "class": "sankey-value" }); value.textContent = fmtPct(typeTotals[type.key] / grand * 100); target.appendChild(value);
      }
      var item = make("div", "ratio-item"); var name = make("span", "legend-name"); var dot = make("span", "legend-dot"); dot.style.background = type.color; name.appendChild(dot); name.appendChild(make("span", "", type.label)); item.appendChild(name); item.appendChild(make("strong", "", fmtPct(typeTotals[type.key] / grand * 100))); legend.appendChild(item);
    });
    var modelLegendGrand = allModels.reduce(function (sum, item) { return sum + item.total; }, 0);
    allModels.forEach(function (item) {
      var modelButton = make("button", "legend-button"); modelButton.type = "button"; modelButton.setAttribute("aria-label", "Select model " + item.model + " for token flow"); modelButton.setAttribute("aria-pressed", selectedModel === item.model ? "true" : "false"); modelButton.addEventListener("click", function () { el("flow-model-filter").value = item.model; renderSankeyV2(item.model); });
      var modelNameNode = make("span", "legend-name"); var modelDot = make("span", "legend-dot"); modelDot.style.background = modelColor(item.model); modelNameNode.appendChild(modelDot); modelNameNode.appendChild(make("span", "", item.model)); modelButton.appendChild(modelNameNode); modelButton.appendChild(make("strong", "", fmtPct(modelLegendGrand ? item.total / modelLegendGrand * 100 : 0))); modelLegend.appendChild(modelButton);
    });
    var aggregate = aggregateUsageV2(scopeRuns, selectedModel);
    var aggregateParts = usagePartsV2(aggregate);
    var reasoningInsideOutput = aggregateParts.output ? aggregateParts.reasoning / aggregateParts.output * 100 : 0;
    el("token-flow-note").textContent = (selectedModel || "Overall") + " · billable-partition denominator " + compact(grand) + " · reasoning is " + fmtPct(reasoningInsideOutput) + " of output and is not added again. Reported total: " + compact(aggregateParts.reported) + ".";
  }
  function percentileV2(values, ratio) {
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    if (!sorted.length) return null;
    var position = (sorted.length - 1) * ratio; var lower = Math.floor(position); var upper = Math.ceil(position);
    if (lower === upper) return sorted[lower];
    return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
  }
  function costQuotaStatsV2(points) {
    var values = points.map(function (point) { return point.value; });
    return { median: percentileV2(values, .5), p75: percentileV2(values, .75), p90: percentileV2(values, .9), max: Math.max.apply(null, values) };
  }
  function costQuotaTooltipV2(point, outlier) {
    return dayLabelV2(point.key) + " · exact index " + fmtCostQuotaIndex(point.value) + " · exact cost " + fmtMoney(point.cost) + " · exact delta " + fmtQuotaPoints(point.delta) + " · samples " + fmtInt(point.samples) + " · " + (point.partial ? "partial pricing" : "full pricing") + (outlier ? " · exact value unchanged; geometry clamped to P90 in Detail view" : "");
  }
  function updateCostQuotaRangeButtonsV2() {
    var detail = el("cost-quota-detail"), fullRange = el("cost-quota-full-range");
    detail.setAttribute("aria-pressed", costQuotaRangeV2 === "detail" ? "true" : "false"); fullRange.setAttribute("aria-pressed", costQuotaRangeV2 === "full" ? "true" : "false");
  }
  function renderCostQuotaIndexV2(days) {
    var target = el("daily-pulse-chart"); target.replaceChildren(); target.setAttribute("viewBox", "0 0 760 246");
    var legend = el("daily-pulse-legend"); legend.replaceChildren(); updateCostQuotaRangeButtonsV2();
    var points = list(days).map(costQuotaIndexV2).filter(function (point) { return point !== null; });
    if (!points.length) { var empty = svg("text", { x: 380, y: 122, "text-anchor": "middle", "class": "chart-empty" }); empty.textContent = "No comparable cost/quota observations."; target.appendChild(empty); legend.appendChild(make("span", "legend-item", "Dates without priced cost or positive quota delta are omitted.")); return; }
    var stats = costQuotaStatsV2(points); var detail = costQuotaRangeV2 === "detail"; var valueMax = Math.max(detail ? stats.p90 : stats.max, .01);
    var defs = svg("defs", {}); var gradient = svg("linearGradient", { id: "index-area-gradient", x1: "0", y1: "0", x2: "0", y2: "1" }); gradient.appendChild(svg("stop", { offset: "0%", "stop-color": "#4a7bff", "stop-opacity": ".24" })); gradient.appendChild(svg("stop", { offset: "100%", "stop-color": "#4a7bff", "stop-opacity": "0" })); defs.appendChild(gradient); target.appendChild(defs);
    var left = 82, right = 20, top = 19, bottom = 36, width = 760 - left - right, height = 246 - top - bottom;
    [0, .5, 1].forEach(function (ratio) {
      var y = top + height * (1 - ratio);
      target.appendChild(svg("line", { x1: left, y1: y, x2: 760 - right, y2: y, "class": "chart-gridline" }));
      var axis = svg("text", { x: left - 8, y: y + 3, "text-anchor": "end", "class": "chart-axis-label axis-index" }); axis.textContent = fmtCostQuotaIndex(valueMax * ratio); target.appendChild(axis);
    });
    var xAt = function (index) { return points.length === 1 ? left + width / 2 : left + width * index / (points.length - 1); };
    var geometryValueV2 = function (point) { return detail ? Math.min(point.value, valueMax) : point.value; };
    var yAt = function (value) { return top + height * (1 - value / valueMax); };
    var linePath = points.map(function (point, index) { return (index ? "L" : "M") + xAt(index) + " " + yAt(geometryValueV2(point)); }).join(" ");
    var areaPath = linePath + " L" + xAt(points.length - 1) + " " + (top + height) + " L" + xAt(0) + " " + (top + height) + " Z";
    target.appendChild(svg("path", { d: areaPath, "class": "chart-index-area" }));
    var pathNode = svg("path", { d: linePath, "class": "chart-index-line" }); addTitle(pathNode, (detail ? "Detail" : "Full range") + " view · exact cost/quota index values remain in point tooltips"); target.appendChild(pathNode);
    points.forEach(function (point, index) {
      var outlier = detail && point.value > valueMax; var marker = svg("circle", { cx: xAt(index), cy: yAt(geometryValueV2(point)), r: outlier ? 7 : 5, fill: outlier ? "#c23a70" : (point.partial ? "#f0a126" : "#263f94"), "class": "chart-index-point" + (point.partial ? " chart-index-partial" : "") + (outlier ? " chart-index-outlier" : "") });
      addTitle(marker, costQuotaTooltipV2(point, outlier));
      interactiveMarkV2(marker, "Filter runs to " + dayLabelV2(point.key) + ". " + costQuotaTooltipV2(point, outlier), function () { applyChartFilterV2(point.key, ""); }); target.appendChild(marker);
    });
    var step = Math.max(1, Math.ceil(points.length / 7)); points.forEach(function (point, index) { if (index % step === 0 || index === points.length - 1) { var label = svg("text", { x: xAt(index), y: 238, "text-anchor": "middle", "class": "chart-axis-label" }); label.textContent = dayLabelV2(point.key); target.appendChild(label); } });
    var series = make("span", "legend-item"); var dot = make("span", "legend-swatch"); dot.style.background = "#263f94"; series.appendChild(dot); series.appendChild(make("span", "", "Indigo point · API-equivalent USD per +1 quota percentage point")); legend.appendChild(series);
    legend.appendChild(make("span", "legend-item", (detail ? "Detail · Y ceiling P90 " + fmtCostQuotaIndex(stats.p90) : "Full range · linear to max") + " · invalid dates skipped and connected"));
    [["median", stats.median], ["P75", stats.p75], ["P90", stats.p90], ["max", stats.max]].forEach(function (entry) { legend.appendChild(make("span", "legend-item index-stat", entry[0] + " " + fmtCostQuotaIndex(entry[1]))); });
    var outliers = points.filter(function (point) { return detail && point.value > valueMax; }).length;
    if (outliers) legend.appendChild(make("span", "legend-item", "Magenta point · " + fmtInt(outliers) + " outlier" + (outliers === 1 ? "" : "s") + " clamped to P90 geometry; exact values unchanged in tooltips"));
    if (points.some(function (point) { return point.partial; })) legend.appendChild(make("span", "legend-item", "Amber point · partial public-rate coverage"));
  }
  function alluvialPathV2(x1, top1, bottom1, x2, top2, bottom2) { var middle = (x1 + x2) / 2; return "M" + x1 + " " + top1 + " C" + middle + " " + top1 + " " + middle + " " + top2 + " " + x2 + " " + top2 + " L" + x2 + " " + bottom2 + " C" + middle + " " + bottom2 + " " + middle + " " + bottom1 + " " + x1 + " " + bottom1 + " Z"; }
  function renderAlluvialV2(kind, days) {
    var isModel = kind === "model";
    var id = isModel ? "daily-model-share-chart" : "daily-type-share-chart";
    var legendId = isModel ? "daily-model-share-legend" : "daily-type-share-legend";
    var target = el(id); target.replaceChildren(); target.setAttribute("viewBox", "0 0 720 286"); target.setAttribute("data-alluvial-kind", kind);
    var legend = el(legendId); legend.replaceChildren();
    var scopeRuns = compositionRunsV2();
    days = dailyDataV2(scopeRuns);
    var keys = isModel ? allModelsV2(scopeRuns) : tokenTypesV2.map(function (type) { return type.key; });
    var totals = {}; keys.forEach(function (key) { totals[key] = 0; });
    var validDays = list(days).filter(function (day) {
      if (!day || day.key === "unavailable") return false;
      var values = {};
      if (isModel) keys.forEach(function (key) { values[key] = number(day.models[key]); });
      else { var parts = usagePartsV2(day.usage); keys.forEach(function (key) { values[key] = number(parts[key]); }); }
      day._alluvialValues = values;
      day._alluvialTotal = keys.reduce(function (sum, key) { return sum + values[key]; }, 0);
      if (!(day._alluvialTotal > 0)) return false;
      keys.forEach(function (key) { totals[key] += values[key]; });
      return true;
    });
    if (isModel) keys.sort(function (a, b) { return totals[b] - totals[a] || a.localeCompare(b); });
    keys = keys.filter(function (key) { return totals[key] > 0; });
    if (!validDays.length || !keys.length) { var empty = svg("text", { x: 360, y: 142, "text-anchor": "middle", "class": "chart-empty" }); empty.textContent = "No daily proportions."; target.appendChild(empty); return; }
    var left = 45, right = 16, top = 20, bottom = 50, width = 720 - left - right, height = 286 - top - bottom;
    [0, 50, 100].forEach(function (value) { var y = top + height * (1 - value / 100); target.appendChild(svg("line", { x1: left, y1: y, x2: 720 - right, y2: y, "class": "chart-gridline" })); var label = svg("text", { x: left - 7, y: y + 3, "text-anchor": "end", "class": "chart-axis-label" }); label.textContent = fmtPct(value); target.appendChild(label); });
    var xAt = function (index) { return validDays.length === 1 ? left + width / 2 : left + width * index / (validDays.length - 1); };
    var nodeWidth = validDays.length > 10 ? 5 : 8;
    validDays.forEach(function (day, index) {
      var used = 0; day._alluvialSegments = {};
      target.appendChild(svg("line", { x1: xAt(index), y1: top, x2: xAt(index), y2: top + height, "class": "alluvial-date-line" }));
      keys.forEach(function (key) {
        var share = day._alluvialValues[key] / day._alluvialTotal;
        day._alluvialSegments[key] = { top: top + height * used, bottom: top + height * (used + share), share: share };
        used += share;
      });
    });
    validDays.slice(0, -1).forEach(function (day, index) {
      var next = validDays[index + 1];
      keys.forEach(function (key) {
        var first = day._alluvialSegments[key], second = next._alluvialSegments[key];
        if (!first.share && !second.share) return;
        var color = isModel ? modelColor(key) : tokenTypesV2.filter(function (type) { return type.key === key; })[0].color;
        var label = isModel ? key : tokenTypesV2.filter(function (type) { return type.key === key; })[0].label;
        var ribbon = svg("path", { d: alluvialPathV2(xAt(index) + nodeWidth / 2, first.top, first.bottom, xAt(index + 1) - nodeWidth / 2, second.top, second.bottom), fill: color, "class": "alluvial-ribbon" });
        addTitle(ribbon, label + " · " + dayLabelV2(day.key) + " " + fmtPct(first.share * 100) + " → " + dayLabelV2(next.key) + " " + fmtPct(second.share * 100) + " · share evolution, not token transfer"); target.appendChild(ribbon);
      });
    });
    validDays.forEach(function (day, index) {
      keys.forEach(function (key) {
        var segment = day._alluvialSegments[key]; if (!segment.share) return;
        var color = isModel ? modelColor(key) : tokenTypesV2.filter(function (type) { return type.key === key; })[0].color;
        var label = isModel ? key : tokenTypesV2.filter(function (type) { return type.key === key; })[0].label;
        var node = svg("rect", { x: xAt(index) - nodeWidth / 2, y: segment.top, width: nodeWidth, height: Math.max(.8, segment.bottom - segment.top), fill: color, "class": "alluvial-node" });
        addTitle(node, dayLabelV2(day.key) + " · " + label + " · " + fmtPct(segment.share * 100));
        interactiveMarkV2(node, "Filter " + dayLabelV2(day.key) + (isModel ? " and " + key : ""), function () { applyChartFilterV2(day.key, isModel ? key : ""); }); target.appendChild(node);
      });
    });
    var step = Math.max(1, Math.ceil(validDays.length / 7)); validDays.forEach(function (day, index) { if (index % step === 0 || index === validDays.length - 1) { var label = svg("text", { x: xAt(index), y: 278, "text-anchor": "middle", "class": "chart-axis-label" }); label.textContent = dayLabelV2(day.key); target.appendChild(label); } });
    var overall = keys.reduce(function (sum, key) { return sum + totals[key]; }, 0);
    keys.forEach(function (key) {
      var label = isModel ? key : tokenTypesV2.filter(function (type) { return type.key === key; })[0].label;
      var color = isModel ? modelColor(key) : tokenTypesV2.filter(function (type) { return type.key === key; })[0].color;
      var node = make(isModel ? "button" : "div", isModel ? "legend-button" : "ratio-item");
      if (isModel) { node.type = "button"; node.addEventListener("click", function () { applyChartFilterV2("", key); }); }
      var name = make("span", "legend-name"); var dot = make("span", "legend-dot"); dot.style.background = color; name.appendChild(dot); name.appendChild(make("span", "", label)); node.appendChild(name); node.appendChild(make("strong", "", "window " + fmtPct(overall ? totals[key] / overall * 100 : 0))); legend.appendChild(node);
    });
  }
  function modelGroupsV2(run) { return runModelGroups(run); }
  function shareBarV2(items, caption) {
    var wrapper = make("div", "mix-cell"); var track = make("div", "stacked"); var total = items.reduce(function (sum, item) { return sum + item.value; }, 0);
    items.forEach(function (item) { var segment = make("span", "stacked-segment"); segment.style.width = (total ? item.value / total * 100 : 0) + "%"; segment.style.background = item.color; segment.title = item.label + " · " + fmtPct(total ? item.value / total * 100 : 0); track.appendChild(segment); });
    wrapper.appendChild(track); wrapper.appendChild(make("span", "mix-caption", caption(items, total))); return wrapper;
  }
  function modelMixV2(run) {
    var items = modelGroupsV2(run).map(function (group) { return { label: group.model, value: group.tokens, color: modelColor(group.model) }; });
    return shareBarV2(items, function (values, total) { return values.length ? values.slice(0, 2).map(function (item) { return item.label + " " + fmtPct(total ? item.value / total * 100 : 0); }).join(" · ") : "No model data"; });
  }
  function tokenMixV2(run) {
    var parts = usagePartsV2(aggregateUsageV2([run], ""));
    var items = tokenTypesV2.map(function (type) { return { label: type.label, value: parts[type.key], color: type.color }; }).filter(function (item) { return item.value > 0; });
    return shareBarV2(items, function (values, total) { var cached = parts.cached + parts.cache_write; return "cached " + fmtPct(total ? cached / total * 100 : 0) + " · output " + fmtPct(total ? parts.output / total * 100 : 0); });
  }
  function readAppliedFiltersV2() {
    appliedFiltersV2.project = el("project-filter").value;
    appliedFiltersV2.model = el("model-filter").value;
    appliedFiltersV2.status = el("status-filter").value;
    appliedFiltersV2.severity = el("severity-filter").value;
    appliedFiltersV2.keyword = el("keyword-filter").value.trim().toLowerCase();
  }
  function matchesV2(run) {
    var modelMatch = !appliedFiltersV2.model || agentList(run).some(function (agent) { return modelName(agent) === appliedFiltersV2.model; });
    var projectMatch = !appliedFiltersV2.project || projectLabelV2(run) === appliedFiltersV2.project;
    return (!activeDayFilterV2 || dayKeyV2(run) === activeDayFilterV2) && projectMatch && modelMatch && (!appliedFiltersV2.status || String(run.status || "unknown") === appliedFiltersV2.status) && (!appliedFiltersV2.severity || severity(run) === appliedFiltersV2.severity) && (!appliedFiltersV2.keyword || runSearchText(run).indexOf(appliedFiltersV2.keyword) !== -1);
  }
  function quotaForRunV2(run) { var quota = run && run.analysis && run.analysis.quota ? run.analysis.quota : {}; return typeof quota.current_used_percent === "number" ? quota.current_used_percent : null; }
  function sortValueV2(run) { if (sortStateV2.key === "cost") return runCost(run).cost; if (sortStateV2.key === "quota") { var quota = quotaForRunV2(run); return quota === null ? -1 : quota; } return dateValueV2(run); }
  function updateSortButtonsV2() { Array.prototype.forEach.call(document.querySelectorAll(".sort-button"), function (button) { var active = button.dataset.sort === sortStateV2.key; button.dataset.active = active ? "true" : "false"; button.dataset.direction = active ? (sortStateV2.direction === "asc" ? "↑" : "↓") : "↕"; button.setAttribute("aria-sort", active ? (sortStateV2.direction === "asc" ? "ascending" : "descending") : "none"); }); }
  function runHoverStatV2(label, value) {
    var item = make("div", "run-hover-stat"); item.appendChild(make("span", "", label)); item.appendChild(make("strong", "", value)); return item;
  }
  function runHoverBreakdownV2(title, items, total) {
    var section = make("section", "run-hover-breakdown"); section.appendChild(make("h4", "", title));
    if (!items.length || !total) { section.appendChild(make("div", "run-hover-line", "No data")); return section; }
    items.forEach(function (item) {
      var line = make("div", "run-hover-line"); line.appendChild(make("span", "", item.label)); line.appendChild(make("strong", "", fmtPct(item.value / total * 100) + " · " + compact(item.value))); section.appendChild(line);
    });
    return section;
  }
  function renderRunHoverCardV2(run) {
    var card = el("run-hover-card"); card.replaceChildren();
    var head = make("div", "run-hover-head"); head.appendChild(make("strong", "", full(run.label))); head.appendChild(make("span", "", dayLabelV2(dayKeyV2(run)) + " · " + projectLabelV2(run))); card.appendChild(head);
    var agents = agentList(run); var tools = agents.reduce(function (sum, agent) { return sum + positive(agent.tool_count); }, 0); var cost = runCost(run); var quota = run && run.analysis && run.analysis.quota ? run.analysis.quota : {}; var delta = typeof quota.observed_delta_percent === "number" && isFinite(quota.observed_delta_percent) ? quota.observed_delta_percent : null;
    var stats = make("div", "run-hover-stats");
    stats.appendChild(runHoverStatV2("Reported tokens", compact(runTokens(run).total_tokens)));
    stats.appendChild(runHoverStatV2("Agents / tools", fmtInt(agents.length) + " / " + fmtInt(tools)));
    stats.appendChild(runHoverStatV2("Est. cost", cost.available ? fmtMoney(cost.cost) + (cost.partial ? " · partial" : "") : "—"));
    stats.appendChild(runHoverStatV2("Quota used", fmtPct(quota.current_used_percent)));
    stats.appendChild(runHoverStatV2("Observed delta", delta === null ? "—" : (delta > 0 ? "+" : "") + delta.toFixed(2) + " pp"));
    stats.appendChild(runHoverStatV2("State", full(run.status) + " · " + severity(run)));
    card.appendChild(stats);
    var modelItems = modelGroupsV2(run).map(function (group) { return { label: group.model, value: group.tokens }; }); var modelTotal = modelItems.reduce(function (sum, item) { return sum + item.value; }, 0);
    var parts = usagePartsV2(aggregateUsageV2([run], "")); var typeItems = tokenTypesV2.map(function (type) { return { label: type.label, value: parts[type.key] }; }).filter(function (item) { return item.value > 0; }); var typeTotal = typeItems.reduce(function (sum, item) { return sum + item.value; }, 0);
    var breakdowns = make("div", "run-hover-breakdowns"); breakdowns.appendChild(runHoverBreakdownV2("Models", modelItems, modelTotal)); breakdowns.appendChild(runHoverBreakdownV2("Token types", typeItems, typeTotal)); card.appendChild(breakdowns);
    card.appendChild(make("p", "run-hover-help", "Quota is account-level/shared · click or press Enter for full run detail"));
  }
  function positionRunHoverCardV2(event, row) {
    var card = el("run-hover-card"); if (card.hidden) return;
    var width = card.offsetWidth, height = card.offsetHeight, x, y;
    if (event && typeof event.clientX === "number") { x = event.clientX + 15; y = event.clientY + 15; }
    else { var rect = row.getBoundingClientRect(); x = rect.left + 20; y = rect.bottom + 8; if (y + height > window.innerHeight - 12) y = rect.top - height - 8; }
    if (x + width > window.innerWidth - 12) x = window.innerWidth - width - 12;
    if (y + height > window.innerHeight - 12) y = (event && typeof event.clientY === "number" ? event.clientY - height - 15 : window.innerHeight - height - 12);
    card.style.left = Math.max(12, x) + "px"; card.style.top = Math.max(12, y) + "px";
  }
  function showRunHoverCardV2(run, row, event) { renderRunHoverCardV2(run); var card = el("run-hover-card"); card.hidden = false; positionRunHoverCardV2(event, row); }
  function hideRunHoverCardV2() { var card = el("run-hover-card"); if (card) card.hidden = true; }
  function renderRowsV2() {
    var body = el("run-rows"); hideRunHoverCardV2(); body.replaceChildren();
    filteredIndexes = runs.map(function (run, index) { return { run: run, index: index }; }).filter(function (item) { return matchesV2(item.run); }).sort(function (a, b) { var first = sortValueV2(a.run), second = sortValueV2(b.run); var result = first === second ? String(a.run.label).localeCompare(String(b.run.label)) : first - second; return sortStateV2.direction === "asc" ? result : -result; }).map(function (item) { return item.index; });
    el("run-count-note").textContent = fmtInt(filteredIndexes.length) + " shown";
    el("runs-empty").hidden = filteredIndexes.length > 0;
    el("runs-table-wrap").hidden = filteredIndexes.length === 0;
    filteredIndexes.forEach(function (index) {
      var run = runs[index]; var row = document.createElement("tr"); row.tabIndex = 0; row.dataset.runIndex = String(index); row.id = "row-" + String(run.label || "run"); row.setAttribute("aria-label", "Open visual detail for " + String(run.label || "run")); row.setAttribute("aria-describedby", "run-hover-card");
      row.addEventListener("mouseenter", function (event) { showRunHoverCardV2(run, row, event); });
      row.addEventListener("mousemove", function (event) { positionRunHoverCardV2(event, row); });
      row.addEventListener("mouseleave", hideRunHoverCardV2);
      row.addEventListener("focus", function () { showRunHoverCardV2(run, row, null); });
      row.addEventListener("blur", hideRunHoverCardV2);
      row.addEventListener("click", function () { hideRunHoverCardV2(); openRunV2(index, true); });
      row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); hideRunHoverCardV2(); openRunV2(index, true); } else if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); var position = filteredIndexes.indexOf(index); var next = event.key === "ArrowDown" ? Math.min(filteredIndexes.length - 1, position + 1) : Math.max(0, position - 1); var nextRow = body.querySelector('[data-run-index="' + filteredIndexes[next] + '"]'); if (nextRow) nextRow.focus(); } });
      var primary = make("div", "run-primary"); primary.appendChild(make("span", "run-date", dayLabelV2(dayKeyV2(run)))); primary.appendChild(make("span", "run-label", run.label || "run")); var first = make("td"); first.appendChild(primary); row.appendChild(first);
      var projectCell = make("td"); projectCell.appendChild(make("span", "project-name", projectLabelV2(run))); row.appendChild(projectCell);
      var modelCell = make("td"); modelCell.appendChild(modelMixV2(run)); row.appendChild(modelCell);
      var typeCell = make("td"); typeCell.appendChild(tokenMixV2(run)); row.appendChild(typeCell);
      var cost = runCost(run); var costCell = make("td", "numeric", cost.available ? fmtMoney(cost.cost) : "—"); costCell.title = cost.available ? fmtMoney(cost.cost) + (cost.partial ? " priced portion · partial" : "") : "No exact catalog match"; row.appendChild(costCell);
      var quota = quotaForRunV2(run); var quotaCell = make("td", "numeric", quota === null ? "—" : fmtPct(quota)); quotaCell.title = "Account-level/shared observation · credits excluded"; row.appendChild(quotaCell);
      var stateCell = make("td"); stateCell.appendChild(make("span", "status", run.status || "unknown")); stateCell.appendChild(make("span", "mix-caption", severity(run))); row.appendChild(stateCell);
      body.appendChild(row);
    });
    updateSortButtonsV2();
    el("active-day-filter").hidden = !activeDayFilterV2;
    el("active-day-label").textContent = activeDayFilterV2 ? "Date · " + dayLabelV2(activeDayFilterV2) : "Day";
  }
  function applyChartFilterV2(day, model) {
    if (day) activeDayFilterV2 = day;
    if (model) { el("model-filter").value = model; el("flow-model-filter").value = model; appliedFiltersV2.model = model; renderSankeyV2(model); }
    renderRowsV2();
    var panel = el("runs-panel"); if (panel && typeof panel.scrollIntoView === "function") panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function applyProjectFilterV2(project) {
    compositionProjectV2 = project || "";
    el("flow-project-filter").value = compositionProjectV2;
    el("project-filter").value = compositionProjectV2;
    renderCompositionV2();
    readAppliedFiltersV2(); renderRowsV2();
    var panel = el("runs-panel"); if (panel && typeof panel.scrollIntoView === "function") panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function renderShareDetailV2(trackId, legendId, items) {
    var track = el(trackId), legend = el(legendId); track.replaceChildren(); legend.replaceChildren();
    var total = items.reduce(function (sum, item) { return sum + item.value; }, 0);
    if (!total) { legend.appendChild(make("span", "muted", "No proportional data.")); return; }
    items.filter(function (item) { return item.value > 0; }).forEach(function (item) {
      var segment = make("span", "stacked-segment"); segment.style.width = item.value / total * 100 + "%"; segment.style.background = item.color; segment.title = item.label + " · " + fmtPct(item.value / total * 100); track.appendChild(segment);
      var row = make("div", "share-row"); var dot = make("span", "legend-dot"); dot.style.background = item.color; row.appendChild(dot); row.appendChild(make("span", "name", item.label)); row.appendChild(make("strong", "", fmtPct(item.value / total * 100))); legend.appendChild(row);
    });
  }
  function renderAgentBarsV2(run) {
    var target = el("detail-agent-bars"); target.replaceChildren();
    var agents = agentList(run).slice().sort(function (a, b) { return tokens(b.token_usage).total_tokens - tokens(a.token_usage).total_tokens || String(a.label).localeCompare(String(b.label)); });
    var total = agents.reduce(function (sum, agent) { return sum + tokens(agent.token_usage).total_tokens; }, 0);
    if (!agents.length) { target.appendChild(make("p", "muted", "No agent records.")); return; }
    agents.forEach(function (agent) {
      var usage = tokens(agent.token_usage); var share = total ? usage.total_tokens / total * 100 : 0;
      var row = make("div", "agent-viz-row"); var label = make("div", "agent-viz-label"); label.appendChild(make("strong", "", full(agent.label))); label.appendChild(make("span", "", full(agent.model) + " · " + full(agent.role) + " · " + fmtInt(agent.tool_count) + " tools")); row.appendChild(label);
      var track = make("div", "agent-viz-track"); var fill = make("div", "agent-viz-fill"); fill.style.width = share + "%"; fill.style.background = modelColor(modelName(agent)); track.appendChild(fill); row.appendChild(track);
      var value = make("div", "agent-viz-value", fmtPct(share) + " · " + compact(usage.total_tokens)); value.title = fmtInt(usage.total_tokens) + " reported tokens"; row.appendChild(value); target.appendChild(row);
    });
  }
  function renderDetailV2(run) {
    var groups = modelGroupsV2(run);
    renderShareDetailV2("detail-model-share", "detail-model-legend", groups.map(function (group) { return { label: group.model, value: group.tokens, color: modelColor(group.model) }; }));
    var usage = aggregateUsageV2([run], ""); var parts = usagePartsV2(usage);
    renderShareDetailV2("detail-token-share", "detail-token-legend", tokenTypesV2.map(function (type) { return { label: type.label, value: parts[type.key], color: type.color }; }));
    renderAgentBarsV2(run); renderDiagnostics(run); renderTree(run.agent_tree, el("agent-tree"));
    var limitations = el("detail-limitations"); limitations.replaceChildren(); var allLimitations = list(run.analysis && run.analysis.limitations); if (!allLimitations.length) limitations.appendChild(make("li", "", "No additional limitations.")); else allLimitations.forEach(function (item) { limitations.appendChild(make("li", "", item)); });
    var quota = run.analysis && run.analysis.quota ? run.analysis.quota : {}; var cost = runCost(run);
    el("dialog-title").textContent = full(run.label); el("dialog-subtitle").textContent = projectLabelV2(run) + " · " + dayLabelV2(dayKeyV2(run)) + " · " + full(run.status) + " · proportions first";
    el("detail-cost").textContent = cost.available ? fmtMoney(cost.cost) + (cost.partial ? " · partial" : "") : "—";
    el("detail-quota-value").textContent = fmtPct(quota.current_used_percent); el("detail-token-total").textContent = compact(runTokens(run).total_tokens); el("detail-token-total").title = fmtInt(runTokens(run).total_tokens) + " reported run tokens"; el("detail-agent-count").textContent = fmtInt(agentList(run).length);
    el("detail-project").textContent = projectLabelV2(run); el("detail-project-evidence").textContent = projectEvidenceV2(run); el("detail-status").textContent = full(run.status); el("detail-severity").textContent = severity(run); el("detail-date").textContent = dayLabelV2(dayKeyV2(run)); el("detail-start").textContent = full(run.start_at); el("detail-end").textContent = full(run.end_at); el("detail-time-evidence").textContent = full(run.time_evidence); el("detail-quality").textContent = list(run.quality_flags).length ? list(run.quality_flags).join(" · ") : "None";
    el("detail-quota").textContent = ["Used " + fmtPct(quota.current_used_percent), "Observed delta " + fmtPct(quota.observed_delta_percent), windowLabel(quota.window_minutes), "Observed " + full(quota.observed_at), "Evidence " + full(quota.evidence_level), "credits excluded"].join(" · ");
  }
  function runIndexFromHashV2() { var label; try { label = decodeURIComponent(String(window.location.hash || "").replace(/^#/, "")); } catch (error) { return -1; } if (!/^run-[0-9]+$/.test(label)) return -1; return runs.findIndex(function (run) { return run.label === label; }); }
  function openRunV2(index, updateLocation) {
    if (!runs[index]) return;
    selectRow(index); renderDetailV2(runs[index]);
    if (updateLocation && window.location.hash !== "#" + runs[index].label) window.history.pushState({ run: runs[index].label }, "", "#" + encodeURIComponent(runs[index].label));
    var dialog = el("run-dialog"); if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); } else dialog.setAttribute("data-open", "true");
  }
  function closeRunV2(updateLocation) {
    var dialog = el("run-dialog"); if (typeof dialog.close === "function" && dialog.open) dialog.close(); dialog.removeAttribute("data-open"); selectRow(null);
    if (updateLocation && runIndexFromHashV2() >= 0) window.history.pushState({}, "", window.location.pathname + window.location.search);
  }
  function syncLocationV2() { var index = runIndexFromHashV2(); if (index >= 0) openRunV2(index, false); else closeRunV2(false); }
  var dashboardSectionIdsV2 = ["overview-panel", "composition-panel", "trends-panel", "projects-panel", "agents-panel", "runs-panel"];
  var sectionScrollQueuedV2 = false;
  function setActiveSectionV2(sectionId) {
    if (dashboardSectionIdsV2.indexOf(sectionId) === -1) sectionId = "overview-panel";
    Array.prototype.forEach.call(document.querySelectorAll(".sidebar-nav a"), function (link) {
      if (link.getAttribute("href") === "#" + sectionId) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
    var switcher = el("section-switcher"); if (switcher) switcher.value = "#" + sectionId;
  }
  function sectionFromHashV2() {
    var hash = String(window.location.hash || "").replace(/^#/, "");
    if (/^run-[0-9]+$/.test(hash)) return "runs-panel";
    return dashboardSectionIdsV2.indexOf(hash) === -1 ? "overview-panel" : hash;
  }
  function updateSectionFromViewportV2() {
    sectionScrollQueuedV2 = false;
    if (runIndexFromHashV2() >= 0) { setActiveSectionV2("runs-panel"); return; }
    var threshold = Math.min(190, Math.max(90, window.innerHeight * .28));
    var active = "overview-panel";
    dashboardSectionIdsV2.forEach(function (sectionId) { var section = el(sectionId); if (section && section.getBoundingClientRect().top <= threshold) active = sectionId; });
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 3) active = "runs-panel";
    setActiveSectionV2(active);
  }
  function scheduleSectionNavigationV2() {
    if (sectionScrollQueuedV2) return;
    sectionScrollQueuedV2 = true;
    window.requestAnimationFrame(updateSectionFromViewportV2);
  }
  function syncNavigationHashV2() { setActiveSectionV2(sectionFromHashV2()); syncLocationV2(); }

  renderReportCoverageV2();
  populateFiltersV2();
  var dailyV2 = dailyDataV2();
  renderMetaAndTodayV2(dailyV2);
  renderProjectOverviewV2(); renderAgentStatisticsV2(); renderCompositionV2(); renderCostQuotaIndexV2(dailyV2); renderRowsV2(); syncNavigationHashV2();
  el("flow-model-filter").addEventListener("change", function () { renderSankeyV2(el("flow-model-filter").value); });
  el("flow-project-filter").addEventListener("change", function () { compositionProjectV2 = el("flow-project-filter").value; renderCompositionV2(); });
  ["project-filter", "model-filter", "status-filter", "severity-filter"].forEach(function (id) { el(id).addEventListener("change", function () { readAppliedFiltersV2(); renderRowsV2(); }); });
  el("keyword-filter").addEventListener("input", function () { readAppliedFiltersV2(); renderRowsV2(); });
  el("reset-run-filters").addEventListener("click", function () { ["project-filter", "model-filter", "status-filter", "severity-filter", "keyword-filter"].forEach(function (id) { el(id).value = ""; }); activeDayFilterV2 = ""; readAppliedFiltersV2(); renderRowsV2(); });
  el("clear-day-filter").addEventListener("click", function () { activeDayFilterV2 = ""; renderRowsV2(); });
  el("cost-quota-detail").addEventListener("click", function () { costQuotaRangeV2 = "detail"; renderCostQuotaIndexV2(dailyV2); });
  el("cost-quota-full-range").addEventListener("click", function () { costQuotaRangeV2 = "full"; renderCostQuotaIndexV2(dailyV2); });
  Array.prototype.forEach.call(document.querySelectorAll(".sort-button"), function (button) { button.addEventListener("click", function () { var key = button.dataset.sort; if (sortStateV2.key === key) sortStateV2.direction = sortStateV2.direction === "asc" ? "desc" : "asc"; else { sortStateV2.key = key; sortStateV2.direction = "desc"; } renderRowsV2(); }); });
  el("dialog-close").addEventListener("click", function () { closeRunV2(true); });
  el("run-dialog").addEventListener("cancel", function (event) { event.preventDefault(); closeRunV2(true); });
  el("run-dialog").addEventListener("click", function (event) { if (event.target === el("run-dialog")) closeRunV2(true); });
  el("section-switcher").addEventListener("change", function () { var target = el("section-switcher").value; if (target) window.location.hash = target; });
  Array.prototype.forEach.call(document.querySelectorAll(".sidebar-nav a"), function (link) { link.addEventListener("click", function () { setActiveSectionV2(String(link.getAttribute("href") || "").replace(/^#/, "")); }); });
  window.addEventListener("scroll", scheduleSectionNavigationV2, { passive: true });
  window.addEventListener("resize", scheduleSectionNavigationV2);
  window.addEventListener("hashchange", syncNavigationHashV2);
  window.addEventListener("popstate", syncNavigationHashV2);
  scheduleSectionNavigationV2();
}());
</script>
</body>
</html>
'''

__all__ = ["default_dashboard_output", "open_dashboard", "render_dashboard"]
