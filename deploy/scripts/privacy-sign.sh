#!/usr/bin/env bash
# The camera sign for the lot's entrances, as a printable A4 page (HTML), filled in from the server's .env.
# Spec: docs/design/security-privacy.md §4.1 (the sign text) · Guide: docs/phases/phase-8-hardening.md P8.9
#
#   deploy/scripts/privacy-sign.sh > sign.html          # English; open it in a browser and print (A4)
#   deploy/scripts/privacy-sign.sh ro > semn.html       # Romanian
#   chromium --headless --no-pdf-header-footer --print-to-pdf=sign.pdf sign.html   # or straight to PDF
#
# Reads PRIVACY_OPERATOR, PRIVACY_CONTACT and PUBLIC_HOST from deploy/.env (ENV_FILE=<path> for another
# file; variables set in the environment win), the same values the Privacy screen shows. Fails when one
# is empty: a sign without the operator's name and contact is not a sign. Change the text together with
# security-privacy.md §4.1 and the Privacy screen.
set -euo pipefail

ENV_FILE=$(realpath -m "${ENV_FILE:-$(dirname "$0")/../.env}")

die() { echo "privacy-sign: $*" >&2; exit 1; }

LANG_CODE=${1:-en}

# env_value <NAME>: the environment's value, else the .env file's (one pair of outer quotes removed).
env_value() {
  local value=${!1:-}
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
    value=$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)
    value=${value#[\'\"]}; value=${value%[\'\"]}
  fi
  printf '%s' "$value"
}

escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' <<<"$1"; }

operator=$(env_value PRIVACY_OPERATOR)
contact=$(env_value PRIVACY_CONTACT)
host=$(env_value PUBLIC_HOST)
missing=()
[ -n "$operator" ] || missing+=(PRIVACY_OPERATOR)
[ -n "$contact" ] || missing+=(PRIVACY_CONTACT)
[ -n "$host" ] || missing+=(PUBLIC_HOST)
[ ${#missing[@]} -eq 0 ] || die "set ${missing[*]} in $ENV_FILE (or the environment) first"

case $LANG_CODE in
  en)
    title="Camera in use: counting free parking spaces"
    body="The cameras count free and taken spaces. Pictures are analysed on site and discarded: no recording, no number-plate reading, no identification of people."
    l_operator="Operator"; l_contact="Contact"; l_details="Details and your rights" ;;
  ro)
    title="Zonă supravegheată video: numărarea locurilor de parcare libere"
    body="Camerele numără locurile libere și ocupate. Imaginile sunt analizate pe loc și șterse imediat: fără înregistrare, fără citirea numerelor de înmatriculare, fără identificarea persoanelor."
    l_operator="Operator"; l_contact="Contact"; l_details="Detalii și drepturile dumneavoastră" ;;
  *) die "unknown language: $LANG_CODE (en or ro)" ;;
esac

cat <<HTML
<!doctype html>
<html lang="$LANG_CODE">
<head>
<meta charset="utf-8">
<title>$(escape "$title")</title>
<style>
  @page { size: A4; margin: 15mm; }
  body { font-family: "DejaVu Sans", Arial, sans-serif; color: #000; margin: 0; }
  .sign { border: 3mm solid #000; padding: 12mm; min-height: 240mm; box-sizing: border-box; }
  svg { display: block; width: 60mm; height: 45mm; margin: 0 auto 8mm; }
  h1 { font-size: 30pt; line-height: 1.15; text-align: center; margin: 0 0 10mm; }
  p { font-size: 17pt; line-height: 1.4; margin: 0 0 8mm; }
  dl { font-size: 16pt; line-height: 1.35; margin: 12mm 0 0; }
  dt { font-weight: bold; margin-top: 5mm; }
  dd { margin: 0; overflow-wrap: anywhere; }
</style>
</head>
<body>
<div class="sign">
<svg viewBox="0 0 120 90" role="img" aria-label="CCTV"><rect x="14" y="22" width="70" height="34" rx="5"/><path d="M84 30 L112 18 V60 L84 48 Z"/><rect x="40" y="56" width="8" height="18"/><rect x="24" y="72" width="40" height="8"/><circle cx="30" cy="39" r="7" fill="#fff"/></svg>
<h1>$(escape "$title")</h1>
<p>$(escape "$body")</p>
<dl>
  <dt>$l_operator</dt><dd>$(escape "$operator")</dd>
  <dt>$l_contact</dt><dd>$(escape "$contact")</dd>
  <dt>$(escape "$l_details")</dt><dd>https://$(escape "$host")/#/privacy</dd>
</dl>
</div>
</body>
</html>
HTML
