#!/usr/bin/env bash
# Set up a production machine for topology T2 (never the dev Pi): automatic security updates, Docker,
# a deny-by-default firewall, SSH with keys only, the private WireGuard VPN and /opt/parking.
# Spec: docs/design/deployment.md §9, §10 · Guide: docs/phases/phase-8-hardening.md P8.2
#
#   sudo ./provision.sh server                       # the cloud VM (API)
#   sudo ./provision.sh site                         # the Raspberry Pi at the lot (vision host)
# Each first run prints the machine's WireGuard public key. Run it again with the other machine's key:
#   sudo ./provision.sh server --peer-key <site key>
#   sudo ./provision.sh site   --peer-key <server key> --endpoint <server public IP or name>
# Options:
#   --version v0.x.y   also copy that release's deploy/ and config/ into /opt/parking
#   --public-proxy     server: open 80/443 for a reverse proxy (deployment.md §5 Option B); not for a tunnel
#
# Safe to run again. For Debian 12+, Raspberry Pi OS (64-bit) and Ubuntu 24.04+.
# DRY_RUN=1 prints every command and file instead of changing anything (no root needed).
# It refuses to turn off SSH passwords while the login user has no authorized key.
set -euo pipefail

VPN_SERVER_IP=${VPN_SERVER_IP:-10.77.0.1}
VPN_SITE_IP=${VPN_SITE_IP:-10.77.0.2}
VPN_PORT=${VPN_PORT:-51820}
PREFIX=${PARKING_PREFIX:-/opt/parking}
REPO=iulian-redinciuc/parking-app
DRY_RUN=${DRY_RUN:-0}

die() { echo "provision: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then echo "+ $*"; else "$@"; fi
}

# write_file <path> <mode>: the content comes on stdin.
write_file() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ write $1 (mode $2)"
    sed 's/^/    /'
  else
    install -D -m "$2" /dev/stdin "$1"
  fi
}

ROLE=${1:-}
[ "$ROLE" = server ] || [ "$ROLE" = site ] || die "usage: provision.sh server|site [--peer-key KEY] [--endpoint HOST] [--version TAG] [--public-proxy]"
shift
PEER_KEY= ENDPOINT= VERSION= PUBLIC_PROXY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --peer-key) PEER_KEY=${2:?--peer-key needs a value}; shift 2 ;;
    --endpoint) ENDPOINT=${2:?--endpoint needs a value}; shift 2 ;;
    --version) VERSION=${2:?--version needs a value}; shift 2 ;;
    --public-proxy) PUBLIC_PROXY=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
[ "$ROLE" = server ] || [ "$PUBLIC_PROXY" = 0 ] || die "--public-proxy is for the server only"
[ "$ROLE" = server ] || [ -z "$PEER_KEY" ] || [ -n "$ENDPOINT" ] || die "the site needs --endpoint <server public IP or name> with --peer-key"
[ -z "$VERSION" ] || [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "--version must look like v0.1.0"

# --- Preflight: nothing is changed before these pass ---
OS_ID=$(. /etc/os-release && echo "$ID")
OS_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
case "$OS_ID" in
  debian | ubuntu) ;;
  *) die "unsupported system '$OS_ID' (Debian, 64-bit Raspberry Pi OS or Ubuntu expected)" ;;
esac
[ "$DRY_RUN" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo (or DRY_RUN=1 to only print)"

DEPLOY_USER=${SUDO_USER:-$(id -un)}
DEPLOY_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
if ! grep -qsE '^(ssh-|ecdsa-|sk-)' "$DEPLOY_HOME/.ssh/authorized_keys"; then
  [ "$DRY_RUN" = 1 ] || die "$DEPLOY_USER has no SSH key in ~/.ssh/authorized_keys: add one (ssh-copy-id) first, or you'd be locked out"
  echo "note: $DEPLOY_USER has no authorized SSH key; a real run would stop here"
fi

if [ "$ROLE" = server ]; then
  MY_IP=$VPN_SERVER_IP PEER_IP=$VPN_SITE_IP
else
  MY_IP=$VPN_SITE_IP PEER_IP=$VPN_SERVER_IP
fi

step "Packages"
export DEBIAN_FRONTEND=noninteractive
run apt-get update
run apt-get install -y ca-certificates curl openssl ufw unattended-upgrades wireguard-tools

step "Automatic security updates"
write_file /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
# Raspberry Pi OS: the kernel and firmware come from its own archive. Reboots (kernel updates) at 04:00.
write_file /etc/apt/apt.conf.d/52parking-unattended 644 <<'EOF'
Unattended-Upgrade::Origins-Pattern {
        "origin=Raspberry Pi Foundation,codename=${distro_codename}";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
run systemctl enable --now unattended-upgrades

step "Docker"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  echo "docker and the compose plugin are already installed"
else
  run install -m 0755 -d /etc/apt/keyrings
  run curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" -o /etc/apt/keyrings/docker.asc
  write_file /etc/apt/sources.list.d/docker.list 644 <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$OS_ID $OS_CODENAME stable
EOF
  run apt-get update
  run apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
run systemctl enable --now docker
[ "$DEPLOY_USER" = root ] || run usermod -aG docker "$DEPLOY_USER"

step "SSH: keys only"
# 00-: sshd keeps the first value it reads, and cloud images ship their own 50-*.conf
write_file /etc/ssh/sshd_config.d/00-parking.conf 644 <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
run sshd -t
run systemctl reload ssh
if [ "$DRY_RUN" != 1 ]; then
  sshd -T | grep -qx 'passwordauthentication no' || die "sshd still accepts passwords: check /etc/ssh/sshd_config"
fi

step "Firewall: deny inbound except SSH$([ "$ROLE" = server ] && echo " and the VPN")"
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow 22/tcp
if [ "$ROLE" = server ]; then
  run ufw allow "$VPN_PORT/udp"
  run ufw allow in on wg0 to any port 8000 proto tcp
  if [ "$PUBLIC_PROXY" = 1 ]; then
    run ufw allow 80/tcp
    run ufw allow 443/tcp
    run ufw allow 443/udp
  fi
else
  # the lot box dials out to the server, so nothing else is open here
  run ufw allow in on wg0 to any port 9000:9001 proto tcp
fi
run ufw --force enable

step "WireGuard VPN ($MY_IP)"
if [ "$DRY_RUN" = 1 ]; then
  PRIVATE_KEY="<this machine's private key>" PUBLIC_KEY="<this machine's public key>"
  echo "+ wg genkey > /etc/wireguard/parking.key (kept if it exists)"
else
  umask 077
  mkdir -p /etc/wireguard
  [ -s /etc/wireguard/parking.key ] || wg genkey >/etc/wireguard/parking.key
  PRIVATE_KEY=$(cat /etc/wireguard/parking.key)
  PUBLIC_KEY=$(wg pubkey </etc/wireguard/parking.key)
  umask 022
fi
if [ -n "$PEER_KEY" ]; then
  {
    echo "[Interface]"
    echo "Address = $MY_IP/24"
    echo "PrivateKey = $PRIVATE_KEY"
    [ "$ROLE" = site ] || echo "ListenPort = $VPN_PORT"
    echo
    echo "[Peer]"
    echo "PublicKey = $PEER_KEY"
    echo "AllowedIPs = $PEER_IP/32"
    if [ "$ROLE" = site ]; then
      echo "Endpoint = $ENDPOINT:$VPN_PORT"
      echo "PersistentKeepalive = 25"
    fi
  } | write_file /etc/wireguard/wg0.conf 600
  run systemctl enable wg-quick@wg0
  run systemctl restart wg-quick@wg0
  # The containers publish ports on the VPN address, so Docker must start after the VPN.
  write_file /etc/systemd/system/docker.service.d/parking-vpn.conf 644 <<'EOF'
[Unit]
Wants=wg-quick@wg0.service
After=wg-quick@wg0.service
EOF
  run systemctl daemon-reload
else
  echo "no --peer-key yet: the VPN is not configured"
fi

step "$PREFIX"
run install -d -o "$DEPLOY_USER" "$PREFIX" "$PREFIX/deploy"
# the containers run as uid 1000 and write to config/ (slot editor) and data/
run install -d -o 1000 -g 1000 "$PREFIX/config" "$PREFIX/data" "$PREFIX/models"
if [ -n "$VERSION" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ copy deploy/ and config/ of $VERSION from github.com/$REPO into $PREFIX (existing config files are kept)"
  else
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz" | tar -xz -C "$tmp" --strip-components=1
    cp -r "$tmp/deploy/." "$PREFIX/deploy/"
    cp -rn "$tmp/config/." "$PREFIX/config/"
    chown -R "$DEPLOY_USER" "$PREFIX/deploy"
    chown -R 1000:1000 "$PREFIX/config"
    rm -rf "$tmp"
  fi
fi

echo
echo "Done ($ROLE)."
echo "WireGuard public key of this machine: $PUBLIC_KEY"
if [ -z "$PEER_KEY" ]; then
  if [ "$ROLE" = server ]; then
    echo "Next: run this script on the lot box, then again here with  --peer-key <its key>"
  else
    echo "Next: run this script again with  --peer-key <the server's key> --endpoint <server public IP or name>"
  fi
else
  echo "Check the VPN:  ping -c 3 $PEER_IP"
  echo "Next: $PREFIX/deploy/scripts/prod-env.sh $ROLE   (docs/phases/phase-8-hardening.md P8.2)"
fi
