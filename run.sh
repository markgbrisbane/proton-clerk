#!/bin/sh
# Runs the email export. In Terminal:   sh run.sh
#
# The script needs one helper, "uv", which fetches the right Python and the
# one library the script uses. If uv isn't on this Mac it is downloaded into
# this folder (.tools) and nothing else on the system is changed.
set -e
cd "$(dirname "$0")"

UV=""
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
elif [ -x ".tools/uv" ]; then
  UV=".tools/uv"
else
  echo "One-time setup: downloading the 'uv' helper into this folder..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$PWD/.tools" sh
  UV=".tools/uv"
  echo "The first run also downloads Python, which can take a minute."
fi

exec "$UV" run mail_export.py "$@"
