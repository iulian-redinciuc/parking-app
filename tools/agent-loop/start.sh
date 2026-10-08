#!/usr/bin/env bash
# Start the agent loop in a background tmux session called "parking-loop".
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${STATE:-$HOME/.parking-loop}"
mkdir -p "$STATE"

if tmux has-session -t parking-loop 2>/dev/null; then
    # The window stays open after the loop ends ("Press Enter to close"), so check the lock, not just the window.
    if ! flock -n "$STATE/lock" true 2>/dev/null; then
        echo "Already running. Watch it with:  tmux attach -t parking-loop"
        exit 0
    fi
    tmux kill-session -t parking-loop   # leftover window from a loop that already ended
fi
rm -f "$STATE/STOP"

tmux new-session -d -s parking-loop -x 200 -y 50 \
    "bash '$HERE/run.sh'; echo; echo 'Loop ended. Press Enter to close this window.'; read -r _"

echo "Agent loop started."
echo "  Watch live:   tmux attach -t parking-loop      (leave it running: Ctrl+B, then D)"
echo "  Quick status: $HERE/status.sh"
echo "  Stop:         $HERE/stop.sh           (after the current task)"
echo "                $HERE/stop.sh --now     (immediately; the task resumes on next start)"
