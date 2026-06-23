# $Vault — Telegram bridge

A **read-only, stdlib-only** public window into the fleet, Pip-Boy themed. It
broadcasts **sanitized** fleet pulses to a public channel and answers a fixed set
of slash-commands in the linked discussion group.

## Safety boundary (identical to the S77 advisory agents)
- Reads ONLY `bots/botN/status.json`, `bots/botN/trades.db` (sqlite **read-only,
  `mode=ro`**), `race_start.json`, `shared_memory/sidecar_heartbeat.json`.
- **Never imports the trading core**, never writes to `bots/`, genes, canaries,
  keys, or `trades.db`. Its only writes are `telegram/state.json` + logs.
- `.env` is parsed for the **four `TELEGRAM_*` keys ONLY** — no other secret is
  ever read into the process, let alone a message.
- Every outbound message passes through `SANITIZE` (bot.py): no raw ◎ balances,
  no wallet linkage, no exact token mints. That dial is how we widen transparency
  later.

## One-time setup (the part that needs your phone)
1. In Telegram, message **@BotFather** → `/newbot` → name it (e.g. `Vault TEC`) →
   username (e.g. `YourBot_bot`). Copy the **token**.
2. **@BotFather → /setprivacy → your bot → Enable.** (Privacy ON = the bot only
   sees `/commands`, not every message — smaller abuse surface.)
3. Create a **public Channel** (New Channel → Public → pick a link, e.g.
   `t.me/YourChannel`). This link is what you share.
4. Channel → **Add the bot as Administrator** (Post Messages permission).
5. Channel → Settings → **Discussion** → create/link a **public group** (this is
   where people comment & run commands). Add the bot to that group too (admin is
   fine; privacy stays ON).
6. Get your own admin id: DM the bot `/id` after it's serving, or message
   `@userinfobot`.
7. Put the values in `~/solana-trader/.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHANNEL_ID=@YourChannel          # or the numeric -100… id
   TELEGRAM_GROUP_ID=-100…                # optional, reference only
   TELEGRAM_ADMIN_IDS=11111111            # your id; comma-separate for more
   ```

## Verify, then run
```bash
cd ~/solana-trader
python3 telegram/bot.py --dry-run     # render every panel locally, send nothing
python3 telegram/bot.py --whoami      # confirm the token (getMe)
python3 telegram/bot.py --test        # getChat + a "link established" test post
python3 telegram/bot.py --broadcast   # post one real fleet-pulse now

# durable serve (survives terminal close; restarts on crash):
nohup ./telegram/loop.sh >> logs/telegram.log 2>&1 &
```
Stop / fully revert:
```bash
pkill -f telegram/loop.sh ; pkill -f "telegram/bot.py --serve"
```

## Commands
Public: `/race` `/status` `/gate` `/regime` `/edge` `/about` `/help` · `/id`
(anyone can learn their own id).
Admin (ids in `TELEGRAM_ADMIN_IDS`): `/ping`, `/broadcast` (force a pulse),
`/say <text>` (post operator text to the channel).

Operator control from the box (how Claude / you drive it):
```bash
python3 telegram/bot.py --say "gm vault dwellers ⚡"
python3 telegram/bot.py --broadcast
```

## 🔒 Disclosure boundary (what is NEVER said in public)

The bot answers a **fixed command table only** — no free-form Q&A, so it cannot be
prompt-injected into leaking anything. When the operator (or Claude via `--say`)
answers a direct question, these are **never** disclosed:

**NEVER reveal**
- Wallet addresses or any link between a `UNIT-0x` alias and a real wallet
- Exact ◎ balances, exact SOL P&L, or total capital
- The specific token mints being held or traded (front-running risk)
- Exact entry thresholds / scores / regime cutoffs / sizing math (the actual alpha)
- The bot token, `.env` contents, API keys (Helius/Birdeye/LunarCrush), dashboard
  PIN/secret, keypair paths, server/host, tunnels (ngrok/cloudflare), file paths
- The operator's real identity, location, or any personal info
- Anything that lets someone copy or front-run the live edge

**OK to share** (all already sanitized in the panels)
- Race standings as rank / progress-% / Δ% · current regime label · gate status
  (n/15, ghost-rate, net sign) · liveness · the edge *thesis* (not its parameters)
- That it's open-source-once-proven, self-host, not a fund (`/invest`)

If a question can't be answered without crossing the line, deflect to the thesis
and `/gate` ("watch it prove itself in the open"). Never confirm a guess about a
hidden value — declining IS the safe answer.

## Hardening (live)
- **Token redaction** — the token is stripped from every log line / error string
  (`_redact`); a urllib exception can otherwise carry the API URL with the token.
- **429 handling** — honors Telegram's `retry_after`; a flood backs the bot off
  instead of getting it rate-limit-banned.
- **Global send throttle** (~16 msg/s) + **per-user 4s cooldown** (anti-spam).
- **8s TTL cache** on `status()`/`_closes()` — a flood of `/race` collapses to one
  disk read, not thousands.
- **Generic errors to users** (paths/exceptions go to the log only).
- **Blocklist** — `TELEGRAM_BLOCK_IDS=id,id` in `.env` silently mutes abusers
  (restart the loop to apply).
- **Backlog drain on fresh deploy** + bounded anti-spam map (no memory leak).
- **Privacy mode ON** — in groups the bot only sees `/commands`, not chatter.

## 🛡️ Discussion-group spam defense (operator playbook)

Our bot defends its own DM surface (cooldown → auto-mute → blocklist). The GROUP is
defended by Telegram-native settings + a dedicated mod bot. Do this once:

**A. Telegram-native (group → Edit / Permissions):**
- **Slow Mode** → 30s–1m (one message per user per interval; kills flood instantly).
- **Permissions** → turn OFF *Add Members* for normal members (stops mass-add spam).
- Keep the group linked as the channel's Discussion group.

**B. Add a moderation bot — Rose (`@MissRose_bot`, free):**
1. Add `@MissRose_bot` to the group → **promote to admin** with *Delete Messages*,
   *Ban Users*, *Restrict Members* (it doesn't need anything else).
2. Enable **captcha-on-join** (new accounts must verify before they can post — this
   alone stops most bot-account spam). Configure via Rose's inline settings: DM
   `@MissRose_bot` → *Settings* → pick the group → **Captcha** → on (button/math).
3. Enable **antiflood** (auto-mute/ban on rapid repeats), **blocklists** (scam
   words/URLs auto-deleted), and **warns**. Same Settings panel, or in-group `/help`.
4. Optional: enable Rose's **anti-spam / report** features; for crypto, consider
   **Combot (`@combot`)** instead/alongside for its CAS global spammer ban-list.

Vault bot + Rose run side-by-side fine (Vault = read-only info, Rose = moderation).
Reconfirm exact command syntax via the bot's own `/help` / settings — it evolves.

## Tuning
- `SANITIZE` (bot.py) — the transparency dial.
- `BROADCAST_INTERVAL` / `FORCE_INTERVAL` — pulse cadence (default: ≤hourly, ≥6h).
- `CMD_COOLDOWN` — per-user anti-spam.
- `COMMANDS_IN_GROUPS` — default `False`: public commands answered in **DMs only**
  (group stays human, dodges TG's per-group msg cap). Admin commands work anywhere.
- `BOT_ALIAS` / `BOT_MODE` — public bot names.
