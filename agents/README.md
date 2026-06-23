# Reasoning Agents — paperclip prototype (2 high-ROI agents)

A lightweight prototype of the multi-agent layer, mirroring the existing shell-loop heartbeat
pattern (`keepalive.sh`) so it needs **zero new infrastructure** — no Node, no DB, no paperclip
server yet. Two agents that automate the highest-judgment operator work:

| Agent | Cadence | Owns | Tools it reads (read-only) |
|---|---|---|---|
| **Strategy Researcher** | hourly | which brain RULES are strengthening / ready for live admission | `strategy_brain.py --evaluate`, `strategy_brain.json`, forward-obs digest |
| **Execution Engineer** | every 3h | the paper→live leak (ghosts, exec failures, exit rails) | `deep_pool_audit.py`, `edge_report.py --by-play` |

## The safety boundary (why this can't hurt the fleet)
The agents are **advisory only**. `agent_runner.py` writes **only** to
`shared_memory/agent_*.json` and `agents/history/`. It never writes a `bots/botN/*` canary,
never arms genes, never runs a mutating command, never touches `trades.db`. The agents PROPOSE;
the operator / Risk Governor dispose. This enforces THE ONE OPERATOR RULE structurally — an agent
*cannot* size up an unproven edge because it has no write path to do so.

## Run it
```bash
cd ~/solana-trader

# One heartbeat, inspect-only (no LLM, no token spend) — writes the exact prompt to disk:
python3 agents/agent_runner.py strategy  --dry-run
python3 agents/agent_runner.py execution --dry-run
cat agents/history/strategy.last_prompt.txt     # review what the agent would reason over

# Go live — pick ONE backend:
#   (a) API:  pip install anthropic && export ANTHROPIC_API_KEY=sk-...
#   (b) CLI:  install the `claude` CLI (paperclip's claude_local adapter uses this)
python3 agents/agent_runner.py strategy          # auto-detects backend
cat shared_memory/agent_strategy.json            # the structured brief

# Continuous heartbeat loop (mirrors keepalive.sh; dies on reboot, not on terminal close):
nohup ./agents/agent_loop.sh >>logs/agents.log 2>&1 &
# cadence override:  STRATEGY_INTERVAL=1800 EXEC_INTERVAL=7200 nohup ./agents/agent_loop.sh ...
# stop:  pkill -f agent_loop.sh
```
**Not wired into `run.sh`** on purpose — opt-in while it's a prototype.

## Outputs
- `shared_memory/agent_strategy.json` · `shared_memory/agent_execution.json` — latest brief (the
  dashboard can read these later; schema is defined in each `roles/*.md`).
- `agents/history/<agent>.jsonl` — append-only audit trail of every heartbeat.
- `agents/history/token_usage.jsonl` — per-heartbeat token ledger (answers "what does this cost").

## Token cost (MEASURED live via the claude CLI backend)
Input is cheap because context = the tools' **already-digested** output (never raw
`forward_obs.jsonl`): ~2.7k chars for strategy, ~1.7k for execution, almost all cache-read after
the first call. **Cost is dominated by OUTPUT** — the briefs are detailed (6 findings + candidate
table ≈ 6–7k output tokens).

Measured (Sonnet, `--output-format json` reports real `total_cost_usd`):
- **Strategy heartbeat ≈ $0.14** (out ~6.7k tok, cache-read ~9.9k, cache-write ~7.7k)
- **Execution heartbeat ≈ $0.05–0.10** (smaller context + shorter brief)

At default cadence (strategy hourly, execution every 3h): **≈ $3.5–4.5/day** for both agents.
The per-call ledger is in `agents/history/token_usage.jsonl` (incl. `cost_usd`) — cost is
self-measuring, not estimated. To cut it: raise the cadence interval, switch routine runs to
`--model haiku`, or cap brief verbosity in the role prompt.

## Migration to paperclip (nothing here is throwaway)
| Prototype artifact | Becomes in paperclip |
|---|---|
| `roles/<agent>.md` | the agent definition / system prompt (`claude_local` adapter) |
| `CONTEXT_SOURCES` in `agent_runner.py` | the adapter's pre-prompt context assembly |
| JSON schema in each role prompt | the task-result contract |
| `agent_loop.sh` cadence | the heartbeat schedule + budget config in `AGENTS.md` |
| advisory-only boundary | a governance/approval policy (Risk Governor must approve acting fixes) |

When you adopt paperclip, you stand up the Node control plane and point its `claude_local`
adapters at these same role files — the reasoning layer is already proven and costed.
