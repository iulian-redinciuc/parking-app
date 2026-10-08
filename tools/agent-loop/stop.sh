#!/usr/bin/env bash
# Stop the agent loop: by default after the current task, or immediately with --now.
set -uo pipefail
STATE="${STATE:-$HOME/.parking-loop}"

if [ "${1:-}" = "--now" ]; then
    tmux kill-session -t parking-loop 2>/dev/null && echo "Stopped now. The interrupted task resumes on the next start." \
        || echo "The loop wasn't running."
    exit 0
fi

mkdir -p "$STATE"
touch "$STATE/STOP"
echo "The loop will stop after the current task (or straight away if it's waiting)."
echo "To stop immediately instead: $0 --now"
