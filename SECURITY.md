# Security

PIPBOI is a **self-hosted** trading bot: it runs entirely on *your* machine, on *your* wallet,
with *your* API keys. Nothing about it phones home. This page explains exactly what touches your
money and your secrets, so you can read the code and trust it before you run it.

> **TL;DR** — There is no server, no account, no telemetry. Your keys and wallet stay in files on
> your computer that are git-ignored and never transmitted. The bot can only ever spend the SOL in
> the one wallet you point it at. Read the code; run it on a fresh wallet you funded small.

## What holds your secrets, and where they live

| Secret | Lives in | Committed to git? |
|---|---|---|
| API keys (Helius, Bitquery), dashboard PIN | `.env` (repo root) | **No** — git-ignored |
| Wallet private key | `KEYPAIR_PATH` (default `~/.config/solana/pipboi.json`) or `src/bots/botN/keypair.json` | **No** — git-ignored |
| Trade history / balances / runtime state | `src/bots/`, `src/shared_memory/`, `*.db` | **No** — git-ignored |

The [`.gitignore`](.gitignore) is configured to exclude all of these (`.env*`, `**/keypair.json`,
`id.json`, `*.key`, `*.pem`, `*.db`, runtime state). **Keep it that way.** This repository has been
scanned and contains **no** private keys, API keys, mnemonics, or wallet addresses — only public
Solana program/mint IDs (wrapped SOL, USDC, etc.), which are not secrets.

## What can move money

- **Only the bot's own trade loop**, and only with the wallet at `KEYPAIR_PATH`. It buys/sells SPL
  tokens via Jupiter swaps. It has no withdrawal path, no transfer-to-arbitrary-address feature, and
  no way to touch any wallet other than the one you configure.
- Funds are bounded by what you deposit. **Fund a fresh wallet with an amount you can lose
  entirely.** The bot sizes trades as a small fraction of that wallet's live balance.
- The dashboard's admin actions (deploy/pause/size) are gated behind a **PIN** generated locally
  into your `.env`.

## Network exposure

- The dashboard binds to **`localhost:8080`** only. It is not exposed to the internet by default,
  and this repo ships **no** tunneling (no ngrok, no Cloudflare tunnel). If you choose to expose it,
  that is on you — put it behind auth and a TLS proxy.
- Outbound calls go only to public APIs you opt into: Helius RPC, Jupiter, DexScreener,
  GeckoTerminal, and (optional) Bitquery. No analytics, no callbacks to us.

## The "operate with Claude" angle

A [`CLAUDE.md`](CLAUDE.md) ships so you can run the bot conversationally. Its rules tell the
assistant to **never print/commit/paste `.env`, keypairs, or `*.db`**, to default to read-only
analysis, and to never push or expose anything without your say-so. If you point an AI assistant at
this repo, it operates under those boundaries — but you remain in control of every funded action.

## Hardening checklist (recommended)

- Use a **dedicated, fresh** trading wallet — never your main one.
- Start in observation: run it, watch the dashboard, read the ledger before scaling anything.
- Keep `.env` permissions tight (`chmod 600 .env` — the setup wizard does this for you).
- Back up your keypair somewhere safe; if it's lost, the funds are lost.
- Review `requirements.txt` and the execution code (`src/wallet.py`, `src/jupiter.py`,
  `src/helius.py`, `src/safety.py`) before going live.

## Reporting a vulnerability

This is experimental, self-hosted software with no managed service behind it. If you find a security
issue in the code, please open a GitHub issue (for non-sensitive reports) or contact the maintainer
privately for anything that could put users' funds at risk. There is no bug-bounty program.

## No warranty

PIPBOI is provided under the [MIT License](LICENSE), **as-is, with no warranty**, and is **not
financial advice**. It trades real money on extremely volatile markets and can lose all of it. You
are solely responsible for your keys, your funds, and your decisions.
