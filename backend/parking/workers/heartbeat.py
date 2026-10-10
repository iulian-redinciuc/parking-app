"""Loop heartbeat file and its check: the Docker healthcheck of the workers (deployment.md §4.2).

A worker calls `LoopHeartbeat.beat(interval)` every time its loop comes round. That rewrites
the file (`HEARTBEAT_FILE`, `/tmp/heartbeat` in the image) with the longest silence allowed,
`max(3 × interval, MIN_MAX_AGE_S)` seconds; the file's modification time is the beat itself.

    python -m parking.workers.heartbeat [FILE]    # exit 0 = alive, 1 = stuck or not started

The health messages can't tell this: they come from their own thread and keep arriving while
the loop hangs in a camera read or a model call.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEFAULT_PATH = Path("/tmp/heartbeat")
MIN_MAX_AGE_S = 20.0  # floor: one slow frame (a 5 s camera timeout, a retry) isn't "stuck"
WRITE_EVERY_S = 1.0  # a flow loop comes round many times a second


def max_age_s(interval: float) -> float:
    return max(3 * interval, MIN_MAX_AGE_S)


class LoopHeartbeat:
    """`beat()` is cheap enough for every loop round; `path` None = off (CLI runs)."""

    def __init__(self, path: Path | None):
        self.path = path
        self._last = float("-inf")
        self._tmp = path.with_name(path.name + ".tmp") if path is not None else None

    def beat(self, interval: float = 0.0) -> None:
        if self.path is None or self._tmp is None:
            return
        now = time.monotonic()
        if now - self._last < WRITE_EVERY_S:
            return
        self._last = now
        # replaced in one step, so the check never reads a half-written file
        self._tmp.write_text(f"{max_age_s(interval):g}\n")
        os.replace(self._tmp, self.path)


def check(path: Path = DEFAULT_PATH, now: float | None = None) -> tuple[bool, str]:
    """(alive, reason). No file = the loop hasn't come round once yet."""
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
        limit = float(path.read_text())
    except FileNotFoundError:
        return False, f"{path}: no heartbeat yet"
    except (OSError, ValueError) as e:
        return False, f"{path}: {e}"
    if age > limit:
        return False, f"loop stuck: last heartbeat {age:.0f} s ago (limit {limit:g} s)"
    return True, f"last heartbeat {age:.0f} s ago (limit {limit:g} s)"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    alive, reason = check(Path(args[0]) if args else DEFAULT_PATH)
    print(reason)
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
