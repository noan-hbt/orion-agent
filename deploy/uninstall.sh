#!/usr/bin/env bash
# Remove Orion from the server: what install.sh set up, and nothing else.
#
#   sudo ./uninstall.sh            # keeps an archive of your data in /root, then removes Orion
#   sudo ./uninstall.sh --purge    # removes everything, no archive: Orion's memory is gone
#   add --yes to skip the question
#
# Removed: the services and timers, the code, Python and the browser, the data, the secrets,
# the users and their group, Orion's Caddy site (Caddy gets its default page back).
# Kept: the packages (Caddy, uv, ffmpeg, sqlite3: other things may use them) and the firewall
# rules (SSH, 80 and 443 stay open). Without --purge, the archive holds the journal (your life
# with Orion), the guardian's ledger and logins, the config and the secrets: keep it safe, or
# delete it.
set -euo pipefail

ROOT="${ORION_ROOT:-/srv/orion}"  # ORION_ROOT and ORION_ETC: a pretend installation, to try
ETC="${ORION_ETC:-/etc}"          # the script out; its users are then left alone
PURGE=0
YES=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        *) echo "unknown option: $arg (--purge, --yes)"; exit 2 ;;
    esac
done

[ -n "${ORION_ROOT:-}" ] || [ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

if [ ! -d "$ROOT" ] && [ ! -d "$ETC/orion" ] && ! compgen -G "$ETC/systemd/system/orion-*" >/dev/null; then
    echo "Orion is not installed here."
    exit 0
fi

ARCHIVE="${ORION_ARCHIVE_DIR:-/root}/orion-$(date +%Y%m%d-%H%M%S).tar.gz"
echo "This removes Orion from this server: services, code, data, secrets, users."
if [ "$PURGE" = 1 ]; then
    echo "--purge: no archive is kept. Its memory, your logins and its secrets are gone for good."
else
    echo "An archive of the data, config and secrets is kept first: $ARCHIVE"
fi
if [ "$YES" != 1 ]; then
    [ -t 0 ] || { echo "no terminal to confirm: add --yes"; exit 1; }
    read -r -p "Type 'uninstall' to go on: " answer
    [ "$answer" = "uninstall" ] || { echo "Nothing done."; exit 1; }
fi

echo "==> services"
units=()
for path in "$ETC"/systemd/system/orion-*.service "$ETC"/systemd/system/orion-*.timer; do
    [ -e "$path" ] && units+=("$(basename "$path")")
done
for unit in "${units[@]}"; do
    systemctl disable --now -q "$unit" 2>/dev/null || true
done
for unit in "${units[@]}"; do
    rm -f "$ETC/systemd/system/$unit"
done
systemctl daemon-reload
systemctl reset-failed 'orion-*' 2>/dev/null || true

if [ "$PURGE" != 1 ]; then
    echo "==> archive"
    kept=()
    for path in "$ROOT/data" "$ROOT/guardian" "$ROOT/config" "$ROOT/backups" "$ETC/orion"; do
        [ -e "$path" ] && kept+=("${path#/}")
    done
    if [ "${#kept[@]}" -gt 0 ]; then
        (umask 077 && tar -C / -czf "$ARCHIVE" "${kept[@]}")
        echo "    $ARCHIVE"
    fi
fi

echo "==> Caddy"
if [ -f "$ETC/caddy/Caddyfile" ] && grep -q "Orion's public face" "$ETC/caddy/Caddyfile"; then
    cat > "$ETC/caddy/Caddyfile" <<'CADDY'
# Caddy's default site (Orion was removed).
:80 {
	root * /usr/share/caddy
	file_server
}
CADDY
    systemctl reload-or-restart caddy 2>/dev/null || true
fi

echo "==> files"
rm -rf "$ROOT" "$ETC/orion"

echo "==> users"
for user in orion-core orion-guardian; do
    [ -z "${ORION_ROOT:-}" ] || break  # a pretend installation has none of its own
    if id "$user" >/dev/null 2>&1; then
        pkill -u "$user" 2>/dev/null || true
        userdel "$user"
    fi
done
[ -z "${ORION_ROOT:-}" ] && getent group orion >/dev/null && groupdel orion

echo
echo "Orion is uninstalled."
if [ "$PURGE" != 1 ] && [ -f "$ARCHIVE" ]; then
    echo "Your data is in $ARCHIVE (root only). To install again with it, unpack it into /"
    echo "after install.sh: sudo tar -C / -xzf $ARCHIVE, then restart orion-guardian and orion-core."
fi
echo "Left in place: Caddy, uv, ffmpeg, sqlite3 and the firewall rules (SSH, 80, 443)."
