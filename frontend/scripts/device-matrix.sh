#!/usr/bin/env bash
# P6.8 device test matrix, live on the dev machine (notifications.md §6.3):
# a throwaway `parking api` (temporary database, the dev VAPID keys from deploy/.env, a random
# worker token), the app built against it and served on localhost, then the Playwright projects
# of playwright.devices.config.ts (emulated phones + desktop on Chromium, real push service).
# Everything is stopped and the temporary folder removed at the end.
#
#   frontend/scripts/device-matrix.sh [playwright args…]
#
# Without arguments the projects (= devices) run one after another, and the API is restarted
# before each: its push budget (20 POSTs / hour per client IP, in memory) would otherwise run out
# on localhost after two devices. Arguments go to Playwright as they are, in one run.
#
# Env: DM_API_PORT (8090), DM_APP_PORT (4175), PW_CHROMIUM_PATH (a system Chromium, e.g.
# /usr/bin/chromium on the dev Pi), DM_KEEP=1 keeps the temporary folder.
set -euo pipefail

FRONTEND="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$FRONTEND")"
API_PORT="${DM_API_PORT:-8090}"
APP_PORT="${DM_APP_PORT:-4175}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/parking-device-matrix.XXXXXX")"
PIDS=()

cleanup() {
  # each server runs in its own process group (setsid), so npx's children go too
  for pid in "${PIDS[@]}"; do kill -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  [[ "${DM_KEEP:-}" == 1 ]] && echo "kept $TMP" || rm -rf "$TMP"
}
trap cleanup EXIT

grep -q '^VAPID_PUBLIC_KEY=.' "$ROOT/deploy/.env" 2>/dev/null || {
  echo "needs VAPID keys in deploy/.env (uv run parking push vapid-keys)" >&2
  exit 1
}

# The API: deploy/.env supplies the VAPID keys; these override the rest for this run only.
export PARKING_DB_URL="sqlite:///$TMP/parking.sqlite"
export WORKER_TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
export CORS_ORIGINS="http://localhost:$APP_PORT"
export PUBLIC_APP_URL="http://localhost:$APP_PORT/"
export LOT_LAT=51.5007 LOT_LON=-0.1246
export TZ=Europe/Bucharest
API_PID=
start_api() {
  (cd "$ROOT/backend" && exec setsid uv run parking api --config "$ROOT/config/lot.yaml" \
    --port "$API_PORT") >>"$TMP/api.log" 2>&1 &
  API_PID=$!
  PIDS+=("$API_PID")
  wait_for "http://127.0.0.1:$API_PORT/healthz"
}
stop_api() {
  kill -- "-$API_PID" 2>/dev/null || true
  while curl -fsS -o /dev/null "http://127.0.0.1:$API_PORT/healthz" 2>/dev/null; do sleep 0.5; done
}
wait_for() {
  for _ in $(seq 60); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0 || sleep 1; done
  echo "not up: $1" >&2
  cat "$TMP/api.log" >&2
  exit 1
}

# The app, built against that API and served at / (as production does; preview reads the base too).
export VITE_API_BASE="http://127.0.0.1:$API_PORT" VITE_BASE=/
(
  cd "$FRONTEND"
  npx vite build --outDir "$TMP/dist" \
    --emptyOutDir >"$TMP/build.log" 2>&1
) || {
  cat "$TMP/build.log" >&2
  exit 1
}
(cd "$FRONTEND" && exec setsid npx vite preview --outDir "$TMP/dist" --port "$APP_PORT" --strictPort) \
  >"$TMP/preview.log" 2>&1 &
PIDS+=($!)

wait_for "http://localhost:$APP_PORT/"

status=0
run() {
  (
    cd "$FRONTEND"
    DM_API_URL="http://127.0.0.1:$API_PORT" DM_APP_URL="http://localhost:$APP_PORT/" \
      DM_WORKER_TOKEN="$WORKER_TOKEN" DM_REPORT="$TMP/report-$1.json" \
      npx playwright test -c playwright.devices.config.ts "${@:2}"
  ) || status=$?
}
if (($#)); then
  start_api
  run all "$@"
else
  for project in 'Android Chrome' 'iPhone installed' 'iPhone Safari' 'Desktop Chrome'; do
    start_api
    run "${project// /-}" --project "$project"
    stop_api
  done
fi

# What the API sent, from its own log table: one line per kind and outcome.
echo "notification_log:"
(cd "$ROOT/backend" && uv run python -c '
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
for kind, st, n in db.execute(
    "select kind, status, count(*) from notification_log group by kind, status order by kind"
):
    print(f"  {kind:12} {st:14} {n}")
' "$TMP/parking.sqlite")
exit "$status"
