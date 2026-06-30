#!/bin/bash
# run.sh — start PIPBOI: the discovery sidecar + the dashboard + the bot.
# Source lives in src/; everything runs with src/ as the working directory so the
# bot, sidecar, dashboard, and tools all agree on where state lives (src/bots,
# src/shared_memory, src/logs). Your .env stays at the repo root.
cd "$(dirname "$0")" || exit 1     # repo root

# First run? Create a minimal .env (at the repo root) so the dashboard can boot,
# then finish setup in the browser at http://localhost:8080 (enter your API keys
# there). Prefer the terminal? Run `python3 src/setup.py` for an interactive wizard.
if [ ! -f .env ]; then
    python3 src/setup.py --bootstrap || exit 1
fi

# Preflight: catch the two things that make boot fail (wrong Python / missing deps)
# and print the one-line fix instead of a scary traceback. Stdlib-only, always runs.
python3 src/doctor.py --preflight || exit 1

# PIPBOI — boot banner (green phosphor)
G=$'\033[1;32m'; D=$'\033[2;32m'; R=$'\033[0m'
printf '\n'
printf '%s   🐸  PIPBOI%s\n' "$G" "$R"
printf '%s      ═══════>   ═══════>   ═══════>%s\n' "$D" "$R"
printf '%s      autonomous solana trading bot%s\n' "$D" "$R"
_PORT=$(grep -E '^DASHBOARD_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
_PORT=${_PORT:-8080}
printf '%s  Dashboard %s→%s http://localhost:%s%s\n\n' "$D" "$G" "$D" "$_PORT" "$R"

cd src || exit 1     # run everything from src/ so all paths resolve consistently

# Kill the entire process group on any exit — Ctrl+C, terminal close, or kill —
# so the bot never runs orphaned when you leave the terminal. SIGHUP is the
# config-reload signal here, so it is forwarded to the bot (not treated as exit).
mkdir -p logs
_DEATHLOG="$PWD/logs/fleet_death.log"
_log_sig() {  # $1 = signal name
    {
        echo "[shutdown] $(date '+%Y-%m-%dT%H:%M:%S%z') caught SIG$1  (run.sh pid=$$ ppid=$PPID)"
        ps -o pid,ppid,stat,etime,command -p "$$" "$PPID" 2>/dev/null | sed 's/^/    /'
    } >> "$_DEATHLOG" 2>&1
}
_shutdown() {
    trap - EXIT INT TERM
    _log_sig "${1:-EXIT}"
    echo ""
    echo "[PIPBOI] shutting everything down (sig ${1:-EXIT})..."
    kill 0
}
trap '_shutdown EXIT' EXIT
trap '_shutdown INT'  INT
trap '_shutdown TERM' TERM
trap '_log_sig HUP-IGNORED; pkill -HUP -f "main.py" 2>/dev/null || true' HUP

# Surface the dashboard PIN (you type it into the setup page to save your keys /
# unlock admin actions) so it's right above the server logs, not scrolled away.
_PIN=$(grep -E '^DASHBOARD_PIN=' ../.env 2>/dev/null | cut -d= -f2 | tr -d ' ')
if [ -n "$_PIN" ]; then
    printf '%s  🔑 Dashboard PIN: %s%s%s  — enter it at http://localhost:%s to save your keys / unlock admin\n\n' \
        "$D" "$G$B" "$_PIN" "$R" "$_PORT" 2>/dev/null || printf '  Dashboard PIN: %s\n\n' "$_PIN"
fi

# Start the discovery sidecar first — one shared polling process feeds the bot.
python3 discovery_service.py &
_SIDECAR_PID=$!
echo "[PIPBOI] Discovery sidecar started (pid $_SIDECAR_PID) — waiting for first snapshot..."
sleep 3

# macOS: caffeinate keeps the machine awake while trading. On Linux it does not
# exist, so fall back to running the dashboard directly.
if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -dims python3 dashboard.py
else
    python3 dashboard.py
fi
