#!/usr/bin/env bash
# Security review of a deployment: the controls of docs/design/security-privacy.md §2, checked.
# Guide: docs/phases/phase-8-hardening.md P8.8
#
#   deploy/scripts/security-check.sh outside https://<PUBLIC_HOST> [address ...]
#       from a machine OUTSIDE (a laptop, the dev Pi): /internal/*, /control/* and /docs are 404
#       through the public entry, other origins get no CORS, the page has the CSP, admin routes
#       need a token, the login limit works, and nmap finds only the web ports (and SSH) on the
#       host and on every extra address (e.g. the lot's public address).
#         ADMIN_PASSWORD=...   also: log in, the token expires in 7 days, logout revokes it
#         SKIP_LOGIN_LIMIT=1   don't use up the 5 logins / 15 min of this IP
#         SKIP_NMAP=1          no port scan;  ALLOWED_PORTS="22 80 443"
#   deploy/scripts/security-check.sh server        on the API machine, in deploy/
#   deploy/scripts/security-check.sh site          on the lot box, in deploy/
#       .env is 600 and its secrets are set, long and not the dev ones; /internal/* and
#       /control/* answer 401 without the worker token; nothing but 80/443 is published on a
#       public interface.
#         DEV_FINGERPRINTS=file   output of `fingerprints` on the dev machine: no secret may match
#   deploy/scripts/security-check.sh fingerprints  sha256 of each secret in .env (to compare, not secret)
#   deploy/scripts/security-check.sh repo          gitleaks over the full history + Dependabot alerts
#
# It only reads (the login check posts wrong passwords). Exit code 1 when a check fails.
set -uo pipefail

MODE=${1:-}
ENV_FILE=${ENV_FILE:-$PWD/.env}
SECRETS="WORKER_TOKEN ADMIN_TOKEN ADMIN_PASSWORD_HASH VAPID_PRIVATE_KEY TUNNEL_TOKEN"
FAILED=0

ok() { echo "ok    $*"; }
bad() { echo "FAIL  $*"; FAILED=$((FAILED + 1)); }
skip() { echo "skip  $*"; }
# check <what> <got> <wanted, a regular expression>
check() { if [[ "$2" =~ ^($3)$ ]]; then ok "$1: $2"; else bad "$1: got '$2', wanted $3"; fi; }
code() { curl -s -o /dev/null -m "${CURL_TIMEOUT:-15}" -w '%{http_code}' "$@" || true; }
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
fingerprint() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

outside() {
  local base=${1%/} host origin=https://evil.example
  shift
  host=${base#*://}
  host=${host%%[:/]*}

  check "$base/healthz" "$(code "$base/healthz")" 200
  local path
  for path in /internal/observations /internal/health /internal/flow-events \
    /control/snapshot /control/reload /docs /openapi.json; do
    check "POST $path through the public entry" \
      "$(code -X POST -H 'Content-Type: application/json' -d '{}' "$base$path")" 404
  done

  local headers
  headers=$(curl -s -m 15 -D - -o /dev/null -H "Origin: $origin" "$base/api/status" | tr -d '\r')
  if grep -qi '^access-control-allow-origin:' <<<"$headers"; then
    bad "CORS: $origin is allowed to read /api/status"
  else
    ok "CORS: no Access-Control-Allow-Origin for $origin"
  fi
  headers=$(curl -s -m 15 -D - -o /dev/null -X OPTIONS -H "Origin: $origin" \
    -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization' \
    "$base/api/admin/login" | tr -d '\r')
  if grep -qi '^access-control-allow-origin:' <<<"$headers"; then
    bad "CORS: preflight from $origin is accepted"
  else
    ok "CORS: preflight from $origin is refused"
  fi

  local page
  page=$(curl -s -m 15 "$base/")
  if grep -q "http-equiv=\"Content-Security-Policy\" content=\"default-src[^\"]*self" <<<"$page"; then
    ok "CSP <meta> in the page"
  else
    bad "no CSP <meta> in $base/"
  fi
  headers=$(curl -s -m 15 -D - -o /dev/null "$base/" | tr -d '\r')
  local name
  for name in strict-transport-security x-content-type-options content-security-policy; do
    if grep -qi "^$name:" <<<"$headers"; then ok "header $name"; else bad "no $name header on $base/"; fi
  done
  check "http:// redirects to https://" "$(code "http://$host/")" '30[18]'

  check "admin route without a token" "$(code "$base/api/admin/session")" 401
  check "admin route with a wrong token" \
    "$(code -H 'Authorization: Bearer wrong' "$base/api/admin/session")" 401

  if [ -n "${ADMIN_PASSWORD:-}" ]; then
    local login token expires days
    login=$(PASSWORD=$ADMIN_PASSWORD python3 -c 'import json, os; print(json.dumps({"password": os.environ["PASSWORD"]}))' |
      curl -s -m 15 -H 'Content-Type: application/json' -d @- "$base/api/admin/login")
    token=$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' <<<"$login")
    expires=$(sed -n 's/.*"expires_at": *"\([^"]*\)".*/\1/p' <<<"$login")
    if [ -z "$token" ]; then
      bad "login with ADMIN_PASSWORD: $login"
    else
      days=$((($(date -d "$expires" +%s) - $(date +%s) + 3600) / 86400))
      check "session token expires in (days)" "$days" 7
      check "session token works" "$(code -H "Authorization: Bearer $token" "$base/api/admin/session")" 200
      check "logout" "$(code -X POST -H "Authorization: Bearer $token" "$base/api/admin/logout")" 204
      check "the token after logout" "$(code -H "Authorization: Bearer $token" "$base/api/admin/session")" 401
    fi
  else
    skip "login / expiry / logout (set ADMIN_PASSWORD)"
  fi

  if [ -z "${SKIP_LOGIN_LIMIT:-}" ]; then
    local i last=
    for i in 1 2 3 4 5 6; do
      last=$(code -H 'Content-Type: application/json' -d '{"password":"security-check wrong password"}' \
        "$base/api/admin/login")
      [ "$last" = 429 ] && break
    done
    check "login limit (429 by the 6th wrong password; this IP is locked out for 15 min)" "$last" 429
  else
    skip "login rate limit (SKIP_LOGIN_LIMIT)"
  fi

  if [ -n "${SKIP_NMAP:-}" ]; then
    skip "port scan (SKIP_NMAP)"
  elif ! command -v nmap >/dev/null; then
    bad "nmap is not installed (or set SKIP_NMAP=1)"
  else
    local target open extra
    for target in "$host" "$@"; do
      open=$(nmap -Pn -T4 -p- --open -oG - "$target" | grep -o '[0-9]*/open/tcp' | cut -d/ -f1 | sort -n | xargs)
      extra=$(for port in $open; do
        [[ " ${ALLOWED_PORTS:-22 80 443} " == *" $port "* ]] || printf '%s ' "$port"
      done)
      if [ -n "$extra" ]; then bad "$target: open TCP ports besides ${ALLOWED_PORTS:-22 80 443}: $extra"; else ok "$target: open TCP ports: ${open:-none}"; fi
    done
  fi
}

check_env() {
  [ -f "$ENV_FILE" ] || { bad "$ENV_FILE not found (run this in deploy/)"; return; }
  check "$ENV_FILE mode" "$(stat -c %a "$ENV_FILE")" 600
  check "$ENV_FILE owner" "$(stat -c %U "$ENV_FILE")" "$(id -un)"
  local name value
  for name in "$@"; do
    value=$(env_value "$name")
    if [ "${#value}" -ge 43 ]; then ok "$name is set (${#value} characters)"; else bad "$name is missing or shorter than 32 random bytes"; fi
  done
  if [ "$(env_value LOG_LEVEL | tr a-z A-Z)" = DEBUG ]; then bad "LOG_LEVEL=DEBUG (serves /docs)"; else ok "LOG_LEVEL is not DEBUG"; fi
  if [ "$(env_value DEBUG_CAPTURE | tr A-Z a-z)" = true ]; then bad "DEBUG_CAPTURE=true (frames kept on disk)"; else ok "DEBUG_CAPTURE is off"; fi
  if [ -z "${DEV_FINGERPRINTS:-}" ]; then
    skip "secrets differ from dev (set DEV_FINGERPRINTS to the dev machine's \`fingerprints\` output)"
    return
  fi
  for name in $SECRETS; do
    value=$(env_value "$name")
    [ -n "$value" ] || continue
    if grep -q "^$name $(fingerprint "$value")\$" "$DEV_FINGERPRINTS"; then bad "$name is the dev machine's"; else ok "$name differs from dev"; fi
  done
}

# Ports the project's containers publish on every interface: only parking-web's 80/443 may be.
check_ports() {
  local line found=
  while read -r line; do
    [ -n "$line" ] || continue
    found=1
    local name=${line%% *} port
    for port in $(grep -oE '(0\.0\.0\.0|\[::\]|:::?)[:]?[0-9]+->' <<<"$line" | grep -oE '[0-9]+->' | tr -d '>-' | sort -u); do
      if [ "$name" = parking-web ] && { [ "$port" = 80 ] || [ "$port" = 443 ]; }; then continue; fi
      bad "$name publishes port $port on every interface"
      found=bad
    done
  done < <(docker ps --filter label=com.docker.compose.project=parking --format '{{.Names}} {{.Ports}}')
  [ "$found" = 1 ] && ok "published ports: only loopback / the VPN address (and parking-web's 80, 443)"
  [ -n "$found" ] || bad "no running container of the parking project"
}

server() {
  check_env WORKER_TOKEN ADMIN_TOKEN ADMIN_PASSWORD_HASH VAPID_PRIVATE_KEY
  local host=$(env_value PUBLIC_HOST) cors=$(env_value CORS_ORIGINS)
  if [ -n "$host" ]; then check "CORS_ORIGINS" "$cors" "https://$host"; else skip "CORS_ORIGINS = https://PUBLIC_HOST (no PUBLIC_HOST)"; fi
  local api=http://127.0.0.1:$(env_value API_HOST_PORT | grep . || echo 8000)
  check "/internal/health without a token" "$(code -X POST -d '{}' "$api/internal/health")" 401
  check "/internal/health with a wrong token" \
    "$(code -X POST -d '{}' -H 'Authorization: Bearer wrong' "$api/internal/health")" 401
  check "/docs" "$(code "$api/docs")" 404
  check_ports
}

site() {
  check_env WORKER_TOKEN
  local ip=$(env_value VPN_BIND_IP) port
  for port in 9000 9001; do
    local got=$(code "http://$ip:$port/control/snapshot")
    if [ "$got" = 000 ]; then skip "worker control port $port (not running)"; else check "/control/snapshot on $ip:$port without a token" "$got" 401; fi
  done
  check_ports
}

fingerprints() {
  [ -f "$ENV_FILE" ] || { echo "$ENV_FILE not found" >&2; exit 2; }
  local name value
  for name in $SECRETS; do
    value=$(env_value "$name")
    [ -z "$value" ] || echo "$name $(fingerprint "$value")"
  done
}

repo() {
  local root alerts
  root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  if "${GITLEAKS:-gitleaks}" detect --source "$root" --log-opts=--all --redact --no-banner >/dev/null 2>&1; then
    ok "gitleaks: full history clean ($(git -C "$root" rev-list --all --count) commits)"
  else
    bad "gitleaks found something, or isn't installed: ${GITLEAKS:-gitleaks} detect --source $root --log-opts=--all --redact"
  fi
  if alerts=$(gh api --paginate "repos/{owner}/{repo}/dependabot/alerts?state=open&severity=high,critical" -q '.[].html_url' 2>&1); then
    check "open high/critical Dependabot alerts" "$(grep -c . <<<"$alerts")" 0
  else
    bad "Dependabot alerts can't be read: $alerts"
  fi
}

case "$MODE" in
  outside)
    [[ "${2:-}" == https://* ]] || { echo "usage: $0 outside https://<PUBLIC_HOST> [address ...]" >&2; exit 2; }
    shift
    outside "$@"
    ;;
  server | site | repo) "$MODE" ;;
  fingerprints)
    fingerprints
    exit 0
    ;;
  *)
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
if [ "$FAILED" -gt 0 ]; then
  echo "$FAILED check(s) failed"
  exit 1
fi
echo "all checks passed"
