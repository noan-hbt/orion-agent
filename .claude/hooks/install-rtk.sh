#!/usr/bin/env bash
# SessionStart hook: install RTK (Rust Token Killer) in cloud sessions.
# Pinned release, checksum-verified. Fails soft: without rtk, the PreToolUse
# hook is a no-op and commands run unchanged.
set -euo pipefail

VERSION="v0.49.0"
SHA256="7278231dfd7e6a730a4ab7f847b195bcf02289c2d57622b0dab75a6411100c8f"
ASSET="rtk-x86_64-unknown-linux-musl.tar.gz"
BIN_DIR="$HOME/.local/bin"

if command -v rtk >/dev/null 2>&1 || [ -x "$BIN_DIR/rtk" ]; then
  exit 0
fi
if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
  echo "install-rtk: unsupported platform $(uname -s)-$(uname -m), skipping" >&2
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if ! curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/$VERSION/$ASSET" -o "$tmp/$ASSET"; then
  echo "install-rtk: download failed, rtk disabled for this session" >&2
  exit 0
fi
echo "$SHA256  $tmp/$ASSET" | sha256sum -c --quiet - || {
  echo "install-rtk: checksum mismatch, refusing to install" >&2
  exit 0
}
tar -xzf "$tmp/$ASSET" -C "$tmp"
mkdir -p "$BIN_DIR"
install -m 0755 "$tmp/rtk" "$BIN_DIR/rtk"
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
echo "install-rtk: installed rtk $VERSION to $BIN_DIR" >&2
