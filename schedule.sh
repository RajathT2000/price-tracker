#!/bin/bash
# Runs `tracker.py check` every hour via launchd (each product is checked on its own hour/day interval).
# Usage: ./schedule.sh on | off
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/local.price-tracker.plist"
if [ "$1" = "off" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; echo "schedule off"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.price-tracker</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/tracker.py</string><string>check</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/run.log</string>
  <key>StandardErrorPath</key><string>$DIR/run.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "schedule on (hourly): $PLIST"
