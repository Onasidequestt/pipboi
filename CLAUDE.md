# CLAUDE.md — operating guide for Vault Bot

You are helping someone run **their own** Vault Bot: an autonomous Solana
memecoin trading fleet with a live dashboard and an evidence-gated learning
loop. This file is your map and your rulebook. Read it before acting.

The person you're helping owns the wallet and the risk. Your job is to help them
**understand, set up, run, and reason about** this system — carefully.

---

## 🚦 Rules (read these first)

1. **THE ONE RULE: never manually enable EV-sizing.** Do not hand-write or
   create `bots/botN/ev_sizing.json`. Sizing up an unproven edge loses money
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
   changing live behavior. Confirm the fleet is actually running before
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
python3 vault_status.py            # bot 1 (the default)
python3 vault_status.py --all      # every bot they're running
python3 vault_status.py --trades 8 # more recent trades
```

(The user can also type `./vault` themselves — same thing, one word. You should
just run `python3 vault_status.py`, which always works regardless of PATH setup.)

It prints one clean terminal card: wallet ◎, progress toward the ◎2.0 prestige
goal, today's P&L, net realized, win/loss record, open positions, and the recent
trades **with the reason each one closed** (trail / stop / dead pool / rug exit).
**Paste that card back to the user as the answer** — it's the whole point: a
glanceable readout right here in the chat. It's read-only and safe to run any
time (trading or stopped). Then, if they want the deeper "is it *making money*"
analysis, reach for the gate/edge tools below.

---

## What this system is

A shared **discovery sidecar** streams candidate tokens to a **fleet of bots**;
each bot scores candidates, decides admission, sizes from its live wallet
balance, executes via Jupiter, and manages exits. A **dashboard** shows it all.
An **evidence loop** logs the forward return of everything scored and learns
which rules actually pay — and **gates** decide if/when to ever size an edge up.

```
data sources → discovery_service.py (sidecar) → bots (main.py ×N) → dashboard.py
                                                      │
                              signal_lab → strategy_brain → gates (arm sizing)
```

## Key files

**Trading core**
- `main.py` — the per-bot loop: signals · execution · exits · the sizing block.
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

**Dashboard / ops**
- `dashboard.py`, `templates/` — the web UI (`localhost:8080`).
- `run.sh` — starts sidecar + dashboard + bots.
- `deploy/` — optional macOS LaunchAgents for reboot-durable uptime.

**Optional**
- `agents/` — advisory Claude agents (needs `ANTHROPIC_API_KEY`); advisory only,
  no path to funds.
- `telegram/` — a read-only public Telegram broadcast bridge.

## Where state lives (all gitignored, machine-local)

- `bots/botN/` — per-bot: `keypair.json`, `trades.db`, `status.json`,
  `positions.json`, `balance_history.jsonl`, and per-feature "canary" JSONs
  (each enables one opt-in behavior; absent = off).
- `shared_memory/` — `forward_obs.jsonl` (the evidence log), the brain's state,
  the discovery snapshot, sidecar heartbeat.
- `logs/` — run logs.

## Running & restarting

```bash
./run.sh                 # start everything (sidecar + dashboard + bots)
# stop: Ctrl-C in that terminal (it tears down the whole process group)
```

- **Dashboard:** http://localhost:8080. UI-only template edits need just a
  browser refresh, not a restart.
- **Code changes need a restart to take effect.** A subtle trap: a change on
  disk is *not* live until the process restarts. If a fix "isn't working,"
  check that the running process started *after* you edited the file.
- **macOS** `run.sh` uses `caffeinate` (no-op problem on Linux — run
  `discovery_service.py` and `dashboard.py` directly there).

## Liveness (don't trust log greps alone)

The run log is block-buffered, so advancing log lines aren't proof of life.
Confirm the fleet is alive via:
- `bots/botN/status.json` mtime (freshly updated), and
- `shared_memory/sidecar_heartbeat.json`, and
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
  `pip install -r requirements.txt` → `./run.sh` → finish setup. There are three
  equivalent ways to enter keys — pick whatever the user prefers:
  (1) the **browser setup page** at `http://localhost:8080` (first run shows it),
  (2) the **terminal wizard** `python3 setup.py`, or
  (3) **you do it for them** — help them get a free Helius key
  (https://dashboard.helius.dev) and optionally a Bitquery key
  (https://account.bitquery.io), then write `.env` (you may run `python3
  setup.py`, or set the values — but **never echo the secret values back** in
  chat). Then they click **Activate** in the dashboard, which generates the
  wallet and starts trading. Restart the fleet after `.env` changes.
- **"Is it making money?"** → `python3 edge_report.py --by-play`,
  `python3 prestige_tracker.py`, `python3 kill_criterion.py`. Report honestly,
  including when the answer is "no / not proven."
- **"Why did it (not) trade X?"** → the `SCOUT` tab + the bot's logs carry skip
  reasons; the ledger (`trades.db` / `LEDGER` tab) carries exit reasons.
- **"Restart it"** → Ctrl-C the `run.sh` terminal, `./run.sh`. Verify liveness.
- **"Tune the strategy"** → explain the tradeoff, prefer a per-bot canary file
  and/or one-bot A/B, keep it reversible, never touch `ev_sizing.json` by hand.

## Boundaries — pause and ask the user before:

- Anything that **moves or risks funds** beyond normal operation.
- Editing admission / sizing / exit **trading logic**.
- `git push`, making a repo public, or anything that **leaves the machine**.
- Deleting `bots/` state, databases, or wallet files.

When unsure, show the plan and let the user decide. You're the careful co-pilot,
not the risk-taker.

---

## Pip-Sol — the dead-simple one-wallet vault (talk to it in plain English)

Pip-Sol is the stripped-down door to this bot for non-experts: **one wallet, set up in ~2 prompts,
watched right here in the terminal — no browser, no dashboard.** You send it SOL and the deposit
activates it; when it's in profit you run one command and 50% of profit comes home to your wallet
(principal always protected, nothing moves on its own).

When the user mentions *"pip-sol" / "my pip bot" / "the simple vault"*, map plain English to the
`pip-sol` verb and paste the result back:

| the user says… | run | notes |
|---|---|---|
| set up pip-sol | `python3 pip-sol setup` | ~2 prompts → a deposit address |
| how's pip-sol? / are we up? | `python3 pip-sol status` | paste the card |
| what's my deposit address? | `python3 pip-sol address` | |
| start it | `python3 pip-sol start` | waits for the deposit to activate it |
| send my profits home / harvest | `python3 pip-sol harvest` | sends 50% of profit home; **user confirms** |
| where do profits go? / change payout wallet | `python3 pip-sol payout <addr>` | |
| go live / trade for real | `python3 pip-sol golive` | spends real SOL — say so first |
| stop it | `python3 pip-sol stop` | the SOL stays in the wallet |

**Safety:** Pip-Sol runs the *careful* posture (proven runner lane + every guard), never writes
`ev_sizing.json`, and **only moves money via `pip-sol harvest`, with your confirmation.** Real money,
can lose, not a fund.
| get my money back / withdraw | `python3 pip-sol withdraw [N]` | sends your SOL home (all, or ◎N) — the easy exit |
| type pip-sol from anywhere | `python3 pip-sol install` | symlink onto PATH |
