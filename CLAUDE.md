# CLAUDE.md — operating guide for PIPBOI

You are helping someone run **their own** PIPBOI: an autonomous Solana
memecoin trading bot with a live dashboard and an evidence-gated learning
loop. This file is your map and your rulebook. Read it before acting.

The person you're helping owns the wallet and the risk. Your job is to help them
**understand, set up, run, and reason about** this system — carefully.

**Layout:** all code lives in `src/` (flat — `import wallet`, `import config`, etc.
resolve because everything runs with `src/` as the working directory). Tests are in
`tests/`. The `.env` lives at the repo root. The `./pipboi` launcher at the root
runs everything from `src/` for you — `./pipboi status`, `./pipboi run`,
`./pipboi setup`, `./pipboi doctor`, `./pipboi test`, or `./pipboi <tool>`
(e.g. `./pipboi edge_report`).

**When the user is stuck on setup/install** ("it won't start", "ModuleNotFound",
"no trades", "where's my PIN?") → run **`./pipboi doctor`** first. It's a stdlib-only
self-check (works even before `pip install`) that reports Python version, deps,
`.env`, the Helius key, the dashboard PIN, the wallet, and the port — each with the
exact fix. Paste its output back; it usually pinpoints the problem in one step.

---

## 🚦 Rules (read these first)

1. **THE ONE RULE: never manually enable EV-sizing.** Do not hand-write or
   create `src/bots/botN/ev_sizing.json`. Sizing up an unproven edge loses money
   faster and corrupts the learning signal. Sizing is armed *only* by the gates
   (`arm_genes.py`) when the edge has proven itself on real, fee-inclusive
   results. If asked to "just size up," explain why the gate exists instead.

2. **This trades real money.** Any change to admission, sizing, or exit logic can
   cost the user SOL. Before applying a trading-logic change, say plainly what it
   does and what it risks, and prefer a small/reversible/canary-gated rollout.
   When in doubt, propose it and let the user decide.

3. **Never expose secrets.** Never print, commit, or paste the contents of
   `.env`, any `keypair.json`/`id.json`, or `*.db`. These are gitignored — keep
   them that way. Never run `git add -A` without checking what it would stage.
   Never push to a remote without the user's explicit go-ahead.

4. **Default to read-only first.** Diagnose with the read-only tools before
   changing live behavior. Confirm the bot is actually running before
   concluding anything from logs (see "Liveness" below).

5. **Be honest about the edge.** A durable positive edge is hard and frequently
   *unproven* here. Don't imply the bot reliably makes money. Encourage
   observation and paper/research use, especially for a new user.

---

## 👀 Check on the bot — the quick glance (do this when asked "how's it doing?")

The easiest way to see the bot is to **just ask you, in plain English** — no web
server, no browser. When the user says anything like *"how's my bot?"*,
*"how's it doing?"*, *"check the bot"*, *"status?"*, *"are we up?"* — run:

```bash
./pipboi status                    # the bot (the default)
./pipboi status --trades 8         # more recent trades
```

(`./pipboi` with no args does the same thing. If you'd rather call Python directly,
`python3 src/status.py` works too.)

It prints one clean terminal card: wallet ◎, progress toward the ◎2.0 prestige
goal, today's P&L, net realized, win/loss record, open positions, and the recent
trades **with the reason each one closed** (trail / stop / dead pool / rug exit).
**Paste that card back to the user as the answer** — it's the whole point: a
glanceable readout right here in the chat. It's read-only and safe to run any
time (trading or stopped). Then, if they want the deeper "is it *making money*"
analysis, reach for the gate/edge tools below.

---

## What this system is

A **discovery sidecar** streams candidate tokens to the **bot**; the bot scores
candidates, decides admission, sizes from its live wallet balance, executes via
Jupiter, and manages exits. A **dashboard** shows it all. An **evidence loop**
logs the forward return of everything scored and learns which rules actually
pay — and **gates** decide if/when to ever size an edge up.

```
data sources → discovery_service.py (sidecar) → bot (main.py) → dashboard.py
                                                      │
                              signal_lab → strategy_brain → gates (arm sizing)
```

## Key files

**Trading core**
- `main.py` — the bot's loop: signals · execution · exits · the sizing block.
- `observer.py` — candidate discovery + scoring + admission decisions.
- `stoic_strategy.py` — position lifecycle: stops, trails, take-profit, rug/drain
  guards, dead-pool exits.
- `validation.py` — the entry scorer (dimensions + thresholds).
- `config.py` / `config/` — configuration, env loading, sizing constants.

**Discovery & execution**
- `discovery_service.py` — the shared sidecar (poll, score-prep, publish snapshot).
- `discovery.py`, `dexscreener.py`, `bonding_curve.py`, `dex_discovery.py`,
  `bitquery_service.py` — candidate sources.
- `wallet.py`, `jupiter.py`, `helius.py`, `safety.py` — chain + swap plumbing.

**Evidence / learning loop**
- `signal_lab.py` — logs every scored token's features + forward return.
- `strategy_brain.py` — scores candidate *rules* on realized EV; promotes the
  stable, positive ones.
- `live_rule.py`, `ev_sizing.py` — admit learned rules · the (gated) sizing gene.
- `honest_objective.py` — the realized, fee-inclusive objective the brain learns on.

**Gates & verdict tools (read-only)**
- `prestige_tracker.py` — balances, the EV-sizing deploy gate, days-to-goal.
- `arm_genes.py` — the only thing that arms sizing; gate-gated, fail-closed.
- `kill_criterion.py` — pre-registered "is the deep-pool edge real?" verdict.
- `prove_edge.py`, `ride_ab.py`, `edge_report.py`, `regime_realized.py`,
  `fee_report.py` — realized-EV analysis, by strategy and exit.

(All of the above live in `src/`.)

**Dashboard / ops**
- `src/dashboard.py`, `src/templates/` — the web UI (`localhost:8080`).
- `run.sh` / `pipboi` — launchers (start sidecar + dashboard + bot).
- `deploy/` — optional macOS LaunchAgents for reboot-durable uptime (`deploy/install.sh`).
- `src/agents/` — advisory Claude agents (needs `ANTHROPIC_API_KEY`); advisory only,
  no path to funds.

## Where state lives (all gitignored, machine-local, under src/)

- `src/bots/botN/` — per-bot: `keypair.json`, `trades.db`, `status.json`,
  `positions.json`, `balance_history.jsonl`, and per-feature "canary" JSONs
  (each enables one opt-in behavior; absent = off).
- `src/shared_memory/` — `forward_obs.jsonl` (the evidence log), the brain's state,
  the discovery snapshot, sidecar heartbeat.
- `src/logs/` — run logs.

## Running & restarting

```bash
./pipboi run             # start everything (sidecar + dashboard + bot); ./run.sh also works
# stop: Ctrl-C in that terminal (it tears down the whole process group)
```

- **Dashboard:** http://localhost:8080. UI-only template edits need just a
  browser refresh, not a restart.
- **Code changes need a restart to take effect.** A subtle trap: a change on
  disk is *not* live until the process restarts. If a fix "isn't working,"
  check that the running process started *after* you edited the file.
- **macOS** `run.sh` uses `caffeinate`; on Linux it auto-falls-back to running the
  dashboard directly (or run `python3 src/discovery_service.py` and
  `python3 src/dashboard.py` yourself).

## Liveness (don't trust log greps alone)

The run log is block-buffered, so advancing log lines aren't proof of life.
Confirm the bot is alive via:
- `src/bots/botN/status.json` mtime (freshly updated), and
- `src/shared_memory/sidecar_heartbeat.json`, and
- the actual `main.py` / `discovery_service.py` processes.

## The evidence & gate philosophy (why it's cautious)

The bot logs the forward return of everything it scores, learns rules from
*realized* outcomes, and **refuses to size up** until an edge clears a
pre-registered, fee-inclusive bar. `kill_criterion.py` can even declare an edge
*disproven* and disarm sizing. This discipline — safe-direction-only, fail-
closed, hard to fool — is the most valuable part of the system. Respect it.
When you propose changes, prefer ones that the gates can still judge honestly.

## Common things the user may ask you to do

- **"Help me set it up" / "onboard me"** → walk through `SETUP.md`. The flow:
  `pip install -r requirements.txt` → `./pipboi run` → finish setup. There are three
  equivalent ways to enter keys — pick whatever the user prefers:
  (1) the **browser setup page** at `http://localhost:8080` (first run shows it),
  (2) the **terminal wizard** `python3 src/setup.py`, or
  (3) **you do it for them** — help them get a free Helius key
  (https://dashboard.helius.dev) and optionally a Bitquery key
  (https://account.bitquery.io), then write `.env` (you may run `python3
  src/setup.py`, or set the values — but **never echo the secret values back** in
  chat). Then they click **Activate** in the dashboard, which generates the
  wallet and starts trading. Restart the bot after `.env` changes.
- **"Is it making money?"** → `./pipboi edge_report --by-play`,
  `./pipboi prestige_tracker`, `./pipboi kill_criterion`. Report honestly,
  including when the answer is "no / not proven."
- **"Why did it (not) trade X?"** → the `SCOUT` tab + the bot's logs carry skip
  reasons; the ledger (`trades.db` / `LEDGER` tab) carries exit reasons.
- **"Restart it"** → Ctrl-C the `./pipboi run` terminal, `./pipboi run`. Verify liveness.
- **"Tune the strategy"** → explain the tradeoff, prefer a `src/bots/botN/` canary
  file (opt-in, reversible — delete it to revert), keep it reversible, never
  touch `ev_sizing.json` by hand.

## Boundaries — pause and ask the user before:

- Anything that **moves or risks funds** beyond normal operation.
- Editing admission / sizing / exit **trading logic**.
- `git push`, making a repo public, or anything that **leaves the machine**.
- Deleting `src/bots/` state, databases, or wallet files.

When unsure, show the plan and let the user decide. You're the careful co-pilot,
not the risk-taker.

