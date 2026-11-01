#!/usr/bin/env bash
# DZCT/kontinentAi — interactive fresh installer for Ubuntu 22.04+.
set -Eeuo pipefail

REPOSITORY='DZCT/kontinentAi'
BRANCH='main'
ARCHIVE_URL="https://codeload.github.com/${REPOSITORY}/tar.gz/refs/heads/${BRANCH}"

fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
command -v apt-get >/dev/null || fail 'Ubuntu 22.04 or newer is required.'
command -v systemctl >/dev/null || fail 'systemd is required.'
# Reattach interactive input even if the script was fetched through a pipe.
if ! { true </dev/tty; } 2>/dev/null; then
    fail 'Open an interactive SSH session (for example Termius) and try again.'
fi
PRIV=()
if [[ "$EUID" -ne 0 ]]; then
    command -v sudo >/dev/null || fail 'Run as root or install sudo.'
    sudo -v
    PRIV=(sudo)
fi
if "${PRIV[@]}" test -e /opt/kontinent-bot || "${PRIV[@]}" test -e /etc/systemd/system/kontinent-bot.service; then
    fail 'A bot is already installed. Nothing was changed. This command is for a NEW installation.'
fi
printf '\n✦ DZCT · kontinentAi\nInstalling a new personal Telegram bot.\n\n'
"${PRIV[@]}" apt-get update
"${PRIV[@]}" apt-get install -y python3 curl ca-certificates tar

WORK_DIR="$(mktemp -d -t kontinentAi-install-XXXXXXXX)"
cleanup() { rm -rf -- "$WORK_DIR"; }
trap cleanup EXIT
trap 'printf "\nInstallation stopped. Keep the error output; do not delete existing bot data.\n" >&2' ERR
curl --fail --show-error --silent --location --connect-timeout 20 --max-time 180 \
    "$ARCHIVE_URL" --output "$WORK_DIR/source.tar.gz"
mkdir "$WORK_DIR/source"
tar -xzf "$WORK_DIR/source.tar.gz" -C "$WORK_DIR/source" --strip-components=1
[[ -f "$WORK_DIR/source/install.py" ]] || fail 'The GitHub repository does not contain install.py at its root.'
"${PRIV[@]}" python3 "$WORK_DIR/source/install.py" </dev/tty
