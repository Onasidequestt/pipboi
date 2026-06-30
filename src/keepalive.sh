#!/bin/bash
# keepalive.sh — S66 evidence-engine supervisor.
# Respawns the two evolution loops if (and only if) they die, so the 24h
# data-span clock that unlocks wire-to-live cannot silently stall on a
# transient crash. Duplicate-safe: each pgrep matches the *persistent*
# process (signal_lab daemon; the brain's `while` bash wrapper carries the
# "strategy_brain.py --evolve" string even during its 3600s sleep), so a
# respawn only fires when the loop is genuinely gone.
#
# Survives process death, NOT reboot/logout (use the launchd plists in
# deploy/ for that). Reversible: pkill -f keepalive.sh
# Start: cd /path/to/pipboi && nohup ./keepalive.sh >>logs/keepalive.log 2>&1 &
cd "$(dirname "$0")" || exit 1
mkdir -p logs
echo "[keepalive] started pid $$ at $(date)"
while true; do
  if ! pgrep -f "signal_lab.py --log" >/dev/null; then
    echo "[keepalive] $(date) signal_lab dead → respawning"
    nohup python3 signal_lab.py --log >>logs/signal_lab.log 2>&1 &
  fi
  if ! pgrep -f "strategy_brain.py --evolve" >/dev/null; then
    echo "[keepalive] $(date) brain loop dead → respawning"
    nohup bash -c 'while true; do echo "=== $(date) ==="; python3 strategy_brain.py --evolve --horizon 30; echo; sleep 3600; done' >>logs/brain_loop.log 2>&1 &
  fi
  sleep 120
done
