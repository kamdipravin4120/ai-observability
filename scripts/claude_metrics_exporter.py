#!/usr/bin/env python3
"""
Prometheus exporter for Claude Code session metrics.
Reads ~/.claude/projects/*/*.jsonl and exposes token usage,
cost estimates, session counts, and memory file counts.

Run: python3 scripts/claude_metrics_exporter.py
Metrics on: http://localhost:8001/metrics
"""
import json
import os
import glob
import time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
PORT = 8001

# Anthropic pricing (per 1M tokens, USD) — Sonnet 4.6 rates
PRICING = {
    "input":          3.00,
    "cache_creation": 3.75,
    "cache_read":     0.30,
    "output":        15.00,
}


def clean_project_name(raw: str) -> str:
    name = raw
    for prefix in ["-home-pk-projects-", "-home-pk-Work-projects-",
                   "-home-pk-Work-", "-home-pk--", "-home-pk-"]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name or raw


def parse_sessions() -> list[dict]:
    sessions = []
    for jsonl_path in sorted(glob.glob(f"{CLAUDE_PROJECTS}/*/*.jsonl")):
        proj_raw  = os.path.basename(os.path.dirname(jsonl_path))
        proj      = clean_project_name(proj_raw)
        session_id = os.path.splitext(os.path.basename(jsonl_path))[0]
        short_id   = session_id[:8]

        title = ""
        model = "unknown"
        input_tok = cache_create = cache_read = output_tok = 0
        user_msgs = assistant_msgs = 0
        first_ts = last_ts = None

        try:
            with open(jsonl_path) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    d = json.loads(raw)
                    t = d.get("type", "")

                    ts = d.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                    if t == "ai-title" and not title:
                        title = d.get("aiTitle", "")[:60]

                    if t == "user":
                        user_msgs += 1

                    if t == "assistant":
                        assistant_msgs += 1
                        msg   = d.get("message", {})
                        usage = msg.get("usage", {})
                        if not model or model == "unknown":
                            model = msg.get("model", "unknown")
                        input_tok    += usage.get("input_tokens", 0)
                        cache_create += usage.get("cache_creation_input_tokens", 0)
                        cache_read   += usage.get("cache_read_input_tokens", 0)
                        output_tok   += usage.get("output_tokens", 0)
        except Exception:
            continue

        cost = (
            input_tok    / 1_000_000 * PRICING["input"] +
            cache_create / 1_000_000 * PRICING["cache_creation"] +
            cache_read   / 1_000_000 * PRICING["cache_read"] +
            output_tok   / 1_000_000 * PRICING["output"]
        )

        last_ts_unix = 0
        if last_ts:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                last_ts_unix = int(dt.timestamp())
            except Exception:
                pass

        memory_files = len(glob.glob(
            f"{CLAUDE_PROJECTS}/{proj_raw}/memory/*.md"
        ))

        sessions.append({
            "project":       proj,
            "session_id":    short_id,
            "title":         re.sub(r'["\\\n\r]', ' ', title) or "untitled",
            "model":         model,
            "input_tokens":  input_tok,
            "cache_create":  cache_create,
            "cache_read":    cache_read,
            "output_tokens": output_tok,
            "user_msgs":     user_msgs,
            "assistant_msgs": assistant_msgs,
            "cost_usd":      round(cost, 6),
            "last_active":   last_ts_unix,
            "memory_files":  memory_files,
        })

    return sessions


def render_metrics(sessions: list[dict]) -> str:
    lines = []

    def metric(name, help_text, mtype):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    # Per-session metrics
    metric("claude_session_input_tokens",
           "Total input tokens for a Claude Code session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",title="{s["title"]}",model="{s["model"]}"'
        lines.append(f'claude_session_input_tokens{{{lbl}}} {s["input_tokens"]}')

    metric("claude_session_cache_creation_tokens",
           "Tokens written to prompt cache in this session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",model="{s["model"]}"'
        lines.append(f'claude_session_cache_creation_tokens{{{lbl}}} {s["cache_create"]}')

    metric("claude_session_cache_read_tokens",
           "Tokens read from prompt cache in this session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",model="{s["model"]}"'
        lines.append(f'claude_session_cache_read_tokens{{{lbl}}} {s["cache_read"]}')

    metric("claude_session_output_tokens",
           "Total output tokens for a Claude Code session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",model="{s["model"]}"'
        lines.append(f'claude_session_output_tokens{{{lbl}}} {s["output_tokens"]}')

    metric("claude_session_cost_usd",
           "Estimated USD cost for a Claude Code session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",model="{s["model"]}"'
        lines.append(f'claude_session_cost_usd{{{lbl}}} {s["cost_usd"]}')

    metric("claude_session_messages_total",
           "Total user+assistant message turns in a session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",role="user"'
        lines.append(f'claude_session_messages_total{{{lbl}}} {s["user_msgs"]}')
        lbl = f'project="{s["project"]}",session="{s["session_id"]}",role="assistant"'
        lines.append(f'claude_session_messages_total{{{lbl}}} {s["assistant_msgs"]}')

    metric("claude_session_last_active_timestamp",
           "Unix timestamp of last activity in this session", "gauge")
    for s in sessions:
        lbl = f'project="{s["project"]}",session="{s["session_id"]}"'
        lines.append(f'claude_session_last_active_timestamp{{{lbl}}} {s["last_active"]}')

    # Per-project aggregates
    projects: dict[str, dict] = {}
    for s in sessions:
        p = s["project"]
        if p not in projects:
            projects[p] = {
                "sessions": 0, "input": 0, "cache_create": 0,
                "cache_read": 0, "output": 0, "cost": 0.0,
                "memory_files": s["memory_files"],
            }
        projects[p]["sessions"]      += 1
        projects[p]["input"]         += s["input_tokens"]
        projects[p]["cache_create"]  += s["cache_create"]
        projects[p]["cache_read"]    += s["cache_read"]
        projects[p]["output"]        += s["output_tokens"]
        projects[p]["cost"]          += s["cost_usd"]

    metric("claude_project_sessions_total",
           "Total number of Claude Code sessions per project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_sessions_total{{project="{p}"}} {v["sessions"]}')

    metric("claude_project_input_tokens_total",
           "Total input tokens across all sessions for a project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_input_tokens_total{{project="{p}"}} {v["input"]}')

    metric("claude_project_output_tokens_total",
           "Total output tokens across all sessions for a project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_output_tokens_total{{project="{p}"}} {v["output"]}')

    metric("claude_project_cache_read_tokens_total",
           "Total cache-read tokens across all sessions for a project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_cache_read_tokens_total{{project="{p}"}} {v["cache_read"]}')

    metric("claude_project_cache_creation_tokens_total",
           "Total cache-creation tokens across all sessions for a project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_cache_creation_tokens_total{{project="{p}"}} {v["cache_create"]}')

    metric("claude_project_cost_usd_total",
           "Estimated total USD cost for a project across all sessions", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_cost_usd_total{{project="{p}"}} {round(v["cost"], 6)}')

    metric("claude_project_memory_files",
           "Number of memory files saved for a project", "gauge")
    for p, v in projects.items():
        lines.append(f'claude_project_memory_files{{project="{p}"}} {v["memory_files"]}')

    # Totals
    all_input  = sum(s["input_tokens"]  for s in sessions)
    all_output = sum(s["output_tokens"] for s in sessions)
    all_cost   = sum(s["cost_usd"]      for s in sessions)
    all_cache_r = sum(s["cache_read"]   for s in sessions)
    all_cache_c = sum(s["cache_create"] for s in sessions)

    lines += [
        "# HELP claude_total_input_tokens_all Grand total input tokens across all projects",
        "# TYPE claude_total_input_tokens_all gauge",
        f"claude_total_input_tokens_all {all_input}",
        "# HELP claude_total_output_tokens_all Grand total output tokens across all projects",
        "# TYPE claude_total_output_tokens_all gauge",
        f"claude_total_output_tokens_all {all_output}",
        "# HELP claude_total_cost_usd_all Estimated grand total cost across all projects",
        "# TYPE claude_total_cost_usd_all gauge",
        f"claude_total_cost_usd_all {round(all_cost, 4)}",
        "# HELP claude_total_cache_read_tokens_all Grand total cache-read tokens",
        "# TYPE claude_total_cache_read_tokens_all gauge",
        f"claude_total_cache_read_tokens_all {all_cache_r}",
        "# HELP claude_total_cache_creation_tokens_all Grand total cache-creation tokens",
        "# TYPE claude_total_cache_creation_tokens_all gauge",
        f"claude_total_cache_creation_tokens_all {all_cache_c}",
        "# HELP claude_total_sessions_all Total sessions across all projects",
        "# TYPE claude_total_sessions_all gauge",
        f"claude_total_sessions_all {len(sessions)}",
        "# HELP claude_total_projects_all Total number of distinct projects",
        "# TYPE claude_total_projects_all gauge",
        f"claude_total_projects_all {len(projects)}",
    ]

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            sessions = parse_sessions()
            body = render_metrics(sessions).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Claude session metrics → http://localhost:{PORT}/metrics")
    server.serve_forever()
