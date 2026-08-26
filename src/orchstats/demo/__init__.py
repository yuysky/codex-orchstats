"""Packaged, fully synthetic session records for the local dashboard demo.

The demo is kept in Python source so it remains available in an installed
wheel without adding runtime package-data configuration.  ``demo_sessions_root``
materializes the records into a short-lived directory; callers must consume
the directory inside the context manager.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Iterator, List, Mapping, Any


_SYNTHETIC_FILES: Mapping[str, List[Dict[str, Any]]] = {
    "demo-alpha-root.jsonl": [
        {
            "type": "session_meta",
            "payload": {
                "id": "demo-alpha-root",
                "parent_thread_id": None,
                "role": "root",
                "depth": 0,
                "cwd": "synthetic/workspace/demo-alpha",
            },
        },
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "medium"},
        },
        {
            "type": "response_item",
            "timestamp": "2030-01-01T09:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "demo-alpha-spawn",
                "arguments": '{"agent_type":"worker","fork_turns":"none","task_name":"synthetic-worker","agent_path":"agents/synthetic-worker"}',
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-01T09:00:02Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 200,
                        "cache_write_input_tokens": 80,
                        "output_tokens": 500,
                        "reasoning_output_tokens": 100,
                        "total_tokens": 1700,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 10,
                        "window_minutes": 300,
                        "resets_at": "2030-01-01T14:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-01T09:00:03Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1800,
                        "cached_input_tokens": 300,
                        "cache_write_input_tokens": 120,
                        "output_tokens": 700,
                        "reasoning_output_tokens": 140,
                        "total_tokens": 2500,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "window_minutes": 300,
                        "resets_at": "2030-01-01T14:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
        {
            "type": "response_item",
            "timestamp": "2030-01-01T09:00:04Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "demo-alpha-spawn",
            },
        },
    ],
    "demo-alpha-child.jsonl": [
        {
            "type": "session_meta",
            "payload": {
                "id": "demo-alpha-child",
                "parent_thread_id": "demo-alpha-root",
                "role": "worker",
                "depth": 1,
                "cwd": "synthetic/workspace/demo-alpha",
            },
        },
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra", "effort": "low"},
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-01T09:00:05Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 700,
                        "cached_input_tokens": 100,
                        "cache_write_input_tokens": 40,
                        "output_tokens": 220,
                        "reasoning_output_tokens": 40,
                        "total_tokens": 920,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "window_minutes": 300,
                        "resets_at": "2030-01-01T14:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-01T09:00:06Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 900,
                        "cached_input_tokens": 160,
                        "cache_write_input_tokens": 60,
                        "output_tokens": 300,
                        "reasoning_output_tokens": 55,
                        "total_tokens": 1200,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 14,
                        "window_minutes": 300,
                        "resets_at": "2030-01-01T14:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
    ],
    "demo-beta-root.jsonl": [
        {
            "type": "session_meta",
            "payload": {
                "id": "demo-beta-root",
                "parent_thread_id": None,
                "role": "root",
                "depth": 0,
                "cwd": "synthetic/workspace/demo-beta",
            },
        },
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-luna", "effort": "high"},
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-03T15:00:01Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 250,
                        "cache_write_input_tokens": 50,
                        "output_tokens": 400,
                        "reasoning_output_tokens": 90,
                        "total_tokens": 1400,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 20,
                        "window_minutes": 300,
                        "resets_at": "2030-01-03T20:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2030-01-03T15:00:02Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1500,
                        "cached_input_tokens": 350,
                        "cache_write_input_tokens": 90,
                        "output_tokens": 600,
                        "reasoning_output_tokens": 130,
                        "total_tokens": 2100,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 23,
                        "window_minutes": 300,
                        "resets_at": "2030-01-03T20:00:00Z",
                        "plan_type": "synthetic",
                        "has_credits": False,
                    }
                },
            },
        },
    ],
}


@contextmanager
def demo_sessions_root() -> Iterator[Path]:
    """Yield a temporary directory containing the packaged demo sessions."""

    with TemporaryDirectory(prefix="orchstats-demo-") as temporary:
        root = Path(temporary)
        for filename, records in _SYNTHETIC_FILES.items():
            content = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
            (root / filename).write_text(content, encoding="utf-8")
        yield root


__all__ = ["demo_sessions_root"]
