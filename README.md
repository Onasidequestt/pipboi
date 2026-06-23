<div align="center">

# ⚙ Vault Bot

**An autonomous Solana memecoin trading fleet — with a live terminal dashboard,
an evidence-gated learning loop, and a design that lets you run and operate it
through your own Claude.**

</div>

---

> ### ⚠️ Read this first — honest disclaimer
>
> This is an **experimental trading research platform**, not a money printer.
> It trades real SOL on some of the most volatile markets that exist (freshly
> launched memecoins). **You can — and likely will — lose money.** The system's
> own built-in verdict tools (`kill_criterion.py`, `prove_edge.py`) are there
> precisely because a durable, positive edge is *hard* and often unproven on
> free data.
>
> Treat this as a platform to **learn, paper-trade, and research** autonomous
> trading — not as financial advice. Run it on a **fresh wallet funded with an
> amount you can afford to lose entirely.** You are solely responsible for your
> own funds and decisions.

---

## What it is

Vault Bot is a self-contained autonomous trading system for Solana memecoins:

- **A fleet of trading bots** (up to 6) that discover, score, size, and exit
  positions on their own, each on its own wallet.
- **A shared discovery sidecar** that streams candidate tokens from multiple
  free data sources (GeckoTerminal, DexScreener, bonding-curve flow, optional
  Bitquery) so the fleet isn't starved.
- **A live "Pip-Boy" terminal dashboard** at `localhost:8080` — fleet status,
  P&L, the ledger of every trade with *why* it closed, and per-bot internals.
- **An evidence loop + deploy gates** — the system logs the forward return of
  every token it scores, learns rules from realized outcomes, and refuses to
  size up an edge until it has *proven* itself on real, fee-inclusive results.
  The discipline is the point.
- **Operate it with Claude** — a [`CLAUDE.md`](CLAUDE.md) ships with the repo so
  a fresh Claude (e.g. [Claude Code](https://claude.com/claude-code)) understands
  the whole system and can help you set it up, run it, and reason about it safely.

## Why "on your own Claude"

The codebase was built to be **read, operated, and extended conversationally.**
Point Claude at this repo and it can: explain any part, walk you through setup,
restart the fleet, read the dashboards, run the read-only analysis tools, and
help you tune things — all guided by [`CLAUDE.md`](CLAUDE.md), which encodes the
architecture, the operating rules, and the safety boundaries.

## Quickstart

```bash
git clone https://github.com/<your-github-username>/vault-bot.git
cd vault-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                      # opens the dashboard at http://localhost:8080
```

**Onboard whichever way you like:**

- **🖥 In the web viewer (no terminal editing):** first run opens
  **http://localhost:8080**, which shows a **setup page** — paste your **Helius**
  key (required) and **Bitquery** key (optional, links to get them are right
  there), hit save, restart, done.
- **🤖 With your own Claude:** open the repo in Claude Code and say *"help me set
  this up."* The bundled [`CLAUDE.md`](CLAUDE.md) lets it walk you through keys,
  wallet, and running it.
- **⌨️ In the terminal:** `python3 setup.py` runs an interactive wizard.

Then in the dashboard, click **Activate** on a bot — it **generates a fresh
wallet, shows you the address to fund, and starts logging trades.** Your
dashboard **PIN** (to unlock admin actions) is printed when you run `./run.sh`.

👉 **Full step-by-step (wallet, keys, funding, operating):** see [SETUP.md](SETUP.md).

## Check on it — just ask Claude

No web server, no browser needed. Open the repo in Claude and **ask in plain
English** — *"how's my bot doing?"* — and it prints one clean card right in the chat:

```text
╔══════════════════════════════════════════════════════════════╗
║  $VAULT · BOT 1                                     ● ONLINE ║
╠══════════════════════════════════════════════════════════════╣
║  WALLET        ◎ 0.8007                                      ║
║  TO PRESTIGE   ◎ 2.0                                         ║
║  ██████████████░░░░░░░░░░░░░░░░░░░░  40%                     ║
║  TODAY         +0.0000 ◎   break-even                        ║
║  RECORD        97 trades · 38W / 59L · 39% win               ║
╠══════════════════════════════════════════════════════════════╣
║  RECENT TRADES                                               ║
║  ▲ +0.0264 ◎   runner    trail +31%   15m                    ║
║  ▼ -0.0024 ◎   quick     dead pool    3m                     ║
╚══════════════════════════════════════════════════════════════╝
```

Wallet, progress to the ◎2.0 prestige goal, today's P&L, win/loss, and every
recent trade **with the reason it closed**. Prefer the terminal? Same thing, one word:

```bash
./vault            # bot 1     ·     ./vault --all     ·     ./vault --trades 8
```

It's read-only — safe to run any time, trading or stopped. ([`CLAUDE.md`](CLAUDE.md)
tells Claude to run it whenever you ask about the bot.) For the full graphical
view, the dashboard is still at **http://localhost:8080**.

## How it fits together

```
   data sources                discovery_service.py             the fleet
 ┌───────────────┐   feeds   ┌──────────────────────┐  snapshot  ┌──────────┐
 │ GeckoTerminal │──────────▶│  shared sidecar:     │───────────▶│  bot 1   │
 │ DexScreener   │           │  poll · score-prep · │            │  bot 2   │  each:
 │ bonding curve │           │  publish snapshot    │            │  bot 3   │  observe → score
 │ Bitquery (opt)│           └──────────────────────┘            └────┬─────┘  → admit → size
 └───────────────┘                                                    │        → execute → exit
                                                                      ▼
                              dashboard.py  ◀───────  status / trades.db / positions
                              http://localhost:8080
```

- **`observer.py`** scores each candidate and decides admission.
- **`stoic_strategy.py`** owns position lifecycle and exits (stops, trails, rug guards).
- **`main.py`** is the per-bot loop: signals → execution → exits → sizing.
- **`signal_lab.py` / `strategy_brain.py`** are the evidence loop: log forward
  returns, learn rules, promote only what proves out.
- **Gates** (`prestige_tracker.py`, `arm_genes.py`, `kill_criterion.py`) decide
  when — if ever — to size an edge up. They fail closed.

See [CLAUDE.md](CLAUDE.md) for the full map.

## Requirements

- **Python 3.9+**
- **macOS or Linux** (`run.sh` uses macOS `caffeinate`; on Linux just run the
  components directly — see SETUP.md)
- A **Helius** API key (free tier to start) and a **Solana wallet** keypair
- A small amount of **SOL** to trade with (start tiny)

## Project layout

| Path | What |
|---|---|
| `main.py`, `observer.py`, `stoic_strategy.py` | the trading core (loop, scoring, exits) |
| `discovery_service.py`, `discovery.py`, `dexscreener.py`, `bonding_curve.py` | candidate discovery |
| `wallet.py`, `jupiter.py`, `helius.py`, `safety.py` | execution + chain plumbing |
| `signal_lab.py`, `strategy_brain.py`, `live_rule.py`, `ev_sizing.py` | the evidence / learning loop |
| `prestige_tracker.py`, `arm_genes.py`, `kill_criterion.py`, `prove_edge.py` | gates + read-only verdict tools |
| `dashboard.py`, `templates/` | the live web dashboard |
| `config.py`, `config/` | configuration |
| `deploy/` | optional macOS LaunchAgents (reboot-durable uptime) |
| `agents/`, `telegram/` | optional: advisory Claude agents · public Telegram bridge |
| `test_*.py` | unit tests |

## Safety & secrets

- Your `.env`, wallet keypairs, trade databases, and runtime state are **all
  gitignored** and never leave your machine.
- Never commit a real `.env` or `keypair.json`. The `.gitignore` is set up to
  prevent it — keep it that way.
- The bot can only ever spend the SOL in the wallet you point it at. **Fund it
  with little. Start in observation, watch the dashboard, learn the system.**

## License

[MIT](LICENSE) — and explicitly **not financial advice**, no warranty, use at
your own risk.
