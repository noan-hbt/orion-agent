#!/usr/bin/env bash
# Start Orion over from zero, without reinstalling: it forgets, and keeps working.
#
#   sudo ./reset.sh              # Orion forgets everything it lived and learned
#   sudo ./reset.sh --guardian   # ... and the guardian too: approvals, autonomies, logins
#   sudo ./reset.sh --backups    # ... and the nightly backups (else the past stays in them)
#   add --no-archive to keep no copy, --yes to skip the question
#
# Forgotten (/srv/orion/data): the journal (every conversation, everything it did), its memory,
# people, reminders, missions, tasks, library, files, photos, its browser and the sites signed
# in to, its workspaces.
# Kept: the code, orion.toml (its settings, the channels, who may write to it), the secrets
# (/etc/orion and the guardian's secrets.env), the modules installed, the plan logins (ChatGPT,
# Claude Code) and the console's key. Without --guardian, the guardian's ledger (approvals,
# autonomies granted, the sends) and the logins of its vault are kept too.
#
# A copy of everything removed goes first to /root/orion-reset-<date>.tar.gz (root only),
# unless --no-archive. Undo: stop both services, unpack it into /, start them again.
set -euo pipefail

ROOT="${ORION_ROOT:-/srv/orion}"  # ORION_ROOT: a pretend installation, to try the script out
GUARDIAN=0
BACKUPS=0
ARCHIVE_IT=1
YES=0
for arg in "$@"; do
    case "$arg" in
        --guardian) GUARDIAN=1 ;;
        --backups) BACKUPS=1 ;;
        --no-archive) ARCHIVE_IT=0 ;;
        --yes) YES=1 ;;
        *) echo "unknown option: $arg (--guardian, --backups, --no-archive, --yes)"; exit 2 ;;
    esac
done

[ -n "${ORION_ROOT:-}" ] || [ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -d "$ROOT/data" ] || { echo "Orion is not installed here ($ROOT/data is missing)."; exit 1; }

# What survives in data/: installed modules, the plans' logins, the console's key.
KEEP_DATA=(modules chatgpt claude-code console.key)
# What survives in guardian/ with --guardian: the secrets and the admin token.
KEEP_GUARDIAN=(secrets.env admin.token)

ARCHIVE="${ORION_ARCHIVE_DIR:-/root}/orion-reset-$(date +%Y%m%d-%H%M%S).tar.gz"
echo "Orion forgets everything it lived and learned: conversations, memory, reminders,"
echo "missions, files, its browser's sign-ins. Its settings and secrets are kept."
[ "$GUARDIAN" = 1 ] && echo "--guardian: approvals, autonomies granted and saved logins go too."
[ "$BACKUPS" = 1 ] && echo "--backups: the nightly backups go too."
if [ "$ARCHIVE_IT" = 1 ]; then
    echo "A copy is kept first: $ARCHIVE"
else
    echo "--no-archive: no copy is kept."
fi
if [ "$YES" != 1 ]; then
    [ -t 0 ] || { echo "no terminal to confirm: add --yes"; exit 1; }
    read -r -p "Type 'reset' to go on: " answer
    [ "$answer" = "reset" ] || { echo "Nothing done."; exit 1; }
fi

# Everything in a folder but the names given, as paths relative to /.
removable() {
    local folder="$1"; shift
    local entry name keep
    for entry in "$folder"/* "$folder"/.[!.]*; do
        [ -e "$entry" ] || continue
        name="$(basename "$entry")"
        keep=0
        for kept in "$@"; do [ "$name" = "$kept" ] && keep=1; done
        [ "$keep" = 1 ] || echo "${entry#/}"
    done
    return 0
}

echo "==> stopping Orion"
timers_were_on=()
for timer in orion-update.timer orion-backup-core.timer orion-backup-guardian.timer; do
    if systemctl is-active -q "$timer" 2>/dev/null; then
        timers_were_on+=("$timer")
        systemctl stop "$timer"
    fi
done
systemctl stop orion-core orion-guardian 2>/dev/null || true

mapfile -t gone < <(removable "$ROOT/data" "${KEEP_DATA[@]}")
if [ "$GUARDIAN" = 1 ] && [ -d "$ROOT/guardian" ]; then
    mapfile -t -O "${#gone[@]}" gone < <(removable "$ROOT/guardian" "${KEEP_GUARDIAN[@]}")
fi

if [ "$ARCHIVE_IT" = 1 ] && [ "${#gone[@]}" -gt 0 ]; then
    echo "==> archive"
    (umask 077 && tar -C / -czf "$ARCHIVE" "${gone[@]}")
    echo "    $ARCHIVE"
fi

echo "==> forgetting"
for path in "${gone[@]}"; do
    rm -rf "/${path:?}"
done
if [ "$BACKUPS" = 1 ]; then
    rm -rf "$ROOT/backups/core/"* "$ROOT/backups/guardian/"*
fi

echo "==> starting Orion"
systemctl start orion-guardian orion-core
for timer in "${timers_were_on[@]}"; do
    systemctl start "$timer"
done

echo
echo "Orion starts over from zero: write to it, it does not know you any more."
if [ "$ARCHIVE_IT" = 1 ] && [ -f "$ARCHIVE" ]; then
    echo "Undo: sudo systemctl stop orion-core orion-guardian && sudo tar -C / -xzf $ARCHIVE"
    echo "      && sudo systemctl start orion-guardian orion-core"
fi
