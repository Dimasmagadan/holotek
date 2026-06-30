#!/bin/bash
# Build "Holotek Launcher.app" — a double-clickable Finder app that starts the
# menu-bar process detached and then exits. Regenerate any time with: ./make-launcher.sh
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
APP="$REPO/Holotek Launcher.app"
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script \"$REPO/start.sh\""
echo "Built: $APP"
echo "Double-click it in Finder to start Holotek."
