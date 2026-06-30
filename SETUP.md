# Setting up your own PIPBOI

This walks you from zero to a running bot on the dashboard. Budget ~15 min. If you
have Claude Code, you can also just say *"help me set up this repo"* — the included
[`CLAUDE.md`](CLAUDE.md) gives it everything it needs to do this with you.

> ⚠️ **Before you start:** this trades real money on volatile markets and can lose
> all of it. Use a **brand-new wallet** funded with an amount you are 100% okay
> losing. Read the disclaimer in [README.md](README.md) and [SECURITY.md](SECURITY.md).

---

## 1. Prerequisites

- **Python 3.9 or newer** — check with `python3 --version`
- **macOS or Linux** (on **Windows**, use [WSL](https://learn.microsoft.com/windows/wsl/install) —
  install Ubuntu, then follow these steps inside it)
- A free **Helius** account → https://dashboard.helius.dev

That's it. You do **not** need the Solana CLI or to create a wallet by hand — the
dashboard generates one for you (the bring-your-own-keypair path is in step 6).

## 2. Clone & install

```bash
git clone https://github.com/Onasidequestt/pipboi.git
cd pipboi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Get a Helius API key

1. Go to https://dashboard.helius.dev → create a project.
2. Copy the **API key**. The free tier is enough to begin.

## 4. Run it

```bash
./pipboi run        # (or ./run.sh) — starts the discovery sidecar, dashboard, and bot
```

It prints your **dashboard PIN** (you'll use it to unlock admin actions). Then open:

```
http://localhost:8080
```

## 5. Finish setup in the browser

On first run the dashboard shows a **setup page**. Enter:

- your **dashboard PIN** (printed in the terminal by `./pipboi run`),
- your **Helius** API key *(required — link to get one is on the page)*,
- your **Bitquery** key *(optional)*,

…and click **Save & continue**. Then restart (`Ctrl-C`, `./pipboi run`) and reload —
the full dashboard appears.

> **Prefer not to use the browser?** Two equivalent alternatives:
> - **Terminal wizard:** `python3 src/setup.py` (interactive, writes `.env`).
> - **Ask your Claude:** open the repo in Claude Code and say *"set this up for me."*
>
> Re-run `python3 src/setup.py` anytime to change settings.

## 6. Get a wallet trading

In the dashboard, the bot starts **undeployed**. Unlock with your PIN and click
**Activate** — PIPBOI **generates a fresh trading wallet and shows you its address.**
**Fund that address** with a small amount of SOL (e.g. 0.1–0.5 to start). The bot
sizes every trade from the live balance, so a small balance = small trades, and it
begins trading on the next cycle.

> **Advanced — bring your own keypair instead of letting PIPBOI generate one:**
> create a fresh keypair with the official Solana CLI and point `KEYPAIR_PATH` at it.
> ```bash
> sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"   # official Anza installer
> solana-keygen new --outfile ~/.config/solana/pipboi.json
> ```
> Then set `KEYPAIR_PATH=~/.config/solana/pipboi.json` in `.env` (or via the wizard)
> and **never reuse your main wallet.**

Start by just **watching**: the dashboard shows every candidate scored, every trade
opened, and every close *with the reason why*. Get a feel for it before you scale.

> **On Linux:** `./run.sh` auto-detects the missing macOS `caffeinate` and runs the
> dashboard directly — no extra steps. (You can also run the pieces by hand:
> `python3 src/discovery_service.py &` then `python3 src/dashboard.py`.)

---

## Operating it

- **Dashboard tabs:** `SCOUT` (what it's seeing), `TRADE` (live positions),
  `LEDGER` (every closed trade + why), `GENE` (the bot's internals + the gate).
- **Restart:** `Ctrl-C` in the `./pipboi run` terminal, then `./pipboi run` again.
- **Check status anytime (read-only):** `./pipboi` (or `./pipboi status --trades 8`).
- **Read-only health/analysis tools** (safe to run anytime):
  ```bash
  ./pipboi prestige_tracker     # balance, the deploy gate, days-to-goal
  ./pipboi edge_report --by-play # realized edge by strategy, fee-aware
  ./pipboi kill_criterion        # the pre-registered "is the edge real?" verdict
  ```
- **Run the test suite:** `./pipboi test`

## Changing settings (the easy knobs)

| What | How |
|---|---|
| **Max trade size** | In the dashboard, use the **size buttons** (`small`/`medium`/`large`). |
| **RPC node** | Set `RPC_URL=...` in `.env`, or re-run `python3 src/setup.py`. |
| **API keys / capital / wallet path** | Re-run `python3 src/setup.py` anytime — it keeps your current values as defaults. |
| **Pause / deploy the bot** | Dashboard buttons (unlock with your PIN first). |

Restart after changing `.env`. Dashboard size buttons take effect live — no restart.

## The one rule

**Never manually enable EV-sizing** (don't hand-write `src/bots/botN/ev_sizing.json`).
Sizing up an unproven edge loses money faster. The gates exist to arm sizing *only*
when the edge has proven itself on real results. Let them do their job. This is rule
#1 in [`CLAUDE.md`](CLAUDE.md) too.

## Optional extras

- **Reboot-durable uptime (macOS):** `./deploy/install.sh` sets up LaunchAgents so
  the bot survives reboots and crashes. See [`deploy/README.md`](deploy/README.md).
- **Advisory Claude agents:** `src/agents/` runs hourly analysts (needs an
  `ANTHROPIC_API_KEY`). Advisory only — they never touch your funds.

## Troubleshooting

**First thing to try — the built-in checkup:**

```bash
./pipboi doctor
```

It checks your Python version, dependencies, `.env`, API key, the dashboard PIN,
the wallet, and the port — and tells you the **one command** to fix whatever's off.

| Symptom | Likely cause / fix |
|---|---|
| Dashboard won't load | Port 8080 in use, or `run.sh` exited — check the terminal output, or run `./pipboi doctor`. |
| `ModuleNotFoundError` on start | Dependencies not installed — `pip install -r requirements.txt` (in your venv). |
| `SyntaxError` on start | You're on Python older than 3.9 — upgrade to 3.9+ (`python3 --version`). |
| "0 candidates" / bot idle | Free data-source rate limits, or a quiet market. It self-recovers. |
| No trades happening | The bot is selective by design. Check the `SCOUT` tab for skip reasons. |
| Forgot your dashboard PIN | It's in `.env` (`DASHBOARD_PIN=`), and `./pipboi doctor` prints it. |
| Wallet shows 0 / no trades | The bot only trades the SOL actually in your wallet. Fund it. |

Stuck? Open the repo in Claude Code and ask — `CLAUDE.md` makes it a capable
co-pilot for this exact codebase.
