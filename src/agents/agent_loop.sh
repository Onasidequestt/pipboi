#!/bin/bash
# agent_loop.sh — reasoning-agent heartbeat supervisor (paperclip prototype).
#
# Mirrors keepalive.sh: a single persistent loop that fires each agent on its own
# cadence. This is the "heartbeat" layer paperclip would otherwise own — kept as a
# shell loop so the prototype needs ZERO new infrastructure (no Node, no DB, no server).
#
# The agents are ADVISORY: they only write shared_memory/agent_*.json + agents/history/.
# They touch nothing the live fleet reads. Safe to run alongside ./run.sh.
#
# Survives process death is NOT handled here (this IS the supervisor); dies on reboot
# like the other loops. Reversible: pkill -f agent_loop.sh
# Start:  cd /path/to/pipboi && nohup ./agents/agent_loop.sh >>logs/agents.log 2>&1 &
#
# Cadence overridable via env: STRATEGY_INTERVAL / EXEC_INTERVAL (seconds).
# Backend overridable via env: AGENT_BACKEND (auto|cli|api|dry-run) — e.g. run the whole
# loop in dry-run to smoke-test scheduling with zero token spend. AGENT_MODEL pins the model.
cd "$(dirname "$0")/.." || exit 1
export PATH="$HOME/.local/bin:$PATH"      # claude CLI lives here (native install)
mkdir -p logs agents/history

STRATEGY_INTERVAL="${STRATEGY_INTERVAL:-3600}"     # hourly
EXEC_INTERVAL="${EXEC_INTERVAL:-10800}"            # every 3h
BACKEND="${AGENT_BACKEND:-auto}"
TICK=60

echo "[agents] started pid $$ at $(date) — strategy/${STRATEGY_INTERVAL}s exec/${EXEC_INTERVAL}s backend=${BACKEND}"

last_strategy=0
last_exec=0
while true; do
  now=$(date +%s)
  if (( now - last_strategy >= STRATEGY_INTERVAL )); then
    echo "[agents] $(date) → strategy heartbeat"
    python3 agents/agent_runner.py strategy --backend "$BACKEND"
    last_strategy=$now
  fi
  if (( now - last_exec >= EXEC_INTERVAL )); then
    echo "[agents] $(date) → execution heartbeat"
    python3 agents/agent_runner.py execution --backend "$BACKEND"
    last_exec=$now
  fi
  sleep "$TICK"
done
