#!/usr/bin/env bash
# After a reboot: how long until the stack is back by itself (target: live data within 3 minutes).
# Spec: docs/design/deployment.md §4.1 · Guide: docs/phases/phase-8-hardening.md P8.4
#
#   sudo reboot                               # then log in again and, without starting anything:
#   deploy/scripts/boot-check.sh server       # the API machine (also the dev Pi / one machine, T1)
#   deploy/scripts/boot-check.sh site         # the lot box: workers only
#   deploy/scripts/boot-check.sh server ground        # only these zones have to be fresh
#
# Waits until every container of the "parking" Compose project runs and is healthy and, on the
# server, until /api/status has no stale zone (the workers are delivering again). Prints the seconds
# since boot at that moment; fails when that is more than BOOT_LIMIT_S (180) or nothing changes for
# BOOT_WAIT_S (300). It only reads: it never starts or restarts a container.
#
# Rehearsals (no reboot): BOOT_PROJECT=<compose project> (default parking), BOOT_API_CONTAINER=<name>,
# BOOT_SINCE=<epoch seconds of the simulated power cut>: counts from that moment instead of the boot.
set -euo pipefail

ROLE=${1:-}
[ "$ROLE" = server ] || [ "$ROLE" = site ] || { echo "usage: $0 server|site [zone ...]" >&2; exit 2; }
shift
ZONES="$*"
LIMIT=${BOOT_LIMIT_S:-180}
WAIT=${BOOT_WAIT_S:-300}
API=${BOOT_API_CONTAINER:-parking-api}
PROJECT=${BOOT_PROJECT:-parking}
SINCE=${BOOT_SINCE:-}
FROM="boot"
[ -z "$SINCE" ] || FROM="the cut"

# Seconds since boot, or since BOOT_SINCE.
uptime_s() {
  if [ -n "$SINCE" ]; then echo $(( $(date +%s) - SINCE )); else awk '{ print int($1) }' /proc/uptime; fi
}

# Containers of the project that aren't "running" + ("healthy" or without a healthcheck).
not_ready() {
  docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.Names}} {{.State}} {{.Status}}' |
    awk '!($2 == "running" && $0 !~ /\((unhealthy|health: starting)\)/) { printf "%s(%s) ", $1, $2 }'
}

# Stale zones in /api/status (all zones, or the ones named on the command line), asked inside the
# API container so the host needs neither python nor jq.
stale_zones() {
  docker exec -e ZONES="$ZONES" "$API" python -c '
import json, os, urllib.request
status = json.load(urllib.request.urlopen("http://localhost:8000/api/status", timeout=5))
wanted = os.environ["ZONES"].split()
missing = [z for z in wanted if z not in {zone["id"] for zone in status["zones"]}]
stale = [z["id"] for z in status["zones"] if z["stale"] and (not wanted or z["id"] in wanted)]
print(" ".join(missing + stale))
' 2>/dev/null || echo "api-unreachable"
}

[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ] ||
  { echo "boot-check: no containers of the $PROJECT project on this machine" >&2; exit 1; }

deadline=$(( $(date +%s) + WAIT ))
while :; do
  waiting=$(not_ready)
  if [ -z "$waiting" ] && [ "$ROLE" = server ]; then
    stale=$(stale_zones)
    [ -z "$stale" ] || waiting="stale: $stale"
  fi
  up=$(uptime_s)
  if [ -z "$waiting" ]; then
    echo "ready ${up} s after $FROM (limit ${LIMIT} s)"
    docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '  {{.Names}}: {{.Status}}'
    [ "$up" -le "$LIMIT" ] && exit 0
    echo "boot-check: later than the limit (or the check was started late: run it right after the reboot)" >&2
    exit 1
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "boot-check: not ready ${up} s after $FROM, still waiting for: $waiting" >&2
    exit 1
  fi
  sleep 2
done
