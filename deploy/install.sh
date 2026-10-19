#!/usr/bin/env bash
# Install Orion on a fresh Debian 12 / Ubuntu 24.04 server (an OVH VPS, for instance).
#
#   sudo ./install.sh [domain]
#
# Without a domain, the server's IP is used through sslip.io (e.g. 51-83-1-2.sslip.io), which is
# enough for Caddy to get a certificate. Safe to run again: it updates the code, never
# overwrites orion.toml or the secret files.
set -euo pipefail

REPO="${ORION_REPO:-https://github.com/noan-hbt/orion-agent.git}"
BRANCH="${ORION_BRANCH:-main}"
ROOT=/srv/orion
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    ip="$(curl -fsS4 https://api.ipify.org)"
    DOMAIN="${ip//./-}.sslip.io"
fi
echo "==> domain: $DOMAIN"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq git curl sqlite3 caddy ufw ca-certificates >/dev/null
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

echo "==> users"
getent group orion >/dev/null || groupadd --system orion
id orion-guardian >/dev/null 2>&1 || useradd --system --gid orion --home "$ROOT" --shell /usr/sbin/nologin orion-guardian
id orion-core >/dev/null 2>&1 || useradd --system --gid orion --home "$ROOT" --shell /usr/sbin/nologin orion-core

echo "==> code"
if [ -d "$ROOT/app/.git" ]; then
    git -C "$ROOT/app" fetch -q origin "$BRANCH"
    git -C "$ROOT/app" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
    install -d -m 0755 "$ROOT"
    git clone -q --branch "$BRANCH" "$REPO" "$ROOT/app"
fi
cd "$ROOT/app"
export UV_PYTHON_INSTALL_DIR="$ROOT/python"
uv sync --frozen --no-dev -q
chmod -R a+rX "$ROOT/app" "$ROOT/python"

echo "==> folders"
install -d -o orion-core -g orion -m 0750 "$ROOT/data" "$ROOT/backups/core"
install -d -o orion-guardian -g orion -m 0700 "$ROOT/guardian" "$ROOT/backups/guardian"
install -d -o orion-guardian -g orion -m 0750 "$ROOT/run"
install -d -m 0755 /etc/orion

if [ ! -f /etc/orion/orion.toml ]; then
    sed "s/@DOMAIN@/$DOMAIN/g" "$HERE/orion.production.toml" > /etc/orion/orion.toml
    chmod 0644 /etc/orion/orion.toml
    echo "    /etc/orion/orion.toml created: fill in your number and handle"
fi
ln -sf /etc/orion/orion.toml "$ROOT/orion.toml"
for who in guardian core; do
    if [ ! -f "/etc/orion/$who.env" ]; then
        install -m 0600 -o "orion-$who" -g orion "$HERE/$who.env.example" "/etc/orion/$who.env"
        echo "    /etc/orion/$who.env created: fill in the secrets"
    fi
done

echo "==> services"
install -m 0644 "$HERE"/orion-*.service "$HERE"/orion-*.timer /etc/systemd/system/
sed "s/@DOMAIN@/$DOMAIN/g" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl reload-or-restart caddy
systemctl enable -q orion-guardian orion-core orion-backup-core.timer orion-backup-guardian.timer
systemctl start orion-backup-core.timer orion-backup-guardian.timer

echo "==> firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw --force enable >/dev/null

if grep -q "^OPENROUTER_API_KEY=$" /etc/orion/core.env || grep -q "^INKBOX_API_KEY=$" /etc/orion/guardian.env; then
    cat <<MSG

Almost there. Fill in /etc/orion/core.env, /etc/orion/guardian.env and /etc/orion/orion.toml,
then:

  sudo $ROOT/app/deploy/orion-admin inkbox setup --webhook-url https://$DOMAIN/inkbox/webhook
  # put the signing key it prints in /etc/orion/guardian.env (INKBOX_SIGNING_KEY)
  sudo systemctl restart orion-guardian orion-core
  sudo $ROOT/app/deploy/orion-admin doctor
MSG
else
    systemctl restart orion-guardian orion-core
    echo "==> Orion restarted on https://$DOMAIN"
fi
