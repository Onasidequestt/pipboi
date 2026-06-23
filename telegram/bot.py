#!/usr/bin/env python3
"""telegram/bot.py — $VAULT public Telegram bridge.

A read-only, stdlib-ONLY window into the autonomous Solana fleet, themed in the
Pip-Boy / $Vault green-phosphor style. It broadcasts SANITIZED fleet pulses
to a public channel and answers a fixed set of slash-commands in the linked
discussion group.

SAFETY BOUNDARY (mirrors the S77 advisory agents — see agents/README.md):
  • Reads ONLY: bots/botN/status.json, bots/botN/trades.db (sqlite, read-only),
    race_start.json, shared_memory/sidecar_heartbeat.json.
  • NEVER imports the trading core (main/observer/stoic_strategy/…).
  • NEVER reads .env secrets into any message — it reads ONLY the few keys it needs
    (the bot token + chat ids) and nothing else is ever rendered.
  • NEVER writes to bots/, genes, canaries, keys, or trades.db. Its only writes are
    its own telegram/state.json (dedupe cursor) and telegram/*.log.
  • Outbound messages pass through SANITIZE — no raw ◎ balances, no wallet linkage.

ZERO third-party deps (python 3.9, no `requests`): pure urllib.

Run modes:
  python3 telegram/bot.py --test       # validate token + channel, send a test post
  python3 telegram/bot.py --dry-run    # render the digest to stdout, send nothing
  python3 telegram/bot.py --broadcast  # post one fleet-pulse to the channel now
  python3 telegram/bot.py --say "txt"  # post arbitrary operator text to the channel
  python3 telegram/bot.py --serve      # long-poll: answer commands + periodic pulse
  python3 telegram/bot.py --whoami     # print bot identity (getMe)

Config (from ~/solana-trader/.env, or real env):
  TELEGRAM_BOT_TOKEN     required — from @BotFather
  TELEGRAM_CHANNEL_ID    required — '@PublicName' or numeric '-100…' (the broadcast channel)
  TELEGRAM_GROUP_ID      optional — the linked discussion group id (for reference only)
  TELEGRAM_ADMIN_IDS     optional — comma list of Telegram user-ids allowed admin commands
"""
import os, sys, json, time, html, sqlite3, argparse, urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent          # ~/solana-trader
HERE = Path(__file__).resolve().parent                 # ~/solana-trader/telegram
STATE_FILE = HERE / "state.json"

# ── gate constants (kept in lock-step with prestige_tracker.py) ──────────────────
GOAL_SOL   = 2.0
GHOST_MAX  = 0.10
MIN_CLOSES = 15
_DP_PLAYS  = {"deep_pool", "brain_rule"}
BOTS       = (1, 2, 3)

# ── SANITIZE: the transparency dial (we tune this together later) ───────────────
# Defaults err to the safe side per the operator's "sanitized stats" choice:
# race standings + % returns + regime + gate + activity are public; raw ◎ balances
# and wallet linkage are NOT. Flip a value to widen what the public sees.
SANITIZE = {
    "show_progress_to_goal": True,   # the race bar/% toward ◎2.0 (a ratio, not a raw balance)
    "show_return_pct":       True,   # Δ% since the race start line
    "show_raw_sol":          False,  # exact ◎ balances — kept OFF
    "show_wallets":          False,  # wallet addresses — kept OFF
    "show_token_mints":      False,  # which exact tokens are held — kept OFF
}

# Public-facing bot aliases (decoupled from wallet identity).
BOT_ALIAS = {1: "UNIT-01", 2: "UNIT-02", 3: "UNIT-03"}
BOT_MODE  = {1: "INSANE", 2: "WILD", 3: "STOIC"}

TG_API = "https://api.telegram.org/bot{token}/{method}"


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
def _load_env():
    """Parse ~/solana-trader/.env for ONLY the telegram keys. Real env overrides."""
    cfg = {}
    envf = ROOT / ".env"
    wanted = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID",
              "TELEGRAM_GROUP_ID", "TELEGRAM_ADMIN_IDS", "TELEGRAM_BLOCK_IDS")
    try:
        for line in envf.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k in wanted:
                cfg[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    for k in wanted:                       # real-env wins over file
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = _load_env()
TOKEN   = CFG.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = CFG.get("TELEGRAM_CHANNEL_ID", "")
ADMINS  = {x.strip() for x in CFG.get("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}
BLOCK   = {x.strip() for x in CFG.get("TELEGRAM_BLOCK_IDS", "").split(",") if x.strip()}


def _require_token():
    if not TOKEN:
        sys.exit("✗ TELEGRAM_BOT_TOKEN not set. Add it to ~/solana-trader/.env "
                 "(see telegram/README.md).")


def _redact(s):
    """Strip the bot token (and any token-shaped substring) from a string before it
    is EVER logged or returned — a urllib exception can carry the full API URL."""
    s = str(s)
    if TOKEN:
        s = s.replace(TOKEN, "<TOKEN>")
    return s


def _log(msg):
    print(f"[tec-tg] {_redact(msg)}", flush=True)


# ── TTL cache: collapse a flood of identical reads into one disk hit ─────────────
def ttl_cache(seconds):
    def deco(fn):
        store = {}
        def wrap(*a):
            now = time.time()
            hit = store.get(a)
            if hit and (now - hit[0]) < seconds:
                return hit[1]
            val = fn(*a)
            store[a] = (now, val)
            if len(store) > 256:                       # opportunistic prune
                for k in [k for k, v in store.items() if now - v[0] > seconds]:
                    store.pop(k, None)
            return val
        return wrap
    return deco


# ── global send pacing (Telegram allows ~30 msg/s overall; stay well under) ──────
_SEND_MIN_INTERVAL = 0.06
_last_send = [0.0]


def _throttle():
    wait = _SEND_MIN_INTERVAL - (time.time() - _last_send[0])
    if wait > 0:
        time.sleep(wait)
    _last_send[0] = time.time()


# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM API (stdlib urllib)
# ═══════════════════════════════════════════════════════════════════════════════
def api(method, params=None, timeout=60, _retry=True):
    """Call a Bot API method. Returns the parsed JSON dict (or {'ok':False,...}).
    Honors Telegram 429 retry_after; NEVER leaks the token in an error string."""
    _require_token()
    url = TG_API.format(token=TOKEN, method=method)
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"ok": False, "description": f"HTTP {e.code}"}
        # 429 Too Many Requests → respect the server's cool-off, retry once
        if e.code == 429 and _retry:
            ra = (body.get("parameters") or {}).get("retry_after", 1)
            time.sleep(min(float(ra), 30) + 0.5)
            return api(method, params, timeout, _retry=False)
        if "description" in body:
            body["description"] = _redact(body["description"])
        return body
    except Exception as e:
        return {"ok": False, "description": _redact(e)}


def send(chat_id, text, parse_mode="HTML", preview=False, reply_to=None):
    if not chat_id:
        return {"ok": False, "description": "no chat_id"}
    _throttle()
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
         "disable_web_page_preview": "false" if preview else "true"}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return api("sendMessage", p)


# ═══════════════════════════════════════════════════════════════════════════════
#  READ-ONLY STATE  (no core imports; same boundary as the advisory agents)
# ═══════════════════════════════════════════════════════════════════════════════
def _load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


@ttl_cache(8)
def status(b):
    return _load_json(ROOT / f"bots/bot{b}/status.json", {}) or {}


@ttl_cache(8)
def _closes(b):
    """Read-only pull of all close events from bot b's ledger."""
    out = []
    try:
        c = sqlite3.connect(f"file:{ROOT}/bots/bot{b}/trades.db?mode=ro", uri=True)
        for (d,) in c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid"):
            try:
                out.append(json.loads(d))
            except Exception:
                pass
        c.close()
    except Exception:
        pass
    return out


def _is_ghost(r):
    if r.get("ghost"):
        return True
    return abs(r.get("pnl", 0.0)) < 1e-9 and r.get("pnl_sol", 0.0) < -0.02


def gate_stats():
    """Replicates prestige_tracker._fleet_deep_pool_stats (clean-era net). Returns
    (n, net, ghosts, ghost_rate) — net is QUALITATIVE downstream (never printed raw)."""
    n = ghosts = 0
    net = 0.0
    for b in BOTS:
        for r in _closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play in _DP_PLAYS:
                n += 1
                if _is_ghost(r):
                    ghosts += 1
                else:
                    net += r.get("pnl_sol", 0.0) or 0.0
    gr = (ghosts / n) if n else 0.0
    return n, net, ghosts, gr


def regime():
    """Current market regime label from the lead bot's observer telemetry."""
    th = status(1).get("thinking", {}) or {}
    ms = th.get("market_state") or th.get("tier1_observer") or {}
    return str(ms.get("regime", "—")).upper()


def closes_last_24h(b):
    cnt = 0
    cutoff = time.time() - 86400
    for r in _closes(b):
        t = _ts(r.get("ts"))
        if t and t >= cutoff:
            cnt += 1
    return cnt


def race_rows():
    """Per-bot race state. Sanitized: progress-to-goal % + Δ% since start, NO raw ◎."""
    start = _load_json(ROOT / "race_start.json", {}) or {}
    start_tot = start.get("start_total", {})
    rows = []
    for b in BOTS:
        s = status(b)
        liq = float(s.get("sol_balance", 0) or 0)
        intr = float(s.get("sol_in_trades", 0) or 0)
        total = liq + intr
        st0 = float(start_tot.get(str(b), total) or total)
        gf = _load_json(ROOT / f"bots/bot{b}/ev_sizing.json")
        if not gf or not gf.get("enabled"):
            gene = "C² flat"
        else:
            sc = gf.get("scale")
            gene = f"EV×{sc}" if sc else "EV full"
        rows.append({
            "bot": b,
            "alias": BOT_ALIAS.get(b, f"UNIT-0{b}"),
            "mode": BOT_MODE.get(b, "?"),
            "progress": total / GOAL_SOL,
            "delta_pct": ((total - st0) / st0 * 100.0) if st0 else 0.0,
            "open": len(s.get("open_positions", {}) or {}),
            "gene": gene,
            "live": _is_live(s),
        })
    rows.sort(key=lambda r: r["progress"], reverse=True)
    return rows


def _is_live(s):
    """A bot counts as live if its status.json updated within the last ~5 min."""
    t = _ts(s.get("last_update"))
    return bool(t and (time.time() - t) < 300)


def fleet_heartbeat_age():
    hb = _load_json(ROOT / "shared_memory/sidecar_heartbeat.json", {}) or {}
    t = _ts(hb.get("ts"))
    return (time.time() - t) if t else None


# ═══════════════════════════════════════════════════════════════════════════════
#  PIP-BOY THEMING + RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════
def _bar(frac, width=12):
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def _pre(body):
    """Wrap a monospace panel; escape so arbitrary content can't break HTML."""
    return "<pre>" + html.escape(body) + "</pre>"


def _utc():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def _gate_label():
    n, net, ghosts, gr = gate_stats()
    net_ok = net >= 0
    n_ok = n >= MIN_CLOSES
    g_ok = gr <= GHOST_MAX
    state = "ARMED ✓" if (net_ok and n_ok and g_ok) else "warming"
    netq = "net +" if net > 1e-6 else ("net −" if net < -1e-6 else "net flat")
    return (f"EDGE GATE: {state}  "
            f"({n}/{MIN_CLOSES} trades {'✓' if n_ok else '…'} · "
            f"ghost {gr:.0%} {'✓' if g_ok else '✗'} · {netq})")


def digest():
    """The sanitized fleet-pulse panel (HTML <pre>)."""
    rows = race_rows()
    live_n = sum(1 for r in rows if r["live"])
    open_n = sum(r["open"] for r in rows)
    trades_24h = sum(closes_last_24h(b) for b in BOTS)
    L = []
    L.append("⚡ V A U L T   T . E . C.")
    L.append("Trading · Energy · Command")
    L.append("=" * 34)
    L.append("")
    L.append("🏁 PRESTIGE RACE  →  ◎2.0")
    for i, r in enumerate(rows, 1):
        line = f"{i}. {r['alias']:<7} "
        if SANITIZE["show_progress_to_goal"]:
            line += f"[{_bar(r['progress'])}] {r['progress']*100:4.0f}%"
        if SANITIZE["show_return_pct"]:
            line += f"  {r['delta_pct']:+.1f}%"
        L.append(line)
        L.append(f"   {r['mode']:<7} gene:{r['gene']}")
    L.append("   (warm-up lap — sizes equal until")
    L.append("    the edge is proven live)")
    L.append("")
    L.append(f"📡 REGIME : {regime()}")
    L.append(f"🧪 {_gate_label()}")
    L.append(f"🤖 FLEET  : {live_n}/3 live · {open_n} open · "
             f"{trades_24h} trades/24h")
    L.append("")
    L.append('"The one validated edge: liquidity')
    L.append(' filling a deep, sellable pool."')
    L.append("=" * 34)
    L.append(f"⏱ {_utc()}")
    return _pre("\n".join(L))


def digest_signature():
    """A coarse fingerprint so we only re-post when something MATERIAL changed
    (matches the operator's 'monitoring only, no noise' principle)."""
    rows = race_rows()
    n, net, ghosts, gr = gate_stats()
    order = "|".join(r["alias"] for r in rows)
    netq = "+" if net > 1e-6 else ("-" if net < -1e-6 else "0")
    return f"{order}::{regime()}::n{n}g{ghosts}{netq}::live{sum(1 for r in rows if r['live'])}"


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND RESPONSES (public, fixed dispatch table — no eval, ever)
# ═══════════════════════════════════════════════════════════════════════════════
def cmd_help():
    L = ["⚡ $VAULT — COMMANDS", "=" * 28,
         "/race    prestige-race standings",
         "/status  fleet liveness snapshot",
         "/gate    edge-proving gate status",
         "/regime  current market regime",
         "/edge    what edge we actually trade",
         "/about   what is $Vault",
         "/help    this menu"]
    return _pre("\n".join(L))


def cmd_about():
    L = ["⚡ $VAULT",
         "Trading · Energy · Command",
         "=" * 28,
         "An autonomous 3-bot Solana fleet running",
         "a fleet-level genetic algorithm: equal",
         "◎1.0 starts, one varying gene (SIZE),",
         "racing to ◎2.0. The winner's gene seeds",
         "the next generation.",
         "",
         "This channel is a SANITIZED live window —",
         "race standings, market regime, and the",
         "edge-proving gate. No wallets, no keys,",
         "no financial advice. Watch the machine",
         "prove its edge in the open.",
         "",
         "Type /help for commands."]
    return _pre("\n".join(L))


def cmd_edge():
    L = ["🎯 THE EDGE (the honest truth)", "=" * 28,
         "Pure price-action is structurally −EV",
         "(198 token-days of backtest say so).",
         "",
         "The ONE validated edge = liquidity",
         "FILLING into a deep, sellable pool:",
         "real accumulation you can actually exit.",
         "Draining pools trap you — those are the",
         "ghosts. The whole gate exists to prove",
         "this edge survives LIVE before sizing up.",
         "",
         "Biggest lever to ◎2.0 is SIZE — but it",
         "stays locked until the edge reads +EV live."]
    return _pre("\n".join(L))


def cmd_status():
    rows = race_rows()
    hb = fleet_heartbeat_age()
    L = ["🤖 FLEET STATUS", "=" * 28]
    for r in rows:
        dot = "🟢" if r["live"] else "🔴"
        L.append(f"{dot} {r['alias']:<7} {r['mode']:<7} "
                 f"{r['open']} open")
    L.append("")
    L.append(f"📡 regime : {regime()}")
    if hb is not None:
        L.append(f"💓 sidecar: {hb:.0f}s ago")
    L.append(f"⏱ {_utc()}")
    return _pre("\n".join(L))


def cmd_gate():
    n, net, ghosts, gr = gate_stats()
    net_ok, n_ok, g_ok = net >= 0, n >= MIN_CLOSES, gr <= GHOST_MAX
    netq = "positive" if net > 1e-6 else "negative" if net < -1e-6 else "flat"
    L = ["🧪 EDGE-PROVING GATE", "=" * 28,
         "Genes stay DORMANT (all bots flat-sized)",
         "until the live edge clears all three:",
         "",
         f"[{'PASS' if n_ok else 'WAIT'}] volume   {n}/{MIN_CLOSES} closes",
         f"[{'PASS' if g_ok else 'WAIT'}] ghost ≤ {GHOST_MAX:.0%}  {gr:.0%} ({ghosts}/{n})",
         f"[{'PASS' if net_ok else 'WAIT'}] net ≥ 0   {netq}",
         ""]
    if net_ok and n_ok and g_ok:
        L.append("⚡ GATE OPEN — the size race is LIVE.")
    else:
        L.append("Warm-up lap. The fleet self-arms the")
        L.append("moment all three read green.")
    L.append(f"⏱ {_utc()}")
    return _pre("\n".join(L))


def cmd_regime():
    L = ["📡 MARKET REGIME", "=" * 28,
         f"Current: {regime()}",
         "",
         "The observer runs a 5-regime ladder:",
         "EUPHORIA · AGGRESSIVE · NORMAL ·",
         "SNIPER · DEAD — each with its own entry",
         "bar and position sizing. Quiet tapes",
         "(sniper/dead) trade smaller and pickier.",
         f"⏱ {_utc()}"]
    return _pre("\n".join(L))


def cmd_invest():
    L = ["⚡ JOIN THE VAULT — SELF-HOST", "=" * 28,
         "$Vault is NOT a fund. We never take",
         "deposits and we never touch your SOL.",
         "",
         "The plan: you run YOUR OWN bot, on YOUR",
         "OWN wallet, with YOUR OWN keys — the same",
         "autonomous fleet you see racing here.",
         "Your funds, your control, your risk.",
         "",
         "Status: the code isn't public yet. The",
         "fleet is still PROVING its edge live",
         "(check /gate). We release once it's earned.",
         "",
         "⚠ Real talk: trading memecoins can lose",
         "100%. No profit is promised or implied.",
         "This is software, not financial advice. DYOR.",
         "",
         "→ /waitlist to get the drop when it's ready."]
    return _pre("\n".join(L))


WAITLIST_FILE = HERE / "waitlist.jsonl"


def _waitlist_ids():
    ids = set()
    try:
        for ln in WAITLIST_FILE.read_text().splitlines():
            try:
                ids.add(str(json.loads(ln).get("id")))
            except Exception:
                pass
    except Exception:
        pass
    return ids


def cmd_waitlist(uid="", uname=""):
    uid = str(uid)
    if uid and uid in _waitlist_ids():
        return _pre("✅ Already on the list, dweller.\n"
                    "Watch /gate — that's the green light.")
    try:
        rec = {"id": uid, "username": uname,
               "ts": datetime.now(timezone.utc).isoformat()}
        with open(WAITLIST_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return _pre("✅ You're on the list, dweller.\n"
                "We'll ping the channel when the code\n"
                "drops. Watch /gate — that's the green light.")


PUBLIC_CMDS = {
    "/start":    cmd_about,
    "/about":    cmd_about,
    "/help":     cmd_help,
    "/race":     digest,
    "/status":   cmd_status,
    "/gate":     cmd_gate,
    "/regime":   cmd_regime,
    "/edge":     cmd_edge,
    "/invest":   cmd_invest,
    "/code":     cmd_invest,
    "/waitlist": cmd_waitlist,   # special-cased in the handler (needs uid/uname)
}

# DM keywords that route an eager non-command message to the invest explainer.
_INVEST_HINTS = ("invest", "buy in", "buyin", "how do i join", "how to join",
                 "fund", "deposit", "put money", "get the code", "the code",
                 "spin up", "run my own", "waitlist")


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVE LOOP  (long-poll getUpdates + periodic pulse)
# ═══════════════════════════════════════════════════════════════════════════════
def _read_state():
    return _load_json(STATE_FILE, {}) or {}


def _write_state(d):
    try:
        STATE_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


# per-user command cooldown (anti-spam)
_last_cmd = {}
CMD_COOLDOWN = 4.0          # seconds per user
COMMANDS_IN_GROUPS = False  # keep groups purely human; public commands answered in DMs only
                            # (also dodges Telegram's ~20-msg/min per-group cap at scale)

# ── automatic spam escalation ───────────────────────────────────────────────────
# Tripping the cooldown is a "strike". Too many strikes in a short window → the user
# is silently auto-muted for a while (in-memory, temporary; clears on restart). This
# is the automated tier; TELEGRAM_BLOCK_IDS is the permanent operator tier.
_strike = {}                # uid -> (count, window_start)
_muted  = {}                # uid -> mute_until_ts
MUTE_STRIKES = 6            # cooldown violations…
MUTE_WINDOW  = 30.0         # …within this window…
MUTE_SECONDS = 600.0        # …earns this long a silent mute


def _allowed(uid):
    """Single rate-gate for every reply path: honors mute, cooldown, and escalation.
    Returns True (and stamps the user) only if a reply should be sent now."""
    now = time.time()
    mu = _muted.get(uid)
    if mu and now < mu:
        return False
    if now - _last_cmd.get(uid, 0) < CMD_COOLDOWN:
        cnt, ws = _strike.get(uid, (0, now))
        if now - ws > MUTE_WINDOW:
            cnt, ws = 0, now
        cnt += 1
        _strike[uid] = (cnt, ws)
        if cnt >= MUTE_STRIKES:
            _muted[uid] = now + MUTE_SECONDS
            _log(f"auto-muted {uid} for {int(MUTE_SECONDS)}s ({cnt} strikes)")
        return False
    _last_cmd[uid] = now
    return True
BROADCAST_INTERVAL = 3600   # post a pulse at most hourly…
FORCE_INTERVAL = 6 * 3600   # …but at least every 6h even if unchanged


def _handle_message(msg):
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    frm = msg.get("from") or {}
    uid = str(frm.get("id", ""))
    uname = frm.get("username") or frm.get("first_name") or ""
    mid = msg.get("message_id")
    is_private = chat.get("type") == "private"

    if uid in BLOCK:                       # muted abuser — silently ignore
        return

    if not text.startswith("/"):
        # Non-command. In groups, privacy mode means we never even see these. In DMs,
        # answer once (rate-limited): route eager "invest" chatter to /invest, else a
        # gentle pointer so a real person isn't met with silence.
        if is_private and _allowed(uid):
            if any(h in text.lower() for h in _INVEST_HINTS):
                send(chat_id, cmd_invest(), reply_to=mid)
            else:
                send(chat_id, _pre("⚡ $Vault terminal.\n"
                                   "Try /help · /race · /gate · /invest"),
                     reply_to=mid)
        return

    # strip "/cmd@BotName" → "/cmd", lowercase, first token only
    cmd = text.split()[0].split("@")[0].lower()
    arg = text[len(text.split()[0]):].strip()

    # admin commands
    if cmd in ("/say", "/broadcast", "/id", "/ping"):
        if uid not in ADMINS:
            if cmd == "/id":  # everyone may learn their own id
                return send(chat_id, _pre(f"your telegram id: {uid}"), reply_to=mid)
            return send(chat_id, _pre("⛔ admin only"), reply_to=mid)
        if cmd == "/id":
            return send(chat_id, _pre(f"your telegram id: {uid} (admin ✓)"), reply_to=mid)
        if cmd == "/ping":
            return send(chat_id, _pre("pong ⚡"), reply_to=mid)
        if cmd == "/broadcast":
            return send(CHANNEL, digest())
        if cmd == "/say":
            if not arg:
                return send(chat_id, _pre("usage: /say <text>"), reply_to=mid)
            return send(CHANNEL, _pre(arg))

    # public commands — DMs only (keep the group human), rate limited per user
    if not (is_private or COMMANDS_IN_GROUPS):
        return
    fn = PUBLIC_CMDS.get(cmd)
    if not fn:
        return
    if not _allowed(uid):
        return
    try:
        body = cmd_waitlist(uid, uname) if cmd == "/waitlist" else fn()
        send(chat_id, body, reply_to=mid)
    except Exception as e:
        _log(f"render error on {cmd}: {e}")        # detail to log (redacted), not to user
        send(chat_id, _pre("⚠ systems busy — try again in a moment."))


def maybe_broadcast(st):
    """Post a pulse if the signature changed (or FORCE_INTERVAL elapsed)."""
    if not CHANNEL:
        return st
    now = time.time()
    sig = digest_signature()
    last_t = st.get("last_broadcast_t", 0)
    last_sig = st.get("last_sig", "")
    if (now - last_t) < BROADCAST_INTERVAL:
        return st
    changed = (sig != last_sig)
    if changed or (now - last_t) >= FORCE_INTERVAL:
        r = send(CHANNEL, digest())
        if r.get("ok"):
            st["last_broadcast_t"] = now
            st["last_sig"] = sig
            _write_state(st)
    return st


def serve():
    _require_token()
    me = api("getMe").get("result", {})
    _log(f"serving as @{me.get('username','?')} · channel={CHANNEL} · "
         f"admins={sorted(ADMINS) or '—'} · blocked={len(BLOCK)}")
    if not CHANNEL:
        _log("⚠ TELEGRAM_CHANNEL_ID unset — broadcasts disabled (commands still work).")
    st = _read_state()
    offset = st.get("offset", 0)
    if not offset:                                  # fresh deploy → skip any backlog
        for upd in api("getUpdates", {"timeout": 0, "offset": -1}).get("result", []):
            offset = max(offset, upd["update_id"] + 1)
        _log(f"backlog drained → offset {offset}")
    st = maybe_broadcast(st)
    while True:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 50,
                                   "allowed_updates": json.dumps(["message"])},
                    timeout=70)
            for upd in r.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message")
                if msg:
                    try:
                        _handle_message(msg)
                    except Exception as e:
                        _log(f"handler error: {e}")
            st["offset"] = offset
            _write_state(st)
            st = maybe_broadcast(st)
            if len(_last_cmd) > 5000:               # bound the anti-spam maps
                cut = time.time() - 3600
                for k in [k for k, v in _last_cmd.items() if v < cut]:
                    _last_cmd.pop(k, None)
                _strike.clear()
            now_p = time.time()                     # drop expired mutes
            for k in [k for k, v in _muted.items() if v < now_p]:
                _muted.pop(k, None)
        except KeyboardInterrupt:
            _log("stopped.")
            break
        except Exception as e:
            _log(f"loop error: {e} — backing off 10s")
            time.sleep(10)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="$Vault Telegram bridge")
    ap.add_argument("--test", action="store_true", help="validate token+channel, send a test post")
    ap.add_argument("--dry-run", action="store_true", help="render digest to stdout, send nothing")
    ap.add_argument("--broadcast", action="store_true", help="post one fleet-pulse now")
    ap.add_argument("--say", metavar="TEXT", help="post arbitrary operator text to the channel")
    ap.add_argument("--serve", action="store_true", help="long-poll: commands + periodic pulse")
    ap.add_argument("--whoami", action="store_true", help="print bot identity (getMe)")
    args = ap.parse_args()

    if args.dry_run:
        # render with no network — pure local read (unescape for a faithful preview)
        for label, fn in (("DIGEST/race", digest), ("status", cmd_status),
                          ("gate", cmd_gate), ("regime", cmd_regime),
                          ("edge", cmd_edge), ("about", cmd_about), ("help", cmd_help)):
            body = html.unescape(fn().replace("<pre>", "").replace("</pre>", ""))
            print(f"\n┌── {label} " + "─" * (50 - len(label)))
            print(body)
        print("\n--- signature:", digest_signature())
        return

    if args.whoami:
        print(json.dumps(api("getMe"), indent=2))
        return

    if args.test:
        _require_token()
        print("getMe   :", json.dumps(api("getMe").get("result", {}), indent=2))
        if CHANNEL:
            ch = api("getChat", {"chat_id": CHANNEL})
            print("getChat :", json.dumps(ch.get("result", ch), indent=2))
            r = send(CHANNEL, _pre("⚡ $Vault link established. Systems nominal."))
            print("test post:", "OK" if r.get("ok") else r)
        else:
            print("⚠ TELEGRAM_CHANNEL_ID unset — skipping channel test.")
        return

    if args.broadcast:
        print(send(CHANNEL, digest()))
        return

    if args.say:
        print(send(CHANNEL, _pre(args.say)))
        return

    if args.serve:
        serve()
        return

    ap.print_help()


if __name__ == "__main__":
    main()
