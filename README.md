# codex-orchstats

A local HTML dashboard for viewing Codex orchestration usage, quota changes, and API-equivalent cost estimates.

## Install

Requires Python 3.9+ on macOS or Linux.

```sh
git clone https://github.com/yuysky/codex-orchstats.git
cd codex-orchstats
pipx install .
```

## Run

Try the built-in demo:

```sh
orchstats dashboard --demo
```

Generate a dashboard from your local Codex history:

```sh
orchstats dashboard
```

## Find the HTML

The dashboard opens automatically in your browser. The command also prints the full HTML path:

```text
Dashboard: /path/to/orchstats-dashboard.html
```

To save it somewhere specific:

```sh
orchstats dashboard --output ~/Desktop/orchstats.html --no-open
```

Then open `~/Desktop/orchstats.html` in any browser.
