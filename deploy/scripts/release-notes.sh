#!/usr/bin/env bash
# Release notes (Markdown, on stdout) from the commit messages between the previous v* tag and TAG.
# Spec: docs/design/deployment.md §8 · Guide: docs/phases/phase-8-hardening.md P8.1
#
#   deploy/scripts/release-notes.sh v0.1.0
#
# Used by .github/workflows/release.yml. Commits are grouped by the phase in their task ID
# (`P5.7: …` → Phase 5); the rest go under "Other". The first release lists the whole history.
set -euo pipefail

TAG=${1:?usage: release-notes.sh <tag>}
IMAGE_PREFIX=ghcr.io/iulian-redinciuc/parking

cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "$TAG^{commit}" > /dev/null || { echo "unknown tag: $TAG" >&2; exit 1; }

prev=$(git describe --tags --abbrev=0 --match 'v*' "$TAG^" 2> /dev/null || true)
range=${prev:+$prev..}$TAG

echo "## Images"
echo
echo '```bash'
echo "docker pull $IMAGE_PREFIX-api:$TAG"
echo "docker pull $IMAGE_PREFIX-vision:$TAG"
echo '```'
echo
echo "Both are built for \`linux/amd64\` and \`linux/arm64\`. Deploy with \`PARKING_VERSION=$TAG docker compose up -d\`"
echo "(docs/design/deployment.md §8)."
echo
if [ -n "$prev" ]; then
  echo "## Changes since $prev"
else
  echo "## Changes"
fi

# oldest first, one line per commit: "<phase, or 999 for Other>\t- <subject> (<short hash>)"
git log --reverse --no-merges --pretty='%s (%h)' "$range" |
  awk '{
    phase = 999
    if (match($0, /^P[0-9]+\.[0-9]+/)) { phase = substr($0, 2, RLENGTH - 1); sub(/\..*/, "", phase) }
    printf "%d\t- %s\n", phase, $0
  }' > "${TMPDIR:-/tmp}/release-notes.$$"
trap 'rm -f "${TMPDIR:-/tmp}/release-notes.$$"' EXIT

for phase in $(cut -f1 "${TMPDIR:-/tmp}/release-notes.$$" | sort -n | uniq); do
  lines=$(awk -F '\t' -v p="$phase" '$1 == p { sub(/^[0-9]+\t/, ""); print }' "${TMPDIR:-/tmp}/release-notes.$$")
  [ -n "$lines" ] || continue
  echo
  if [ "$phase" = 999 ]; then echo "### Other"; else echo "### Phase $phase"; fi
  echo
  echo "$lines"
done
