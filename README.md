# codex-orchstats

Local, privacy-first analytics for Codex orchestration. See how tokens are distributed across projects, agents, models, and token types; inspect observed account-level quota movement; estimate API-equivalent reference cost; and flag deterministic orchestration waste without sending your data anywhere.

![Synthetic codex-orchstats dashboard](assets/dashboard-preview.png)

## What it shows

- Token composition across uncached input, cached input, cache write, output, and reasoning.
- Model, project, and Agent-role allocation for local Codex runs.
- Daily composition and trends from safe event timestamps rather than filesystem modification time.
- The latest observed account-level quota state, kept separate from projects, models, agents, and price estimates.
- Deterministic diagnostics for routing, forks, duplicated work, validation, overlap, and review chains.
- Source-scan coverage so missing, invalid, duplicate, orphaned, cyclic, or unreadable sessions are visible instead of silently ignored.

## Dashboard preview

API-equivalent cost per observed quota point over time. This is a heuristic cross-signal index, not actual billing or the price of plan quota:

![Synthetic API-equivalent cost and observed quota trend](assets/dashboard-cost-quota-trend.png)

Token and model composition:

![Synthetic token composition dashboard](assets/dashboard-composition.png)

Project allocation:

![Synthetic project allocation dashboard](assets/dashboard-allocation.png)

Agent-role statistics:

![Synthetic Agent statistics](assets/dashboard-agent-statistics.png)

Filterable run table:

![Synthetic run table](assets/dashboard-runs.png)

## Quick start

Requires Python 3.9+ on macOS or Linux.

```sh
git clone https://github.com/yuysky/codex-orchstats.git
cd codex-orchstats
pipx install .
orchstats --version
```

If `pipx` is unavailable, use a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

Open the bundled synthetic demo before reading any local Codex data:

```sh
orchstats dashboard --demo
```

Then inspect the default local Codex history:

```sh
orchstats dashboard
```

The dashboard is one self-contained local HTML file. It does not start a server, upload sessions, use telemetry, call an AI service, or load remote assets.

## Commands

### Dashboard

Build and open the private local dashboard:

```sh
orchstats dashboard
orchstats dashboard --since 30d
orchstats dashboard --sessions-root <sessions-directory> --since all
orchstats dashboard --demo --output reports/demo.html --no-open
```

`--demo` and `--sessions-root` are mutually exclusive. Demo mode always uses the full bundled synthetic history. Existing output files are never overwritten.

### Analyze one orchestration run

```sh
orchstats analyze <root.jsonl>
orchstats analyze <root.jsonl> --format json --output reports/run.json
orchstats analyze <root.jsonl> --no-discover
```

By default, descendant sessions are discovered through explicit Codex parent metadata.

### Summarize usage

```sh
orchstats usage --since 24h
orchstats usage --since 7d --format markdown
orchstats usage --sessions-root <sessions-directory> --format json
```

Usage reports token totals and the latest observed account-level quota snapshot without converting subscription quota into dollars.

### Watch local activity

```sh
orchstats watch
orchstats watch <root.jsonl> --once
orchstats watch <sessions-directory> --interval 2
```

Press Ctrl-C to stop continuous watch mode.

### Run deterministic diagnostics

```sh
orchstats lint <root.jsonl>
orchstats lint <root.jsonl> --format json
orchstats lint <root.jsonl> --fail-on high
```

`--fail-on high` returns exit code `2` when a HIGH diagnostic is present.

### Create a shareable projection

```sh
orchstats sanitize <root.jsonl> --output public-summary.json
orchstats sanitize <root.jsonl> --sessions-root <sessions-directory> --output public-summary.json
```

`sanitize` writes the deliberately smaller `public-summary-v1` JSON projection. Unknown free text is discarded or mapped to a generic category.

## Output and privacy boundaries

| Output | Intended use | Shareable? |
|---|---|---|
| Dashboard HTML | Local multi-run exploration | No |
| Analyze, usage, watch, and lint output | Local inspection and debugging | No |
| `public-summary-v1` JSON | Minimal aggregate evidence | Yes, after reviewing it |
| Raw Codex JSONL | Local source evidence | Never |

Raw sessions stay on your computer. The parser removes POSIX, home-relative, Windows drive, UNC, and `file://` absolute paths at its data boundary. Dashboard payloads omit raw session IDs, commands, hashes, message content, credentials, and balances.

Dashboard files are still private because they may contain final project directory names, safe timestamps, model metadata, diagnostics, API-equivalent estimates, and account-level quota observations. Do not upload or share them.

The sanitizer also excludes project names, quota, dates, identifiers, paths, commands, hashes, source content, credentials, and balances. Review the generated JSON before sharing it.

## Accuracy and limitations

API-equivalent cost is a heuristic comparison based on bundled public model rates. It is not a Codex bill, a subscription price, or a dollar value for plan quota. Models without an exact known rate remain unpriced, and reasoning is not charged twice because it is already included within output tokens.

Shared quota is account-level and is never attributed to a project, model, or agent. The tool cannot establish actual Codex billing, exact semantic duplication, per-agent quota consumption, or a dollar value for subscription quota.

## Exit codes and common problems

| Code | Meaning |
|---|---|
| `0` | Command completed successfully |
| `1` | Local input or output was invalid or unavailable |
| `2` | Argument parsing failed, or `lint --fail-on high` found a HIGH diagnostic |

- If the default sessions directory is missing, run `orchstats dashboard --demo`, then pass the correct directory with `--sessions-root`.
- If the HTML was generated but the browser did not open, open the printed path manually or use `--no-open`.
- If an output already exists, choose a new path; the CLI does not overwrite reports or sanitized exports.
- If a model has no exact price, it stays visibly unpriced rather than receiving a fabricated estimate.

## Minimal repository by design

This public repository intentionally contains only what is required to understand, install, and run the tool: this README, the MIT license, package metadata, runtime source, packaged schemas and synthetic demo, and the dashboard images used above.

Agent instructions, changelogs, goals, tests, policies, internal documentation, GitHub workflows, release scripts, raw sessions, generated reports, build artifacts, and personal working material are not required by users at runtime and are intentionally not uploaded.

## Run without installing

```sh
PYTHONPATH=src python3 -m orchstats --help
PYTHONPATH=src python3 -m orchstats dashboard --demo
```

## License

MIT
