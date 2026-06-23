#!/bin/bash
cd "$(dirname "$0")"

# First run? Create a minimal config so the dashboard can boot, then finish setup
# in the browser at http://localhost:8080 (enter your API keys there). Prefer the
# terminal? Run `python3 setup.py` instead for an interactive wizard.
if [ ! -f .env ]; then
    python3 setup.py --bootstrap || exit 1
fi

# $Vault — boot banner (green phosphor)
G=$'\033[1;32m'; D=$'\033[2;32m'; R=$'\033[0m'
printf '\n'
printf '%s   ⚙  $VAULT%s\n' "$G" "$R"
printf '%s      ═══════>   ═══════>   ═══════>%s\n' "$D" "$R"
printf '%s      autonomous solana trading fleet%s\n' "$D" "$R"
_PORT=$(grep -E '^DASHBOARD_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
_PORT=${_PORT:-8080}
printf '%s  Dashboard %s→%s http://localhost:%s%s\n\n' "$D" "$G" "$D" "$_PORT" "$R"

# Kill the entire process group on any exit — Ctrl+C, terminal close, or kill.
# This guarantees the bots never run orphaned when you leave the terminal.
#
# ★ FLEETDEATH (2026-06-11): debug "the fleet keeps dying ~every 24-52min". The dashboard logs a
# graceful uvicorn "Shutting down" = it caught SIGTERM. `kill 0` here sends SIGTERM to the whole
# group, so the death chain is: run.sh catches a signal -> kill 0 -> dashboard SIGTERM -> teardown
# -> KeepAlive respawn. Two changes: (1) LOG the exact signal+parent to logs/fleet_death.log so the
# next death is identified; (2) HUP no longer tears down -- SIGHUP is the config-RELOAD signal in
# this system (config_manager._sighup_handler in the bots; run_optimizer.sh does `kill -HUP`), and
# under launchd there is NO controlling terminal, so a stray HUP to run.sh/its group must NOT kill
# the fleet. Keep EXIT/INT/TERM teardown (genuine terminal-close / manual kill). Revert: restore
# run.sh.bak.fleetdeath.* .
_DEATHLOG="$(cd "$(dirname "$0")" && pwd)/logs/fleet_death.log"
_log_sig() {  # $1 = signal name
    {
        echo "[fleet_death] $(date '+%Y-%m-%dT%H:%M:%S%z') caught SIG$1  (run.sh pid=$$ ppid=$PPID)"
        ps -o pid,ppid,stat,etime,command -p "$$" "$PPID" 2>/dev/null | sed 's/^/    /'
    } >> "$_DEATHLOG" 2>&1
}
_shutdown() {
    trap - EXIT INT TERM  # remove teardown traps first to prevent re-entry
    _log_sig "${1:-EXIT}"
    echo ""
    echo "[Fleet] shutting everything down (sig ${1:-EXIT})..."
    kill 0  # SIGTERM to every process in this process group (dashboard + all bots)
}
trap '_shutdown EXIT' EXIT
trap '_shutdown INT'  INT
trap '_shutdown TERM' TERM
# SIGHUP -> log + IGNORE the teardown; forward it to the BOTS only so config still hot-reloads.
trap '_log_sig HUP-IGNORED; pkill -HUP -f "main.py" 2>/dev/null || true' HUP

# Start the discovery sidecar first — one shared polling process for all 3 bots.
# Bots fall back to standalone polling if this fails to start.
python3 discovery_service.py &
_SIDECAR_PID=$!
echo "[Fleet] Discovery sidecar started (pid $_SIDECAR_PID) — waiting for first snapshot..."
sleep 3   # give the sidecar time to publish its first snapshot before bots connect

caffeinate -dims python3 dashboard.py
