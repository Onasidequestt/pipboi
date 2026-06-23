#!/bin/bash
# Guarded fleet launcher for the com.vaultbot.fleet LaunchAgent (uptime hardening).
#
# Why a guard: the fleet has ONE port-8080 dashboard, and run.sh's own `kill 0` only tears down
# ITS process group. If this agent ever starts run.sh while a MANUAL `nohup ./run.sh` is already
# running, you get two dashboards fighting for 8080 + a duplicate sidecar. launchd already runs at
# most ONE instance per Label, so the only collision is launchd-vs-manual. This guard refuses to
# launch a second fleet when port 8080 is already bound, logs it, and exits so KeepAlive + the
# plist's ThrottleInterval re-check ~every 60s — it converges (launches the moment the manual
# fleet stops) with no tight respawn loop.
#
# Pairs with: dashboard.py `_startup() → _kill_orphans()` (kills stale bots on its own boot) and
# run.sh's `kill 0` trap (group teardown). Together: at most one fleet, auto-recovered on death,
# auto-started on login/reboot via the plist's RunAtLoad.
set -u
ROOT="/Users/onasidequest/solana-trader"
cd "$ROOT" || exit 1
mkdir -p logs
LOG="$ROOT/logs/fleet_launchd.log"

# Already a fleet on 8080? Don't double-launch — let launchd retry on the throttle interval.
if lsof -ti tcp:8080 >/dev/null 2>&1; then
  echo "[fleet_launchd] $(date) — port 8080 already bound; a fleet is running. Not starting a second." >> "$LOG"
  exit 0
fi

echo "[fleet_launchd] $(date) — port 8080 free → starting the fleet via run.sh" >> "$LOG"
exec ./run.sh >> "logs/run_$(date +%s).log" 2>&1
