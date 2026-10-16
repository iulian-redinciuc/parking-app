"""Memory guard for the agent loop.

The dev Pi freezes when memory runs out, and its hardware watchdog then reboots it (2026-10-15).
This checks MemAvailable every second; when it falls below the limit it kills the biggest process
started by the agent loop (a descendant of run.sh), so a runaway job dies instead of the machine.
Never touches anything else: not Claude itself, not Docker containers, not other users' processes.

    python3 -I memguard.py <state dir>       # started by start.sh, one instance (pid file)
"""

import os
import signal
import sys
import time

MIN_AVAILABLE_MB = int(os.environ.get("MEMGUARD_MIN_MB", "700"))
ROOT = os.environ.get("MEMGUARD_ROOT", "agent-loop/run.sh")  # override only for tests
SPARE = ("claude", "timeout", "tmux", "run.sh", "memguard", "sshd")


def available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 1 << 30


def read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def processes() -> dict[int, tuple[int, int, str]]:
    """pid -> (parent pid, rss MB, command line)."""
    out = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        stat = read(f"/proc/{name}/stat")
        if not stat:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        rss_mb = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
        cmd = read(f"/proc/{name}/cmdline").replace("\0", " ").strip()
        out[int(name)] = (int(fields[1]), rss_mb, cmd)
    return out


def loop_descendants(procs: dict[int, tuple[int, int, str]]) -> list[int]:
    roots = [pid for pid, (_, _, cmd) in procs.items() if ROOT in cmd and "memguard" not in cmd]
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    found, todo = [], list(roots)
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def killable(pid: int, cmd: str) -> bool:
    if os.stat(f"/proc/{pid}").st_uid != os.getuid():
        return False
    if "docker" in read(f"/proc/{pid}/cgroup") or "containerd" in cmd:
        return False
    first = os.path.basename(cmd.split(" ", 1)[0]) if cmd else ""
    return not any(s in first or (s == "run.sh" and s in cmd) for s in SPARE)


def log(state: str, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] memguard: {msg}\n"
    for name in ("loop.log", "memguard.log"):
        with open(os.path.join(state, name), "a") as f:
            f.write(line)


def main() -> None:
    state = sys.argv[1]
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "memguard.pid"), "w") as f:
        f.write(str(os.getpid()))
    log(state, f"started (kills the loop's biggest process below {MIN_AVAILABLE_MB} MB available)")
    while True:
        time.sleep(1)
        avail = available_mb()
        if avail >= MIN_AVAILABLE_MB:
            continue
        try:
            procs = processes()
            pids = [p for p in loop_descendants(procs) if p in procs and killable(p, procs[p][2])]
        except OSError:
            continue
        if not pids:
            continue
        victim = max(pids, key=lambda p: procs[p][1])
        try:
            os.kill(victim, signal.SIGKILL)
        except OSError:
            continue
        log(state, f"only {avail} MB available: killed pid {victim} ({procs[victim][1]} MB): {procs[victim][2][:160]}")
        time.sleep(3)


if __name__ == "__main__":
    main()
