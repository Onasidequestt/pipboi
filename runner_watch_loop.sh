#!/usr/bin/env bash
# runner_watch_loop.sh — durable watcher for the RUNNEREDGE trial (S119).
# Runs runner_watch.py on a 30-min interval; it is READ-ONLY (opens bots/bot1/trades.db
# with ?mode=ro, writes only runner_watch_log.jsonl) and NEVER acts — it prints a
# pre-registered, fail-closed verdict (SCALE-CANDIDATE / KILL / PENDING) and the
# operator-suggested action, nothing more. Matches the gene_arm_loop.sh / watchdog_loop.sh
# nohup idiom. NOT KeepAlive-critical: dies on reboot, restart with:
#   cd ~/solana-trader && nohup ./runner_watch_loop.sh >logs/runner_watch_loop.log 2>&1 &
# Stop: pkill -f runner_watch_loop.sh
cd "$(dirname "$0")" || exit 1
INTERVAL="${1:-1800}"   # seconds between checks (default 30 min)
mkdir -p logs
echo "[runner-watch] loop start pid=$$ interval=${INTERVAL}s $(date -u +%FT%TZ)"
while true; do
  python3 runner_watch.py 2>&1 | sed 's/^/[runner-watch] /'
  sleep "$INTERVAL"
done
