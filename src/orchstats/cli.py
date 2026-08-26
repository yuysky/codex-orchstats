"""Command-line interface for local, privacy-safe orchestration reports."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from . import __version__
from .analysis import RunAnalysis, analyze_run
from .dashboard import default_dashboard_output, open_dashboard, render_dashboard
from .demo import demo_sessions_root
from .history import DASHBOARD_WINDOWS, build_dashboard_history
from .parser import load_run, parse_session
from .reporting import (
    dumps_json,
    render_analysis,
    render_lint,
    render_usage,
    render_watch,
)
from .sanitize import SanitizeError, write_sanitized
from .usage import DEFAULT_SESSIONS_ROOT, SUPPORTED_WINDOWS, build_usage_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchstats",
        description="Estimate Codex orchestration usage from permitted local evidence.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
        help="show the installed orchstats version and exit",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="summarize one local orchestration run")
    _add_root_options(analyze)

    lint = commands.add_parser("lint", help="show diagnostics for one local run")
    _add_root_options(lint)
    lint.add_argument(
        "--fail-on",
        choices=("high",),
        default=None,
        help="return exit code 2 when a HIGH diagnostic is present",
    )

    usage = commands.add_parser("usage", help="aggregate recent local session usage")
    usage.add_argument("--sessions-root", type=Path, default=None)
    usage.add_argument("--since", choices=SUPPORTED_WINDOWS, default="7d")
    usage.add_argument("--format", choices=("text", "json", "markdown"), default="text")

    watch = commands.add_parser("watch", help="watch one local session snapshot")
    watch.add_argument("path", nargs="?", type=Path)
    watch.add_argument("--sessions-root", type=Path, default=None)
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--once", action="store_true")

    sanitize = commands.add_parser("sanitize", help="write a public-summary JSON export")
    _add_root_options(sanitize, include_format=False, include_output=False)
    sanitize.add_argument("--output", type=Path, required=True)

    dashboard = commands.add_parser("dashboard", help="generate a local orchestration dashboard")
    dashboard_sources = dashboard.add_mutually_exclusive_group()
    dashboard_sources.add_argument("--sessions-root", type=Path, default=None)
    dashboard_sources.add_argument(
        "--demo",
        action="store_true",
        help="use the packaged fully synthetic sessions and include full history",
    )
    dashboard.add_argument("--since", choices=DASHBOARD_WINDOWS, default="7d")
    dashboard.add_argument("--output", type=Path, default=None)
    dashboard.add_argument("--no-open", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a command and return its process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return _command_analyze(args)
        if args.command == "lint":
            return _command_lint(args)
        if args.command == "usage":
            return _command_usage(args)
        if args.command == "watch":
            return _command_watch(args)
        if args.command == "sanitize":
            return _command_sanitize(args)
        if args.command == "dashboard":
            return _command_dashboard(args)
        parser.error("unknown command")
    except KeyboardInterrupt:
        # This is intentionally a successful stop for watch, and is harmless
        # for an interrupted one-shot command as well.
        return 0
    except SanitizeError as exc:
        _print_error(str(exc))
        return 1
    except (OSError, TypeError, ValueError, UnicodeError):
        # Do not echo exception strings: they can contain local paths or raw
        # parser details.  The command contract exposes only an exit status.
        _print_error("local report could not be generated")
        return 1


def _add_root_options(
    command: argparse.ArgumentParser,
    *,
    include_format: bool = True,
    include_output: bool = True,
) -> None:
    command.add_argument("root", type=Path)
    command.add_argument("--sessions-root", type=Path, default=None)
    command.add_argument("--no-discover", action="store_true")
    if include_format:
        command.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    if include_output:
        command.add_argument("--output", type=Path, default=None)


def _load_analysis(args: Any) -> RunAnalysis:
    run = load_run(
        args.root,
        sessions_root=args.sessions_root,
        discover=not bool(args.no_discover),
    )
    return analyze_run(run)


def _command_analyze(args: Any) -> int:
    analysis = _load_analysis(args)
    rendered = render_analysis(analysis, args.format)
    _emit(rendered, args.output)
    return 0


def _command_lint(args: Any) -> int:
    analysis = _load_analysis(args)
    rendered = render_lint(analysis, args.format)
    _emit(rendered, args.output)
    if args.fail_on == "high" and any(
        str(item.severity).upper() == "HIGH" for item in analysis.diagnostics
    ):
        return 2
    return 0


def _command_usage(args: Any) -> int:
    report = build_usage_report(
        args.sessions_root if args.sessions_root is not None else DEFAULT_SESSIONS_ROOT,
        since=args.since,
    )
    _emit(render_usage(report, args.format), None)
    return 0


def _command_watch(args: Any) -> int:
    if args.interval < 0:
        raise ValueError("interval must not be negative")
    explicit = args.path is not None
    if explicit and args.path.is_file():
        source_file = args.path
        discovery_root = source_file.parent
    elif explicit and args.path.exists() and not args.path.is_dir():
        raise ValueError("watch path is not a file or directory")
    else:
        candidate_root = args.path if explicit else (
            args.sessions_root if args.sessions_root is not None else DEFAULT_SESSIONS_ROOT
        )
        if candidate_root.exists() and not candidate_root.is_dir():
            raise ValueError("watch path is not a directory")
        source_file = _latest_jsonl(candidate_root)
        if source_file is None:
            _emit(render_watch(None), None)
            return 0
        # Restrict descendant discovery to this dated directory.  The full
        # sessions tree is inspected only once to choose the current source.
        discovery_root = source_file.parent
        # A newest child is a common watcher target.  Use only the safe
        # parser projection to walk its parent_thread_id chain back to the
        # highest available ancestor before loading the run tree.
        source_file = _watch_ancestor(source_file, discovery_root)

    try:
        while True:
            run = load_run(source_file, sessions_root=discovery_root)
            _emit(render_watch(analyze_run(run)), None)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _command_sanitize(args: Any) -> int:
    run = load_run(
        args.root,
        sessions_root=args.sessions_root,
        discover=not bool(args.no_discover),
    )
    analysis = analyze_run(run)
    write_sanitized(analysis, args.output, input_root=args.root)
    return 0


def _command_dashboard(args: Any) -> int:
    if bool(getattr(args, "demo", False)):
        # Demo records are materialized only for the duration of the local
        # history/render pass.  ``all`` is intentional: synthetic timestamps
        # are fixed and should not disappear behind the normal rolling window.
        with demo_sessions_root() as sessions_root:
            return _write_dashboard(sessions_root, "all", args)

    explicit_root = getattr(args, "sessions_root", None)
    sessions_root = explicit_root if explicit_root is not None else DEFAULT_SESSIONS_ROOT
    if not sessions_root.exists() or not sessions_root.is_dir():
        if explicit_root is None:
            _print_error(
                "default sessions root is unavailable; use --demo or --sessions-root to choose a sessions directory"
            )
        else:
            _print_error("sessions root is not a directory")
        return 1

    return _write_dashboard(sessions_root, args.since, args)


def _write_dashboard(sessions_root: Path, since: str, args: Any) -> int:
    payload = build_dashboard_history(sessions_root, since=since)
    target = args.output if args.output is not None else default_dashboard_output()
    _emit(render_dashboard(payload), target)
    if not args.no_open and not open_dashboard(target):
        sys.stderr.write("warning: dashboard was generated but the browser could not be opened\n")
    sys.stdout.write("Dashboard: %s\n" % Path(target).resolve())
    return 0


def _latest_jsonl(root: Path) -> Optional[Path]:
    if not root.exists() or not root.is_dir():
        return None
    candidates: List[Tuple[float, str, Path]] = []
    try:
        for path in root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((modified, str(path), path))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _watch_ancestor(source_file: Path, discovery_root: Path) -> Path:
    """Return the highest available same-directory ancestor of a session."""

    metadata = {}
    try:
        candidates = sorted(
            path for path in discovery_root.rglob("*.jsonl") if path.is_file()
        )
    except OSError:
        candidates = []
    for candidate in candidates:
        try:
            trace = parse_session(candidate)
        except (OSError, TypeError, ValueError, UnicodeError):
            continue
        session_id = getattr(trace, "session_id", None)
        if session_id:
            metadata.setdefault(
                str(session_id),
                (candidate, getattr(trace, "parent_thread_id", None)),
            )

    try:
        latest_trace = parse_session(source_file)
    except (OSError, TypeError, ValueError, UnicodeError):
        return source_file
    current_id = getattr(latest_trace, "session_id", None)
    visited = set()
    while current_id and str(current_id) not in visited:
        visited.add(str(current_id))
        entry = metadata.get(str(current_id))
        if entry is None:
            return source_file
        candidate, parent_id = entry
        parent_entry = metadata.get(str(parent_id)) if parent_id else None
        if parent_entry is None:
            return candidate
        current_id = parent_id
    return source_file


def _emit(text: str, output: Optional[Path]) -> None:
    if output is None:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    target = Path(output)
    if target.exists():
        raise SanitizeError("output target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise SanitizeError("output target already exists")


def _print_error(message: str) -> None:
    sys.stderr.write("error: %s\n" % message)


__all__ = ["build_parser", "main"]
