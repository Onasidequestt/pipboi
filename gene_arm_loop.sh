#!/usr/bin/env bash
# gene_arm_loop.sh — durable watcher for the PRESTIGE RACE (gen 1).
# Runs arm_genes.py --arm on an interval; it stays DORMANT (writes nothing) until the
# deep_pool DEPLOY GATE passes (net≥0, ghost≤10%, n≥15), then activates the three size
# genes (Bot1 full / Bot2 moderate / Bot3 control) and self-heals if a file is removed.
# Survives a fleet ./run.sh (different process name). Dies on reboot — restart with:
#   cd ~/solana-trader && nohup ./gene_arm_loop.sh >logs/gene_arm_loop.log 2>&1 &
# Stop: pkill -f gene_arm_loop.sh
cd "$(dirname "$0")" || exit 1
INTERVAL="${1:-600}"   # seconds between checks (default 10 min)
mkdir -p logs
echo "[gene-arm] loop start pid=$$ interval=${INTERVAL}s $(date -u +%FT%TZ)"
while true; do
  python3 arm_genes.py --arm 2>&1 | sed 's/^/[gene-arm] /'
  sleep "$INTERVAL"
done
