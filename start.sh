#!/bin/bash
# Opens the settings in TextEdit, waits for you to finish, then runs the export.
# Installed as "Run Mail Export.command" so it can be double-clicked in Finder.
cd "$(dirname "$0")" || exit 1
export PYTHONUNBUFFERED=1       # keeps the progress messages in step with the log

edit_settings() {
  echo
  echo "Opening the settings in TextEdit."
  echo "  1. Fill in what to look for, following the examples in the file."
  echo "  2. Save with Cmd+S."
  echo "  3. Quit TextEdit with Cmd+Q. The export starts as soon as TextEdit quits."
  echo "If nothing needs changing, just quit TextEdit."
  echo "(Pressing Return in this window also starts the export.)"
  open -W -n -e config.yaml &
  editor=$!
  if [ -t 0 ]; then
    while kill -0 "$editor" 2>/dev/null; do
      if read -r -t 1 _; then break; fi
    done
  else
    wait "$editor"
  fi
}

while :; do
  [ "${MAIL_EXPORT_SKIP_EDIT:-}" = 1 ] || edit_settings
  echo
  echo "Starting the export."
  sh run.sh "$@" 2>&1 | tee last-run.log
  status=${PIPESTATUS[0]}
  [ "$status" -eq 0 ] && break
  echo "The export stopped before finishing. The message above says what to fix."
  echo "A copy of everything above is saved in: $PWD/last-run.log"
  [ -t 0 ] || exit "$status"
  printf "Press Return to open the settings and try again, or close this window to stop. "
  read -r _ || exit "$status"
done

echo
echo "Finished. Everything is in: $PWD"
echo "Results are in the exports folder. To run this again, double-click"
echo "\"Run Mail Export.command\" in that folder."
