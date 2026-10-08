#!/usr/bin/env bash
# Show what the agent loop is doing. Use -f to follow the live log.
STATE="${STATE:-$HOME/.parking-loop}"

if ! flock -n "$STATE/lock" true 2>/dev/null; then echo "Loop process: running (tmux session parking-loop)"
else echo "Loop process: not running"; fi
echo
cat "$STATE/status.txt" 2>/dev/null || echo "No status yet. Start the loop with start.sh"
echo
if [ "${1:-}" = "-f" ]; then
    tail -n 30 -f "$STATE/logs/current.log"
else
    echo "Last activity:"
    tail -n 15 "$STATE/logs/current.log" 2>/dev/null
fi
