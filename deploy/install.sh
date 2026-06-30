#!/bin/bash
# install.sh — OPTIONAL: install the macOS LaunchAgents for reboot-durable uptime.
#
# This is for people who want PIPBOI to survive reboots/crashes. It is NOT required —
# you can always just run `./run.sh` (or `./pipboi run`) in a terminal.
#
# What it does: fills in this repo's real path into the plist templates and loads them
# as per-user LaunchAgents. No sudo, no system-wide changes — everything lives under
# ~/Library/LaunchAgents and runs as you.
#
#   ./deploy/install.sh            # install + start all agents
#   ./deploy/install.sh fleet      # just the main fleet agent
#   ./deploy/uninstall.sh          # remove them all
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # repo root
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$ROOT/src/logs"

# Only one evolve/optimizer loop should run — stop any manual ones first.
pkill -f "strategy_brain.py --evolve" 2>/dev/null || true

want="${1:-all}"
for tmpl in "$ROOT"/deploy/com.pipboi.*.plist; do
    label="$(basename "$tmpl" .plist)"            # com.pipboi.<name>
    name="${label##*.}"
    if [ "$want" != "all" ] && [ "$want" != "$name" ]; then continue; fi
    dest="$LA/$label.plist"
    sed "s#__PIPBOI_DIR__#$ROOT#g" "$tmpl" > "$dest"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dest"
    launchctl enable "gui/$(id -u)/$label"
    echo "✓ installed $label  →  $dest"
done

echo
echo "Done. Check status:   launchctl list | grep com.pipboi"
echo "Dashboard:            http://localhost:8080"
