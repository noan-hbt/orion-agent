#!/usr/bin/env bash
# Install Orion on a fresh Debian 12 / Ubuntu 24.04 server (an OVH VPS, for instance).
#
#   sudo ./install.sh [domain]
#
# Without a domain, the server's IP is used through sslip.io (e.g. 51-83-1-2.sslip.io), which is
# enough for Caddy to get a certificate. The first time, it asks a few questions (`orion
# setup`: the keys, checked, and who you are); after that, Orion changes its settings itself,
# by message. Safe to run again: it updates the code and Caddy, never overwrites orion.toml or
# the secret files (it only adds sections that came later, like the app's).
set -euo pipefail

REPO="${ORION_REPO:-https://github.com/noan-hbt/orion-agent.git}"
BRANCH="${ORION_BRANCH:-main}"
ROOT=/srv/orion
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    ip="$(curl -fsS4 --max-time 10 https://api.ipify.org || curl -fsS4 --max-time 10 https://ifconfig.me || true)"
    if ! [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "could not find this server's public IPv4 address: give a domain instead,"
        echo "  sudo ./install.sh orion.example.com"
        exit 1
    fi
    DOMAIN="${ip//./-}.sslip.io"
fi
echo "==> domain: $DOMAIN"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq git curl sqlite3 caddy ufw ca-certificates ffmpeg >/dev/null
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
install -d -m 0755 "$ROOT/python"  # stays empty when the system Python is recent enough
uv sync --frozen --no-dev -q
# Orion's own browser ([modules.computer]): Chromium and the libraries it needs.
PLAYWRIGHT_BROWSERS_PATH="$ROOT/browsers" .venv/bin/playwright install --with-deps chromium >/dev/null
chmod -R a+rX "$ROOT/app" "$ROOT/python" "$ROOT/browsers"

echo "==> folders"
install -d -o orion-core -g orion -m 0750 "$ROOT/data" "$ROOT/backups/core"
install -d -o orion-guardian -g orion -m 0700 "$ROOT/guardian" "$ROOT/backups/guardian"
install -d -o orion-guardian -g orion -m 0750 "$ROOT/run"
install -d -m 0755 /etc/orion
# orion.toml is written by the guardian when Orion changes its settings by message (D92): its
# folder is the guardian's, the core reads it through the group.
install -d -o orion-guardian -g orion -m 0750 "$ROOT/config"
CONFIG="$ROOT/config/orion.toml"
if [ ! -f "$CONFIG" ]; then
    if [ -f /etc/orion/orion.toml ]; then  # an earlier install kept it there
        cp /etc/orion/orion.toml "$CONFIG"
        mv /etc/orion/orion.toml /etc/orion/orion.toml.moved
    else
        sed "s/@DOMAIN@/$DOMAIN/g" "$HERE/orion.production.toml" > "$CONFIG"
    fi
fi
"$HERE/add-app" "$CONFIG" "$DOMAIN"
chown orion-guardian:orion "$CONFIG"
chmod 0640 "$CONFIG"
ln -sfn "$CONFIG" "$ROOT/orion.toml"
for who in guardian core; do
    if [ ! -f "/etc/orion/$who.env" ]; then
        install -m 0600 -o "orion-$who" -g orion "$HERE/$who.env.example" "/etc/orion/$who.env"
    fi
done

if grep -q "^OPENROUTER_API_KEY=$" /etc/orion/core.env && [ -t 0 ]; then
    echo "==> setup"
    "$ROOT/app/.venv/bin/orion" --config "$CONFIG" setup --server --secrets /etc/orion
    for who in guardian core; do
        chown "orion-$who:orion" "/etc/orion/$who.env"
        chmod 0600 "/etc/orion/$who.env"
    done
fi
# Everything in config/ is the guardian's, the copy `orion setup` keeps beside it too (D148).
chown -R orion-guardian:orion "$ROOT/config"
chmod 0640 "$ROOT"/config/orion.toml*

echo "==> services"
install -m 0644 "$HERE"/orion-*.service "$HERE"/orion-*.timer /etc/systemd/system/
sed "s/@DOMAIN@/$DOMAIN/g" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl reload-or-restart caddy
systemctl enable -q orion-guardian orion-core orion-backup-core.timer orion-backup-guardian.timer \
    orion-update.timer
# From now on Orion follows its branch by itself, and goes back if a new version fails (D94).
systemctl start orion-backup-core.timer orion-backup-guardian.timer orion-update.timer

echo "==> firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw --force enable >/dev/null

if grep -q "^OPENROUTER_API_KEY=$" /etc/orion/core.env; then
    cat <<MSG

Almost there: run the questions again, from a terminal on the server:

  sudo $ROOT/app/deploy/install.sh $DOMAIN
MSG
else
    systemctl restart orion-guardian orion-core
    echo "==> Orion restarted on https://$DOMAIN"
    # The first phone's code, shown here: after it, everything is done from the app (D154).
    for _ in $(seq 1 30); do
        if devices=$("$HERE/orion-admin" app devices 2>/dev/null); then
            case "$devices" in
                Aucun*) echo "==> your phone"; "$HERE/orion-admin" app pair iphone ;;
            esac
            break
        fi
        sleep 1
    done
fi
