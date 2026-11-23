#!/bin/bash
# Installs the email export into ~/mail-export and starts it. In Terminal, paste:
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/markgbrisbane/proton-clerk/main/install.sh)"
#
# Running it again updates the script and keeps your settings, your Bridge
# login, the downloaded mail and earlier exports.
set -euo pipefail

REF="361c8523edcb5be15af8b090c98eec49f22335e9"       # the exact version of the files this installer fetches
SOURCE="${MAIL_EXPORT_SOURCE:-https://github.com/markgbrisbane/proton-clerk/archive/$REF.tar.gz}"
DEST="$HOME/mail-export"

stop() {
  printf '\nSTOPPED: %s\n\n' "$1" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || stop "This only runs on a Mac."

if [ ! -d "/Applications/Proton Mail Bridge.app" ] && [ ! -d "$HOME/Applications/Proton Mail Bridge.app" ]; then
  stop "Proton Mail Bridge isn't installed. Get it from https://proton.me/mail/bridge,
sign in, wait for it to finish syncing, then paste the command again."
fi

browser=""
for app in "Google Chrome" "Chromium" "Microsoft Edge" "Brave Browser" "Vivaldi"; do
  if [ -d "/Applications/$app.app" ] || [ -d "$HOME/Applications/$app.app" ]; then
    browser="$app"
    break
  fi
done
[ -n "$browser" ] || stop "Google Chrome isn't installed. It is used to make the PDFs.
Get it from https://www.google.com/chrome/, then paste the command again."

echo "Downloading the email export..."
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL "$SOURCE" | tar -xz -C "$work" --strip-components=1 \
  || stop "The download failed. Check the internet connection and paste the command again."
for file in mail_export.py run.sh start.sh README.md config.yaml; do
  [ -f "$work/$file" ] || stop "The download was incomplete ($file is missing). Paste the command again."
done

mkdir -p "$DEST"
chmod 700 "$DEST"
/bin/cp -f "$work/mail_export.py" "$work/run.sh" "$work/README.md" "$DEST/"
/bin/cp -f "$work/start.sh" "$DEST/Run Mail Export.command"
chmod +x "$DEST/Run Mail Export.command"
if [ -f "$DEST/config.yaml" ]; then
  /bin/cp -f "$work/config.yaml" "$DEST/config.example.yaml"
  echo "Kept your existing settings (config.yaml)."
else
  /bin/cp -f "$work/config.yaml" "$DEST/config.yaml"
fi
echo "Installed in $DEST"

# Bridge has to be open for the export to reach the mailbox.
port="$(sed -n 's/^ *imap_port: *\([0-9][0-9]*\).*/\1/p' "$DEST/config.yaml" | head -1)"
port="${port:-1143}"
if ! nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
  echo "Opening Proton Mail Bridge..."
  open -a "Proton Mail Bridge" || true
  for _ in $(seq 1 30); do
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && break
    sleep 2
  done
fi
echo "If Proton Mail Bridge is still syncing, let it finish before you carry on."

[ "${MAIL_EXPORT_NO_START:-}" = 1 ] && exit 0
exec /bin/bash "$DEST/Run Mail Export.command"
