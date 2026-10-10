#!/usr/bin/env bash
# Nightly backup of the API machine and its off-machine copy; also lists and restores backups.
# Spec: docs/design/deployment.md §7 · Guide: docs/phases/phase-8-hardening.md P8.6 · docs/runbook.md
#
#   deploy/backup.sh                  # cron, 03:30: archive in data/backups/ (14 daily + 8 weekly kept),
#                                     # then archive + .env to the encrypted remote
#   deploy/backup.sh list             # archives here and on the remote
#   deploy/backup.sh env [NAME]       # fetch a backed-up .env from the remote (never overwrites one)
#   deploy/backup.sh restore [NAME]   # put an archive back (default: the newest); the API must be stopped
#
# The remote is the rclone remote "parking-backup" in deploy/rclone.conf (git-ignored, mode 600). It
# must be of type crypt: the archive holds the config and the lot's coordinates, .env holds the secrets.
# Exit codes: 0 done · 1 failed · 3 the local archive was written but NOT copied off this machine.
# A run leaves its outcome in data/backups/last-run.json; the API turns a bad or missing one into the
# "backup failed" admin alert (docs/design/notifications.md §5.1).
set -euo pipefail

cd "$(dirname "$0")"

RCLONE=${RCLONE:-rclone}
RCLONE_CONF=${BACKUP_RCLONE_CONFIG:-$PWD/rclone.conf}
REMOTE=${BACKUP_REMOTE:-parking-backup}
REMOTE_DAYS=${BACKUP_REMOTE_DAYS:-60}                 # a little over the 8 weeks kept locally
API=${BACKUP_API_CONTAINER:-parking-api}
ROOT=${BACKUP_ROOT:-$(cd .. && pwd)}                  # the folder holding config/ and data/
ENV_FILE=${BACKUP_ENV_FILE:-$PWD/.env}
ENV_NAME=${BACKUP_ENV_NAME:-$(hostname)}
BACKUPS=$ROOT/data/backups
CLI=/app/backend/.venv/bin/parking

MSG=
die() { MSG=$*; echo "backup: $*" >&2; exit 1; }
rc() { "$RCLONE" --config "$RCLONE_CONF" "$@"; }

# Fails unless the remote exists and encrypts.
remote_ready() {
  command -v "$RCLONE" >/dev/null || { echo "rclone is not installed"; return 1; }
  [ -f "$RCLONE_CONF" ] || { echo "no $RCLONE_CONF"; return 1; }
  local kind
  kind=$(rc listremotes --long | awk -v r="$REMOTE:" '$1 == r { print $2 }')
  [ -n "$kind" ] || { echo "no remote \"$REMOTE\" in $RCLONE_CONF"; return 1; }
  [ "$kind" = crypt ] || { echo "remote \"$REMOTE\" is of type $kind, not crypt: refusing to upload unencrypted"; return 1; }
}

local_archives() { find "$BACKUPS" -maxdepth 1 -name 'parking-*.tar.gz' -printf '%f\n' 2>/dev/null | sort; }
remote_archives() { rc lsf --files-only --include 'parking-*.tar.gz' "$REMOTE:backups" 2>/dev/null | sort; }

# The run's outcome for the API: {"ts", "status": ok | local_only | failed, "message"}.
record() {
  local status=failed tmp
  case "$1" in 0) status=ok ;; 3) status=local_only ;; esac
  # only where the API container has made the folder (it must stay the container user's)
  [ -d "$BACKUPS" ] && tmp=$(mktemp "$BACKUPS/.last-run.XXXXXX" 2>/dev/null) || return 0
  printf '{"ts": "%s", "status": "%s", "message": "%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" \
    "$(printf %s "$MSG" | tr -d '\n\t' | sed 's/\\/\\\\/g; s/"/\\"/g')" >"$tmp" &&
    chmod 644 "$tmp" && mv "$tmp" "$BACKUPS/last-run.json" || rm -f "$tmp"
}

cmd_run() {
  trap 'record $?' EXIT
  docker exec "$API" "$CLI" backup --out /app/data/backups || die "the local backup failed"
  local newest why
  newest=$(local_archives | tail -n 1)
  [ -n "$newest" ] || die "no archive in $BACKUPS"

  if ! why=$(remote_ready); then
    MSG="not copied off this machine ($why)"
    echo "backup: NOT copied off this machine ($why)" >&2
    exit 3
  fi
  rc copy --include 'parking-*.tar.gz' "$BACKUPS" "$REMOTE:backups" || die "the upload failed"
  if [ -f "$ENV_FILE" ]; then
    # a replaced .env is kept under env-old/ (the VAPID keys can't be recreated)
    rc copyto "$ENV_FILE" "$REMOTE:env/$ENV_NAME.env" --backup-dir "$REMOTE:env-old/$(date -u +%Y%m%d-%H%M%S)" ||
      die "the upload of .env failed"
  fi
  remote_archives | grep -qxF "$newest" || die "$newest is not on the remote after the upload"
  rc delete --min-age "${REMOTE_DAYS}d" --include 'parking-*.tar.gz' "$REMOTE:backups" ||
    echo "backup: couldn't delete archives older than $REMOTE_DAYS days on the remote" >&2
  echo "backup: $newest and .env copied to $REMOTE (encrypted)"
}

cmd_list() {
  echo "Here ($BACKUPS):"
  local_archives | sed 's/^/  /'
  local why
  if why=$(remote_ready); then
    echo "Remote ($REMOTE:backups):"
    remote_archives | sed 's/^/  /'
    echo "Remote .env files ($REMOTE:env):"
    rc lsf --files-only "$REMOTE:env" 2>/dev/null | sed 's/\.env$//; s/^/  /'
  else
    echo "Remote: not available ($why)"
  fi
}

cmd_env() {
  local why name=${1:-}
  why=$(remote_ready) || die "$why"
  [ ! -e "$ENV_FILE" ] || die "$ENV_FILE exists: not overwriting it (move it away first)"
  if [ -z "$name" ]; then
    name=$(rc lsf --files-only "$REMOTE:env" | sed 's/\.env$//')
    [ -n "$name" ] || die "no .env on the remote"
    [ "$(wc -l <<<"$name")" = 1 ] || die "several .env files on the remote, name one: $(tr '\n' ' ' <<<"$name")"
  fi
  (umask 077 && rc copyto "$REMOTE:env/$name.env" "$ENV_FILE") || die "couldn't fetch $name.env"
  [ -s "$ENV_FILE" ] || die "$name.env is not on the remote"
  echo "backup: wrote $ENV_FILE (from $name)"
}

cmd_restore() {
  local name=${1:-} why image
  if [ -z "$name" ]; then
    if why=$(remote_ready); then name=$(remote_archives | tail -n 1); else name=$(local_archives | tail -n 1); fi
    [ -n "$name" ] || die "no archive found (remote: ${why:-empty})"
  fi
  [[ "$name" =~ ^parking-[0-9]{8}-[0-9]{4}\.tar\.gz$ ]] || die "not an archive name: $name"
  [ -z "$(docker ps -q --filter "name=^$API\$")" ] || die "$API is running: stop it first (docker stop $API)"
  if [ ! -f "$BACKUPS/$name" ]; then
    why=$(remote_ready) || die "$name is not in $BACKUPS and the remote is not available ($why)"
    mkdir -p "$BACKUPS" || die "can't create $BACKUPS"
    rc copy --include "$name" "$REMOTE:backups" "$BACKUPS" || die "couldn't fetch $name"
    [ -f "$BACKUPS/$name" ] || die "$name is not on the remote"
  fi
  # the image: the stopped API container's, else the release pinned in .env
  image=${BACKUP_IMAGE:-$(docker inspect -f '{{.Config.Image}}' "$API" 2>/dev/null || true)}
  if [ -z "$image" ]; then
    image=$(sed -n 's/^PARKING_VERSION=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
    [ -n "$image" ] || die "no $API container and no PARKING_VERSION in $ENV_FILE: set BACKUP_IMAGE"
    image=ghcr.io/iulian-redinciuc/parking-api:$image
  fi
  mkdir -p "$ROOT/config" "$ROOT/data"
  docker run --rm --network none --user 1000:1000 --read-only --cap-drop ALL \
    -e PARKING_DB_URL=sqlite:////app/data/db/parking.sqlite \
    -v "$ROOT/config:/app/config" -v "$ROOT/data:/app/data" \
    "$image" "$CLI" restore "/app/data/backups/$name" || die "the restore failed"
  echo "backup: restored $name with $image. Now start the stack (docker compose ... up -d) and check /api/status."
}

case "${1:-run}" in
  run) cmd_run ;;
  list) cmd_list ;;
  env) cmd_env "${2:-}" ;;
  restore) cmd_restore "${2:-}" ;;
  *) die "usage: backup.sh [run | list | env [NAME] | restore [NAME]]" ;;
esac
