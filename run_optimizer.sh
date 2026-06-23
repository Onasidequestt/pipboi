#!/usr/bin/env bash
# run_optimizer.sh — derive optimal thresholds from realized trade data and apply them.
#
# What this does:
#   1. Runs goldilocks.py --emit-override  → writes thresholds_override.json atomically.
#      Live bots reload this file automatically every 20 closes via config_manager.reload()
#      + apply_overrides(). On the next restart, apply_overrides() is called at startup,
#      so the values survive a clean reboot without any other action.
#
#   2. (Optional) Signals live bots via SIGHUP to reload immediately instead of waiting
#      for the next 20-close trigger. Uncomment the kill line below to enable.
#
# When to run:
#   - Manually after each ~20-trade batch to incorporate new data.
#   - Or via cron: add to crontab with `crontab -e`
#     Example — run at 6 AM and 6 PM daily:
#       0 6,18 * * * cd ~/solana-trader && ./run_optimizer.sh >> logs/goldilocks_cron.log 2>&1
#
# What it does NOT do:
#   - It does NOT restart the bots. thresholds_override.json is hot-reloaded in-process.
#   - It does NOT touch TP/SL for INSANE tiers (too few tagged closes — runs at defaults).
#   - GEM_MIN_VOLUME_5M, momentum floor, and WILD/STOIC TP/SL are all carried in
#     thresholds_override.json and applied live via apply_overrides() — no source patching.

set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs
LOG="logs/goldilocks_$(date +%Y%m%d_%H%M%S).txt"

echo "════════════════════════════════════════"
echo "  GOLDILOCKS OPTIMIZER  $(date)"
echo "════════════════════════════════════════"

python3 goldilocks.py --emit-override 2>&1 | tee "$LOG"

# Signal running bots to reload thresholds immediately (instead of waiting for the next
# 20-close trigger). Uses bot_pids.json — the bots run as ".../Python main.py", so the
# old `pgrep -f "python3 main.py"` never matched. SIGHUP only reloads config; it is safe.
if [ -f bot_pids.json ]; then
  HUP_PIDS=$(python3 -c "import json;print(' '.join(str(v) for v in json.load(open('bot_pids.json')).values()))" 2>/dev/null || true)
  [ -n "$HUP_PIDS" ] && kill -HUP $HUP_PIDS 2>/dev/null || true
fi
pkill -HUP -f "main.py" 2>/dev/null || true   # fallback if bot_pids.json is stale

echo ""
echo "  Log saved → $LOG"
echo "════════════════════════════════════════"
