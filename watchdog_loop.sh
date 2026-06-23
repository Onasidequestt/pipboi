#!/usr/bin/env bash
# watchdog_loop.sh — run fleet_watchdog.py every 6h (nohup-friendly while-loop).
#
# Matches the existing signal_lab / strategy_brain nohup-loop pattern. This gives
# immediate recurring coverage in the CURRENT session; it DIES on reboot/logout
# (it survives while the fleet's `caffeinate` keeps the Mac awake). For coverage
# that survives a reboot, also install deploy/com.vaultbot.watchdog.plist.
#
#   Start (background, detached):
#     cd ~/solana-trader
#     nohup ./watchdog_loop.sh >logs/watchdog_loop.log 2>&1 &
#
#   Stop:
#     pkill -f watchdog_loop.sh
#
# fleet_watchdog.py holds a single-instance lock, so this loop and the launchd
# agent cannot double-run / double-sell if both are active.

cd "$(dirname "$0")" || exit 1
INTERVAL="${WATCHDOG_INTERVAL_S:-21600}"   # 6h default; override via env for testing

echo "[watchdog-loop] started pid $$ — interval ${INTERVAL}s"
while true; do
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') watchdog tick ==="
  python3 fleet_watchdog.py
  sleep "$INTERVAL"
done
