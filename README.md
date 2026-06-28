<div align="center">

<img src="assets/datboi-logo.png" alt="DATBOI" width="150">

# 🐸 DATBOI

**An open-source autonomous Solana memecoin trading bot you run yourself — your
wallet, your keys, your machine. A live terminal dashboard, an evidence-gated
learning loop, and a design you can operate through your own Claude.**

[🌐 Live site](https://onasidequestt.github.io/datpipboi/) · [❓ FAQ](FAQ.md) · [🔒 Security](SECURITY.md) · [⚙️ Setup](SETUP.md) · [MIT License](LICENSE)

*o shit waddup — dat boi rollin' in.*

</div>

---

> ### ⚠️ Read this first — honest disclaimer
>
> This is an **experimental trading research platform**, not a money printer. It
> trades real SOL on some of the most volatile markets that exist (freshly
> launched memecoins). **You can — and likely will — lose money.** The system's
> own verdict tools (`kill_criterion.py`, `prove_edge.py`) exist precisely
> because a durable, positive edge is *hard* and often unproven on free data.
>
> Treat this as a platform to **learn, paper-trade, and research** autonomous
> trading — not as financial advice. Run it on a **fresh wallet funded with an
> amount you can afford to lose entirely.** You are solely responsible for your
> own funds and decisions. **`$DATBOI` is a community token, not a fund and not a
> promise of returns.**

---

## Is this safe to run? (the short version)

DATBOI runs **entirely on your machine**. There is no account, no server, no
telemetry — nothing phones home. Your API keys and wallet live in local,
git-ignored files and are never transmitted. The bot can only ever spend the SOL
in the one wallet you point it at. **This repo has been scanned and contains no
keys, secrets, or wallet addresses.** The full breakdown is in **[SECURITY.md](SECURITY.md)** —
read it before you fund anything.

## What it is

- **A trading bot** that discovers, scores, sizes, and exits positions on its
  own, on your wallet.
- **A discovery sidecar** that streams candidate tokens from free data sources
  (GeckoTerminal, DexScreener, bonding-curve flow, optional Bitquery) so the bot
  isn't starved.
- **A live terminal dashboard** at `localhost:8080` — bot status, P&L, the ledger
  of every trade with *why* it closed, and the bot's internals.
- **An evidence loop + deploy gates** — it logs the forward return of every token
  it scores, learns rules from realized outcomes, and refuses to size up an edge
  until it has *proven* itself on real, fee-inclusive results. The discipline is
  the point.
- **Operate it with Claude** — a [`CLAUDE.md`](CLAUDE.md) ships with the repo so a
  fresh Claude (e.g. [Claude Code](https://claude.com/claude-code)) understands
  the whole system and can help you set it up, run it, and reason about it safely.

## Quickstart

### 👀 Try it in 30 seconds — no wallet, no keys, no risk

```bash
git clone https://github.com/Onasidequestt/datpipboi.git
cd datpipboi && ./datboi          # prints a status card — pure stdlib, nothing to fund
```

A phosphor-green status card prints right in your terminal — no signup, no
funding, nothing to lose. Once it's actually trading, that same card shows your
live vault, P&L, and every trade with *why* it closed. Like the vibe? Run the
full bot 👇

### 🚀 Run the full bot

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./datboi run                      # (or ./run.sh) — opens the dashboard at http://localhost:8080
```

**Enter your keys whichever way you like** (you only need a free **Helius** key to start):

- **🖥 In the browser (no editing):** first run opens **http://localhost:8080** with
  a **setup page** — paste your Helius key (and optional Bitquery key; links to get
  them are right there), unlock with the **PIN** that `./datboi run` printed in your
  terminal, save, and restart.
- **⌨️ In the terminal:** `python3 src/setup.py` runs an interactive wizard.
- **🤖 With your own Claude:** open the repo in Claude Code and say *"help me set
  this up."* [`CLAUDE.md`](CLAUDE.md) gives it everything it needs.

Then in the dashboard, click **Activate** — DATBOI **generates a fresh wallet,
shows you the address to fund, and starts logging trades.** (Prefer your own
keypair? Point `KEYPAIR_PATH` at it in `.env` instead.)

👉 **Full step-by-step (keys, wallet, funding, operating):** see [SETUP.md](SETUP.md).

## Check on it — just ask Claude

No web server, no browser needed. Open the repo in Claude and **ask in plain
English** — *"how's my bot doing?"* — and it prints one clean card right in the chat:

```text
╔══════════════════════════════════════════════════════════════╗
║  $DATBOI · DATBOI                               ● ONLINE ║
╠══════════════════════════════════════════════════════════════╣
║  WALLET        ◎ 0.8007                                      ║
║  MILESTONE     ◎ 2.0                                         ║
║  ██████████████░░░░░░░░░░░░░░░░░░░░  40%                     ║
║  TODAY         +0.0000 ◎   break-even                        ║
║  RECORD        97 trades · 38W / 59L · 39% win               ║
╠══════════════════════════════════════════════════════════════╣
║  RECENT TRADES                                               ║
║  ▲ +0.0264 ◎   runner    trail +31%   15m                    ║
║  ▼ -0.0024 ◎   quick     dead pool    3m                     ║
╚══════════════════════════════════════════════════════════════╝
```

Wallet, progress toward the ◎2.0 milestone (an internal graduation gate — not a
target or promised return), today's P&L, win/loss, and every recent trade **with
the reason it closed**. Prefer the terminal? Same thing, one word:

```bash
./datboi                 # status card   ·   ./datboi status --trades 8
```

It's read-only — safe to run any time, trading or stopped. For the full graphical
view, the dashboard is at **http://localhost:8080**.

## How it fits together

```
   data sources                discovery_service.py             the bot
 ┌───────────────┐   feeds   ┌──────────────────────┐  snapshot  ┌──────────┐
 │ GeckoTerminal │──────────▶│  discovery sidecar:  │───────────▶│          │  observe → score
 │ DexScreener   │           │  poll · score-prep · │            │   bot    │  → admit → size
 │ bonding curve │           │  publish snapshot    │            │          │  → execute → exit
 │ Bitquery (opt)│           └──────────────────────┘            └────┬─────┘
 └───────────────┘                                                    │
                                                                      ▼
                              dashboard.py  ◀───────  status / trades.db / positions
                              http://localhost:8080
```

- **`observer.py`** scores each candidate and decides admission.
- **`stoic_strategy.py`** owns position lifecycle and exits (stops, trails, rug guards).
- **`main.py`** is the bot's loop: signals → execution → exits → sizing.
- **`signal_lab.py` / `strategy_brain.py`** are the evidence loop: log forward
  returns, learn rules, promote only what proves out.
- **Gates** (`prestige_tracker.py`, `arm_genes.py`, `kill_criterion.py`) decide
  when — if ever — to size an edge up. They fail closed.

See [CLAUDE.md](CLAUDE.md) for the full map.

## Project layout

The repo root stays minimal; all the code lives in **`src/`**, grouped by role:

| Path | What |
|---|---|
| `datboi`, `run.sh` | the launchers — `./datboi` (status/run/setup/tools) and `./run.sh` |
| `src/main.py`, `observer.py`, `stoic_strategy.py`, `validation.py` | trading core (loop, scoring, exits) |
| `src/discovery_service.py`, `discovery.py`, `dexscreener.py`, `bonding_curve.py` | candidate discovery |
| `src/wallet.py`, `jupiter.py`, `helius.py`, `safety.py` | execution + chain plumbing |
| `src/signal_lab.py`, `strategy_brain.py`, `live_rule.py`, `honest_objective.py` | the evidence / learning loop |
| `src/prestige_tracker.py`, `arm_genes.py`, `kill_criterion.py`, `prove_edge.py`, `edge_report.py` | gates + read-only verdict tools |
| `src/dashboard.py`, `src/templates/` | the live web dashboard |
| `src/config.py`, `src/config/` | configuration |
| `src/agents/` | optional advisory Claude agents (advisory only, no path to funds) |
| `tests/` | the test suite (`./datboi test`) |
| `deploy/` | optional macOS LaunchAgents for reboot-durable uptime |
| `docs/` | the project site (served via GitHub Pages) |

## Requirements

- **Python 3.9+**
- **macOS or Linux** (`run.sh` uses macOS `caffeinate`; on Linux it falls back
  automatically — see SETUP.md)
- A **Helius** API key (free tier to start) and a **Solana wallet** (the dashboard
  can generate one for you)
- A small amount of **SOL** to trade with (start tiny)

## Safety & secrets

- Your `.env`, wallet keypairs, trade databases, and runtime state are **all
  git-ignored** and never leave your machine.
- The bot can only ever spend the SOL in the wallet you point it at. **Fund it
  with little. Start in observation, watch the dashboard, learn the system.**
- Full detail, including exactly what can move money: **[SECURITY.md](SECURITY.md)**.

## $DATBOI

`$DATBOI` is the **community token** for DATBOI — a flag for an open, honest,
self-hostable trading-research project. It is **not a fund**, not a managed
account, and **not a promise of returns**. Value, if any, comes from what the
community builds and adopts. The bot above is the real, working product; the token
is the community around it. Always run your own bot on your own keys.

## License

[MIT](LICENSE) — and explicitly **not financial advice**, no warranty, use at your
own risk.
