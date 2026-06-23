# Setting up your own Dat Pip Boi

This walks you from zero to a running fleet on the dashboard. Budget ~15–20 min.
If you have Claude Code, you can also just say *"help me set up this repo"* — the
included [`CLAUDE.md`](CLAUDE.md) gives it everything it needs to do this with you.

> ⚠️ **Before you start:** this trades real money on volatile markets and can
> lose all of it. Use a **brand-new wallet** funded with an amount you are 100%
> okay losing. Read the disclaimer in [README.md](README.md).

---

## 1. Prerequisites

- **Python 3.9 or newer** — check with `python3 --version`
- **macOS or Linux**
- The **Solana CLI** (for creating a wallet) — install:
  ```bash
  sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"
  ```
- A free **Helius** account → https://dashboard.helius.dev

## 2. Clone & install

```bash
git clone https://github.com/<your-github-username>/vault-bot.git
cd vault-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Create a trading wallet

Create a **fresh** keypair just for this bot (never reuse your main wallet):

```bash
solana-keygen new --outfile ~/.config/solana/vault-bot.json
```

Note the **pubkey** it prints — that's the address you'll fund. Then **fund it
with a small amount of SOL** (e.g. 0.1–0.5 SOL to start). The bot sizes every
trade from the live wallet balance, so a small balance = small trades.

## 4. Get a Helius API key

1. Go to https://dashboard.helius.dev → create a project.
2. Copy the **API key**.

The free tier is enough to begin. (You can add more keys later to spread rate
limits across bots — see `HELIUS_API_KEY_1..3` in `.env.example`.)

## 5. Run it

```bash
./run.sh
```

This starts the **discovery sidecar**, the **dashboard**, and the **bots**, and
prints your **dashboard PIN**. Open:

```
http://localhost:8080
```

## 6. Finish setup in the browser

On first run the dashboard shows a **setup page**. Enter:

- your **dashboard PIN** (printed in the terminal by `./run.sh`),
- your **Helius** API key *(required — link to get one is on the page)*,
- your **Bitquery** key *(optional)*,

…and click **Save & continue**. Then restart the fleet (`Ctrl-C`, `./run.sh`) and
reload — the full dashboard appears.

> **Prefer not to use the browser?** Two alternatives, same result:
> - **Terminal wizard:** `python3 setup.py` (interactive, writes `.env`).
> - **Ask your Claude:** open the repo in Claude Code and say *"set this up for
>   me"* — `CLAUDE.md` gives it what it needs.
>
> Re-run `python3 setup.py` (or revisit `/setup`) anytime to change settings.

> **On Linux** (no `caffeinate`): run the sidecar + dashboard directly instead of `./run.sh`:
> ```bash
> python3 discovery_service.py &      # the shared sidecar
> python3 dashboard.py                # the dashboard (spawns the bots)
> ```

## 7. Deploy a bot

In the dashboard, a bot starts **undeployed**. Click to deploy bot #1 — it will
either generate a keypair for that bot or use the one at `KEYPAIR_PATH`. Fund
that wallet, and the bot begins trading on the next cycle.

Start by just **watching**: the dashboard shows every candidate scored, every
trade opened, and every close *with the reason why*. Get a feel for it before
you scale anything.

---

## Operating it

- **Dashboard tabs:** `SCOUT` (what it's seeing), `TRADE` (live positions),
  `LEDGER` (every closed trade + why), `GENE` (per-bot internals + the gate).
- **Restart the fleet:** `Ctrl-C` in the `run.sh` terminal, then `./run.sh` again.
- **Read-only health/analysis tools** (safe to run anytime):
  ```bash
  python3 prestige_tracker.py     # balances, the deploy gate, days-to-goal
  python3 edge_report.py --by-play # realized edge by strategy, fee-aware
  python3 kill_criterion.py        # the pre-registered "is the edge real?" verdict
  python3 race.py                  # the prestige-race leaderboard
  ```

## Changing settings (the easy knobs)

| What | How |
|---|---|
| **Max trade size** | In the dashboard, use the per-bot **size buttons**: `small` / `medium` / `large` = 50% / 75% / 100% of wallet max per trade. (Writes `bots/botN/size_mode.json`.) |
| **RPC node** | Set `RPC_URL=...` in `.env` to route through a custom node, or leave blank to use Helius. Re-run `python3 setup.py` to set it interactively. |
| **API keys / capital / wallet path** | Re-run `python3 setup.py` anytime — it keeps your current values as defaults. |
| **Pause / deploy a bot** | Dashboard buttons (unlock with your PIN first). |

Restart the fleet after changing `.env` (`Ctrl-C`, then `./run.sh`). Dashboard
size buttons take effect live — no restart needed.

## The one rule

**Never manually enable EV-sizing** (don't hand-write `bots/botN/ev_sizing.json`).
Sizing up an unproven edge loses money faster. The gates exist to arm sizing
*only* when the edge has proven itself on real results. Let them do their job.
This is rule #1 in [`CLAUDE.md`](CLAUDE.md) too.

## Optional extras

- **Reboot-durable uptime (macOS):** `deploy/README.md` sets up LaunchAgents so
  the fleet survives reboots and crashes.
- **Public Telegram bridge:** `telegram/` broadcasts a sanitized fleet pulse.
- **Advisory Claude agents:** `agents/` runs hourly analysts (needs an
  `ANTHROPIC_API_KEY`). Advisory only — they never touch your funds.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Dashboard won't load | Port 8080 in use, or `run.sh` exited — check the terminal output. |
| "0 candidates" / bots idle | Free data-source rate limits, or a genuinely quiet market. It self-recovers; see `discovery_service.py`. |
| No trades happening | The bot is selective by design. Check the `SCOUT` tab for skip reasons. |
| `int | None` syntax error | You're on Python <3.9 — upgrade. |
| Wallet shows 0 / no trades | The bot only trades the SOL actually in `KEYPAIR_PATH`'s wallet. Fund it. |

Stuck? Open the repo in Claude Code and ask — `CLAUDE.md` makes it a capable
co-pilot for this exact codebase.
