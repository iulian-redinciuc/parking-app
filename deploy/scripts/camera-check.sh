#!/usr/bin/env bash
# P4.2 "Done when": can the vision host reach its network cameras, from the host and from inside the
# vision container? Spec: docs/design/hardware.md §3 · Guide: docs/phases/phase-4-occupancy-camera.md P4.2
#
#   cd /opt/parking/deploy
#   ./scripts/camera-check.sh -f docker-compose.site.yml        # T2 lot box (the -f files of `up`)
#   ./scripts/camera-check.sh                                    # everything on one machine
#
# Reads CAM_GROUND_RTSP_URL, CAM_GROUND_SNAPSHOT_URL and CAM_RAMP_RTSP_URL from .env (ENV_FILE=<path>);
# empty ones are skipped. For each URL, on the host: ffprobe (without FFmpeg on the host: the vision
# image's ffprobe with --network host) and curl; inside the container (docker compose run --rm --no-deps vision-occupancy): ffprobe, and
# `parking grab` with the snapshot URL, i.e. the worker's own snapshot code (digest or basic login).
# The URLs only travel in the environment (the container gets the same values with -e NAME), and every line printed has user:pass@ replaced by ***@.
# Exit 0 when every check that ran passed. A camera on the box itself (picamera:/device:) has no URL:
# check it with `parking grab` (deployment.md §4.3).
set -uo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=${ENV_FILE:-$PWD/.env}
[ -f "$ENV_FILE" ] || { echo "camera-check: $ENV_FILE not found" >&2; exit 2; }

# env_value NAME: the value of NAME= in the .env file (last one wins, quotes removed), never sourced
env_value() {
  NAME=$1 awk -F= '$1 == ENVIRON["NAME"] { sub(/^[^=]*=/, ""); v = $0 } END { print v }' "$ENV_FILE" |
    sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
quiet() { grep -vE '^ *Container |level=warning' || true; }  # docker compose's own chatter
redact() { sed -E 's#(//)[^/@[:space:]]*@#\1***@#g; s#((user|password|pwd)=)[^&[:space:]]*#\1***#g'; }

declare -A URL=(
  [CAM_GROUND_RTSP_URL]=$(env_value CAM_GROUND_RTSP_URL)
  [CAM_GROUND_SNAPSHOT_URL]=$(env_value CAM_GROUND_SNAPSHOT_URL)
  [CAM_RAMP_RTSP_URL]=$(env_value CAM_RAMP_RTSP_URL)
)
fails=0
ran=0
result() {  # result <label> <exit code> <output>
  ran=$((ran + 1))
  if [ "$2" -eq 0 ]; then
    printf 'PASS  %-46s %s\n' "$1" "$(tail -n1 <<<"$3" | redact)"
  else
    fails=$((fails + 1))
    printf 'FAIL  %-46s %s\n' "$1" "$(tail -n2 <<<"$3" | tr '\n' ' ' | redact)"
  fi
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
compose=(docker compose "$@")

for name in CAM_GROUND_RTSP_URL CAM_RAMP_RTSP_URL; do
  url=${URL[$name]}
  [ -n "$url" ] || { echo "skip  $name is empty"; continue; }
  probe='ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height -of csv=p=0 "$U"'
  if command -v ffprobe >/dev/null; then
    label="host: ffprobe $name"
    out=$(U=$url timeout 30 sh -c "$probe" 2>&1)
  else  # no FFmpeg on the host: the vision image's ffprobe in the host's network namespace
    label="host network: ffprobe $name"
    image=$("${compose[@]}" config --images vision-occupancy 2>/dev/null)
    out=$(U=$url timeout 60 docker run --rm --network host -e U --entrypoint sh "$image" -c "$probe" 2>&1)
  fi
  rc=$?
  [ $rc -eq 0 ] && [ -n "$out" ] || rc=${rc/#0/1}
  result "$label" "$rc" "$out"
  out=$(export "$name=$url"; timeout 120 "${compose[@]}" run --rm --no-deps -T -e "$name" --entrypoint sh vision-occupancy -c \
    "ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height -of csv=p=0 \"\$$name\"" 2>&1)
  rc=$?
  [ $rc -eq 0 ] && [ -n "$out" ] || rc=${rc/#0/1}
  result "container: ffprobe $name" "$rc" "$(quiet <<<"$out")"
done

url=${URL[CAM_GROUND_SNAPSHOT_URL]}
if [ -z "$url" ]; then
  echo "skip  CAM_GROUND_SNAPSHOT_URL is empty"
else
  out=$(U=$url O=$tmp/s.jpg timeout 30 sh -c 'curl -sS -f --anyauth --max-time 15 -o "$O" \
    -w "%{http_code} %{content_type} %{size_download} bytes" "$U"' 2>&1)
  rc=$?
  if [ $rc -eq 0 ] && [ "$(head -c2 "$tmp/s.jpg" | od -An -tx1 | tr -d ' ')" != ffd8 ]; then
    rc=1 out="$out: not a JPEG"
  fi
  result "host: curl CAM_GROUND_SNAPSHOT_URL" "$rc" "$out"
  out=$(export CAM_GROUND_SNAPSHOT_URL=$url; timeout 120 "${compose[@]}" run --rm --no-deps -T -e CAM_GROUND_SNAPSHOT_URL \
    --entrypoint sh vision-occupancy -c '/app/backend/.venv/bin/parking grab --camera cam-ground --source "snapshot:$CAM_GROUND_SNAPSHOT_URL" --out /tmp/camera-check.jpg --force' 2>&1)
  rc=$?
  result "container: parking grab CAM_GROUND_SNAPSHOT_URL" "$rc" "$(quiet <<<"$out")"
fi

echo "$((ran - fails)) of $ran checks passed"
[ "$ran" -gt 0 ] && [ "$fails" -eq 0 ]
