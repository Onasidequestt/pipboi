#!/bin/bash
# telegram/loop.sh — $Vault Telegram bridge supervisor.
#
# Mirrors keepalive.sh / agents/agent_loop.sh: a thin persistent wrapper that keeps
# `bot.py --serve` alive across crashes. --serve already long-polls + posts the
# periodic pulse internally; this layer just restarts it if it ever dies and keeps
# a log. Dies on reboot like the other loops (not a daemon).
#
# Start (durable, survives terminal close):
#   cd ~/solana-trader && nohup ./telegram/loop.sh >> logs/telegram.log 2>&1 &
# Stop / revert the whole Telegram bridge:
#   pkill -f telegram/loop.sh ; pkill -f "telegram/bot.py --serve"
#
# Safe to run alongside ./run.sh and the agent loop — it is READ-ONLY on the fleet.
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

echo "[tec-tg] supervisor started pid $$ at $(date)"
while true; do
  python3 telegram/bot.py --serve
  code=$?
  echo "[tec-tg] bot exited (code $code) at $(date) — restarting in 10s"
  sleep 10
done
