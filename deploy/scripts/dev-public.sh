#!/usr/bin/env bash
# Dev phone testing: the dev API over HTTPS through a Cloudflare quick tunnel (no account, no domain).
# Spec: docs/design/deployment.md §5 (Option Q) · Guide: docs/phases/phase-3-frontend.md P3.9
#
#   deploy/scripts/dev-public.sh up     # dev stack + quick tunnel, point the Pages preview at it
#   deploy/scripts/dev-public.sh url    # print the current https://<words>.trycloudflare.com address
#   deploy/scripts/dev-public.sh down   # stop the tunnel, Pages preview back to mock data
#
# The address changes every time the tunnel (re)starts: run `up` again after a reboot.
# Only the app's own parking-tunnel-quick container is used, never a cloudflared on the host.
# DEV_PUBLIC_WAIT=0 skips waiting for the Pages run to finish.
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER=parking-tunnel-quick
WORKFLOW=pages.yml
PREVIEW_URL=https://iulian-redinciuc.github.io/parking-app/
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile quick)

# The address of the running tunnel: the last one in the log of the container's current start.
tunnel_url() {
  local started
  started=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null) || return 1
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] || return 1
  docker logs --since "$started" "$CONTAINER" 2>&1 |
    grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -n 1 | grep .
}

# Rebuild the Pages preview with API_BASE=$1 (a URL or `mock`).
set_preview_api() {
  if [ "$(gh variable list --json name,value --jq '.[] | select(.name == "API_BASE") | .value')" = "$1" ]; then
    echo "API_BASE is already $1"
    return
  fi
  gh variable set API_BASE --body "$1"
  local before
  before=$(gh run list --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId // 0')
  gh workflow run "$WORKFLOW" --ref main
  echo "API_BASE=$1, Pages workflow started"
  [ "${DEV_PUBLIC_WAIT:-1}" = 1 ] || return 0
  local id=
  for _ in $(seq 1 30); do
    id=$(gh run list --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId // 0')
    [ "$id" != "$before" ] && break
    sleep 2
  done
  [ "$id" != "$before" ] || { echo "the Pages run didn't show up; check: gh run list --workflow $WORKFLOW" >&2; return 1; }
  gh run watch "$id" --exit-status --interval 10 >/dev/null
  echo "Pages preview deployed"
}

case "${1:-}" in
  up)
    "${COMPOSE[@]}" up -d
    url=
    for _ in $(seq 1 30); do
      url=$(tunnel_url) && break
      sleep 2
    done
    [ -n "$url" ] || { echo "no tunnel address in the log; check: docker logs $CONTAINER" >&2; exit 1; }
    ok=
    for _ in $(seq 1 30); do                      # the new hostname takes a few seconds to resolve
      curl -fsS --max-time 10 -o /dev/null "$url/healthz" 2>/dev/null && { ok=1; break; }
      sleep 2
    done
    [ -n "$ok" ] || { echo "$url/healthz doesn't answer; check: docker logs $CONTAINER" >&2; exit 1; }
    set_preview_api "$url"
    echo "API:     $url"
    echo "Preview: $PREVIEW_URL"
    ;;
  down)
    "${COMPOSE[@]}" rm --stop --force tunnel-quick
    set_preview_api mock
    ;;
  url)
    tunnel_url || { echo "the quick tunnel isn't running (deploy/scripts/dev-public.sh up)" >&2; exit 1; }
    ;;
  *)
    echo "usage: $0 up | down | url" >&2
    exit 2
    ;;
esac
