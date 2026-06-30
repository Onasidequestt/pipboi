#!/bin/bash
# Guarded launcher for the com.pipboi.fleet LaunchAgent (optional reboot-durable uptime).
#
# Why a guard: there is ONE port-8080 dashboard. If launchd starts run.sh while a manual
# `./run.sh` is already running, two dashboards fight for 8080. launchd runs at most one
# instance per Label, so the only collision is launchd-vs-manual. This refuses to launch a
# second fleet when 8080 is already bound, logs it, and exits so KeepAlive re-checks ~every
# 60s — converging (it starts the moment the manual fleet stops) with no tight respawn loop.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # repo root (this script lives in deploy/)
cd "$ROOT" || exit 1
mkdir -p src/logs
LOG="$ROOT/src/logs/fleet_launchd.log"

if lsof -ti tcp:8080 >/dev/null 2>&1; then
  echo "[fleet_launchd] $(date) — port 8080 already bound; a fleet is running. Not starting a second." >> "$LOG"
  exit 0
fi

echo "[fleet_launchd] $(date) — port 8080 free → starting the fleet via run.sh" >> "$LOG"
exec ./run.sh >> "src/logs/run_$(date +%s).log" 2>&1
