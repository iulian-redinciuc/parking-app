#!/usr/bin/env bash
# Create a NEW production deploy/.env with fresh secrets (never the dev tokens, keys or passwords).
# Spec: docs/design/config.md §5, deployment.md §9, §10 · Guide: docs/phases/phase-8-hardening.md P8.2
#
#   PARKING_VERSION=v0.x.y PUBLIC_HOST=<hostname> ./prod-env.sh server   # on the cloud VM: new WORKER_TOKEN,
#                                                                    # ADMIN_TOKEN and VAPID keys; PUBLIC_HOST
#                                                                    # (optional here) also sets CORS_ORIGINS
#                                                                    # and PUBLIC_APP_URL (deployment.md §5-§6)
#   PARKING_VERSION=v0.x.y WORKER_TOKEN=<the server's> ./prod-env.sh site   # on the lot box
#
# Writes ../.env next to the compose files (ENV_FILE=<path> for another place), mode 600, and never
# overwrites an existing file or prints a secret. It ends with the list of values still to fill in.
# The VAPID keys come from the released API image (PARKING_CLI="uv run --project <repo>/backend parking" uses a checkout instead).
set -euo pipefail

cd "$(dirname "$0")/.."

die() { echo "prod-env: $*" >&2; exit 1; }

ROLE=${1:-}
[ "$ROLE" = server ] || [ "$ROLE" = site ] || die "usage: PARKING_VERSION=v0.x.y prod-env.sh server|site"
VERSION=${PARKING_VERSION:-}
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "set PARKING_VERSION to a release tag (v0.x.y), never latest"
TEMPLATE=$PWD/.env.example
ENV_FILE=${ENV_FILE:-$PWD/.env}
VPN_SERVER_IP=${VPN_SERVER_IP:-10.77.0.1}
VPN_SITE_IP=${VPN_SITE_IP:-10.77.0.2}
[ -f "$TEMPLATE" ] || die "$TEMPLATE not found"
[ ! -e "$ENV_FILE" ] || die "$ENV_FILE exists: not overwriting it (rotating secrets is a manual step, see the runbook)"

umask 077
tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.new"' EXIT
cp "$TEMPLATE" "$tmp"

# set_var <NAME> <value>: replace the template's NAME=... line.
set_var() {
  grep -q "^$1=" "$tmp" || die "$1 is not in .env.example"
  NAME=$1 VALUE=$2 awk -F= '$1 == ENVIRON["NAME"] { print $1 "=" ENVIRON["VALUE"]; next } { print }' "$tmp" >"$tmp.new"
  mv "$tmp.new" "$tmp"
}

set_var PARKING_VERSION "$VERSION"
if [ "$ROLE" = server ]; then
  set_var VPN_BIND_IP "$VPN_SERVER_IP"
  set_var WORKER_TOKEN "$(openssl rand -hex 32)"
  set_var ADMIN_TOKEN "$(openssl rand -hex 32)"
  read -r -a cli <<<"${PARKING_CLI:-docker run --rm ghcr.io/iulian-redinciuc/parking-api:$VERSION /app/backend/.venv/bin/parking}"
  keys=$("${cli[@]}" push vapid-keys) || die "couldn't generate the VAPID keys with: ${cli[*]}"
  for name in VAPID_PUBLIC_KEY VAPID_PRIVATE_KEY; do
    value=$(sed -n "s/^$name=//p" <<<"$keys")
    [ -n "$value" ] || die "no $name in the output of: ${cli[*]} push vapid-keys"
    set_var "$name" "$value"
  done
  public="PUBLIC_HOST, CORS_ORIGINS and PUBLIC_APP_URL (P8.3), "
  if [ -n "${PUBLIC_HOST:-}" ]; then
    [[ "$PUBLIC_HOST" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$PUBLIC_HOST" == *.* ]] \
      || die "PUBLIC_HOST must be a bare hostname (no https://, port or path): $PUBLIC_HOST"
    set_var PUBLIC_HOST "$PUBLIC_HOST"
    set_var CORS_ORIGINS "https://$PUBLIC_HOST"
    set_var PUBLIC_APP_URL "https://$PUBLIC_HOST/"
    public=
  fi
  todo="LOT_LAT / LOT_LON, ${public}VAPID_SUBJECT,
  ADMIN_PASSWORD_HASH (${cli[*]} admin hash-password; with docker add -it),
  TUNNEL_TOKEN (only for a Cloudflare tunnel instead of parking-web, P8.3),
  API_CPUS / API_MEMORY / WEB_CPUS / WEB_MEMORY after measuring (P8.4 step 2, P8.10)"
else
  [[ "${WORKER_TOKEN:-}" =~ ^[0-9a-f]{64}$ ]] || die "set WORKER_TOKEN to the value in the server's .env (64 hex characters)"
  set_var VPN_BIND_IP "$VPN_SITE_IP"
  gid=${DOCKER_GID:-$(getent group docker | cut -d: -f3 || true)}
  [[ "$gid" =~ ^[0-9]+$ ]] || die "no docker group on this machine: run provision.sh first (or set DOCKER_GID)"
  set_var DOCKER_GID "$gid"
  set_var WORKER_TOKEN "$WORKER_TOKEN"
  set_var API_INTERNAL_URL "http://$VPN_SERVER_IP:8000"
  todo="LOT_LAT / LOT_LON (same as the server), CAM_GROUND_SNAPSHOT_URL / CAM_GROUND_RTSP_URL,
  CAM_RAMP_RTSP_URL (Phase 5), VISION_CPUS / FLOW_CPUS / VISION_MEMORY / FLOW_MEMORY after measuring (P8.2 step 5)"
fi

mv -n "$tmp" "$ENV_FILE"
[ ! -e "$tmp" ] || die "$ENV_FILE appeared meanwhile: not overwriting it"
echo "Wrote $ENV_FILE ($ROLE, $VERSION)."
[ "$ROLE" = site ] || echo "Back it up somewhere private: replacing the VAPID keys breaks every push subscription."
echo "Still to fill in by hand: $todo"
