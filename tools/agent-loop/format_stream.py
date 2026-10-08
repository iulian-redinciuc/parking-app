"""Turn `claude -p --output-format stream-json` output into a readable live log.

Reads stdin line by line, prints one human-readable line per event, and writes
a shell-sourceable summary of the session (errors, usage-limit reset time) to
the file given as the first argument.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime

LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your (usage )?limit|out of (extra )?usage|"
    r"credit balance is too low|weekly limit|session limit",
    re.I,
)
TRANSIENT_RE = re.compile(
    r"overloaded|rate.?limit|\b(429|500|502|503|504|529)\b|timed? ?out|ECONNRESET|"
    r"ENOTFOUND|EAI_AGAIN|socket hang up|network error|API Error",
    re.I,
)
AUTH_RE = re.compile(
    r"invalid api key|please run /login|not logged in|authentication_error|"
    r"oauth token has expired|login required",
    re.I,
)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def out(marker: str, text: str, limit: int = 300) -> None:
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    print(f"[{ts()}] {marker} {text}", flush=True)


def parse_reset(text: str) -> int | None:
    """Best-effort: find when a usage limit resets, as a unix timestamp."""
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"resets? in (\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return int(time.time()) + n * (3600 if unit.startswith("h") else 60)
    m = re.search(r"resets? (?:at |on )?([^\n|·.]+)", text, re.I)
    if not m:
        return None
    when = m.group(1).replace(",", " ").strip()
    env = dict(os.environ)
    tz = re.search(r"\(([A-Za-z_]+/[A-Za-z_/]+)\)", when)
    if tz:
        env["TZ"] = tz.group(1)
        when = when.replace(tz.group(0), "").strip()
    try:
        epoch = int(
            subprocess.run(
                ["date", "-d", when, "+%s"], env=env, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    except (subprocess.CalledProcessError, ValueError):
        return None
    if epoch < time.time():  # a time without a date that already passed today
        epoch += 86400
    return epoch


def describe_tool(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"$ {inp.get('command', '').splitlines()[0] if inp.get('command') else ''}"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"{name} {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}"
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        active = [t.get("content", "") for t in todos if t.get("status") == "in_progress"]
        done = sum(1 for t in todos if t.get("status") == "completed")
        return f"plan {done}/{len(todos)} done" + (f" · now: {active[0]}" if active else "")
    if name == "Agent":
        return f"subagent: {inp.get('description', '')}"
    return name


def main() -> None:
    summary_path = sys.argv[1]
    session_id, started, is_error, result_text = "", False, False, ""
    problems: list[str] = []
    turns, cost = "", ""

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            out("!", line)
            problems.append(line)
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            session_id, started = ev.get("session_id", ""), True
            out("==", f"session {session_id} started (model {ev.get('model', '?')})")
        elif kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    out("»", block["text"])
                elif block.get("type") == "tool_use":
                    out("·", describe_tool(block.get("name", ""), block.get("input", {})), 200)
        elif kind == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                    out("!", f"tool error: {body}", 200)
        elif kind == "result":
            is_error = bool(ev.get("is_error")) or ev.get("subtype") != "success"
            result_text = str(ev.get("result") or ev.get("subtype") or "")
            turns, cost = ev.get("num_turns", ""), ev.get("total_cost_usd", "")
            status = "ERROR" if is_error else "finished"
            out("==", f"session {status} · turns {turns} · {ev.get('duration_ms', 0) // 60000} min")
            if result_text:
                print(f"\n{result_text}\n", flush=True)
            if is_error:
                problems.append(result_text)

    problem_text = "\n".join(problems)
    limit_hit = bool(problems) and bool(LIMIT_RE.search(problem_text))
    reset = parse_reset(problem_text) if limit_hit else None
    fields = {
        "SESSION_ID": session_id,
        "SESSION_STARTED": int(started),
        "IS_ERROR": int(is_error or bool(problems and not result_text)),
        "LIMIT_HIT": int(limit_hit),
        "RESET_EPOCH": reset or "",
        "TRANSIENT": int(not limit_hit and bool(TRANSIENT_RE.search(problem_text))),
        "AUTH_ERROR": int(bool(AUTH_RE.search(problem_text))),
        "TURNS": turns,
        "COST_USD": cost,
    }
    with open(summary_path, "w") as f:
        for k, v in fields.items():
            f.write(f"{k}={shlex.quote(str(v))}\n")


if __name__ == "__main__":
    main()
