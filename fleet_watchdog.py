#!/usr/bin/env python3
"""
fleet_watchdog.py — recurring fleet health + orphan auto-cleanup.

WHY THIS EXISTS
---------------
The S60 ghost-prune hardening (main.py: query both token programs, two-strike rule,
degraded-read bail) prevents MOST orphans at the source. This is the safety net for
whatever still slips through, plus a periodic check on the other ways the fleet can
silently go wrong (a frozen bot, a stalled sidecar, dead evolution loops, a fresh
ghost-close streak).

It is the scheduled, autonomous version of `reconcile.py` — and it reuses that tool's
audited primitives (enumerate_holdings / close_empty / sell_orphan) rather than
reimplementing on-chain logic.

WHAT IT DOES EACH RUN
---------------------
Per bot (1,2,3):
  • Enumerate on-chain holdings across public RPCs (false-empty-read resistant).
  • Auto-reclaim rent from empty ATAs (harmless, reversible — ATA recreated on next buy).
  • Detect ORPHANS (held on-chain, NOT in positions.json). Value each in SOL.
      - value ≥ SELL_MIN_SOL  → auto-sell → SOL via Jupiter (reconcile.sell_orphan).
      - value <  SELL_MIN_SOL  → leave + alert (not worth the slippage/fees).
  • NEVER touches a mint tracked in positions.json (won't yank a live position).

Fleet-wide:
  • Bot liveness   — status.json last_update age (> STALE_BOT_S = ALERT).
  • Sidecar        — sidecar_heartbeat.json age (> STALE_HB_S = ALERT).
  • Evolution loops— signal_lab --log + strategy_brain --evolve processes alive.
  • Ghost streak   — ghost_close/ghost_prune count in trades.db over the last 24h.

OUTPUT
------
  • Human summary  → stdout + logs/watchdog.log (appended, timestamped).
  • Machine record → logs/watchdog_health.jsonl (one JSON line per run).
  • Exit code      → 0 = all clear, 1 = at least one ALERT (for launchd/cron signaling).

SAFETY
------
  • SOL-denominated sell gate (no USD anywhere), default 0.05 SOL.
  • Single-instance lock — nohup loop + launchd can't double-run and double-sell.
  • --dry-run reports everything and touches nothing.

USAGE
-----
    python3 fleet_watchdog.py                 # full run (auto rent-reclaim + auto-sell ≥0.05◎)
    python3 fleet_watchdog.py --dry-run       # report only, no on-chain action
    python3 fleet_watchdog.py --sell-min-sol 0.1
    python3 fleet_watchdog.py --no-sell       # rent-reclaim + alert, never sell
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from solders.keypair import Keypair

# Reuse reconcile.py's audited on-chain primitives — single source of truth.
from reconcile import (
    enumerate_holdings,
    close_empty,
    sell_orphan,
    SOL_MINT,
)

# ── Tunables (all SOL-denominated; nothing valued in USD) ───────────────────────
BOTS            = (1, 2, 3)
SELL_MIN_SOL    = 0.05          # auto-sell orphans worth ≥ this; below = leave + alert
STALE_BOT_S     = 180.0         # status.json older than this = bot frozen/offline
STALE_HB_S      = 30.0          # sidecar heartbeat older than this = sidecar stalled
GHOST_WINDOW_H  = 6.0           # look-back window (= run cadence): only flag NEW ghosts,
                                # not stale pre-restart history from an earlier code era
GHOST_ALERT_N   = 3             # this many fresh ghosts in the window = ALERT (active source)
BAL_DROP_SOL    = 0.05          # SOL drop on a bot since the last run = ALERT (fast bleed)
LOOP_PROCS      = {             # name → pgrep -f pattern for the evolution loops
    "signal_lab":    "signal_lab.py --log",
    "strategy_brain": "strategy_brain.py --evolve",
}

ROOT      = Path(__file__).resolve().parent
LOG_TXT   = ROOT / "logs" / "watchdog.log"
LOG_JSONL = ROOT / "logs" / "watchdog_health.jsonl"
LOCK_PATH = ROOT / "shared_memory" / "watchdog.lock"
LOCK_TTL  = 1800.0              # a held lock older than this is considered stale

# Known-dead orphan allowlist — genuinely-stuck dust with NO Jupiter route (rugged tokens
# the bot can't sell). Without this, each such mint re-trips the "unpriced orphan" alert →
# exit 1 every 6h forever, for ~◎0 of unrecoverable value. Mints listed here are skipped
# entirely (no sell attempt, no alert) and reported under known_dead instead. Add the FULL
# mint when an unpriced-orphan alert recurs across runs (full mints are in watchdog_health
# .jsonl orphans[].mint). Format: a JSON list of mints, or {"mints": {mint: note, ...}}.
DEAD_ORPHANS_PATH = ROOT / "shared_memory" / "dead_orphans.json"

def load_dead_orphans() -> set:
    try:
        d = json.loads(DEAD_ORPHANS_PATH.read_text())
        if isinstance(d, dict):
            return set(d.get("mints", {}) or {})
        if isinstance(d, list):
            return set(d)
    except Exception:
        pass
    return set()


# ── Single-instance lock (nohup loop + launchd safety) ──────────────────────────
def acquire_lock() -> bool:
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < LOCK_TTL:
                return False           # another run is in flight
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
        return True
    except Exception:
        return True                    # never let the lock itself block a run

def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except Exception:
        pass


# ── Pricing: value a holding in SOL (no USD reported anywhere) ───────────────────
async def _sol_valued(client: httpx.AsyncClient, mints: list[str]) -> dict[str, float]:
    """Return {mint: price_in_SOL} via Jupiter price v2 (vsToken=SOL). Best-effort."""
    if not mints:
        return {}
    try:
        r = await client.get(
            "https://api.jup.ag/price/v2",
            params={"ids": ",".join(mints), "vsToken": SOL_MINT},
            timeout=15,
        )
        data = r.json().get("data", {})
        return {m: float(v["price"]) for m, v in data.items() if v and v.get("price")}
    except Exception:
        return {}


# ── Per-bot reconciliation ──────────────────────────────────────────────────────
async def reconcile_bot(client: httpx.AsyncClient, bot: int, *, do_sell: bool,
                        do_close: bool, sell_min_sol: float) -> dict:
    out: dict = {"bot": bot, "alerts": [], "actions": []}

    kp_path = ROOT / f"bots/bot{bot}/keypair.json"
    if bot == 1 and not kp_path.exists():
        kp_path = Path(os.path.expanduser("~/.config/solana/id.json"))
    if not kp_path.exists():
        out["alerts"].append(f"keypair missing ({kp_path})")
        return out

    keypair = Keypair.from_bytes(bytes(json.load(open(kp_path))))
    wallet  = str(keypair.pubkey())

    pos_path = ROOT / f"bots/bot{bot}/positions.json"
    tracked  = set(json.load(open(pos_path)).keys()) if pos_path.exists() else set()

    sol, accounts = await enumerate_holdings(client, wallet)
    nonzero = [a for a in accounts if a["amount"] > 0]
    empty   = [a for a in accounts if a["amount"] == 0]
    _untracked = [a for a in nonzero if a["mint"] not in tracked]
    # Known-dead orphans (no-route rugs) are skipped entirely — no sell, no alert.
    _dead   = load_dead_orphans()
    orphans = [a for a in _untracked if a["mint"] not in _dead]
    out["known_dead"] = [{"mint": a["mint"], "ui": a["ui"]}
                         for a in _untracked if a["mint"] in _dead]

    out.update(sol=round(sol, 4), n_accounts=len(accounts),
               n_orphans=len(orphans), n_empty=len(empty), n_tracked=len(tracked),
               n_known_dead=len(out["known_dead"]))

    # Value orphans in SOL
    val_sol = await _sol_valued(client, [a["mint"] for a in orphans]) if orphans else {}
    orphan_rows = []
    for a in orphans:
        v = val_sol.get(a["mint"], 0.0) * a["ui"]
        orphan_rows.append({"mint": a["mint"], "ui": a["ui"], "sol": round(v, 5),
                            "program": a["program"][:4]})
    out["orphans"] = sorted(orphan_rows, key=lambda x: -x["sol"])

    # Reclaim rent from empty ATAs (safe, always on unless dry-run)
    if do_close and empty:
        try:
            await close_empty(client, keypair, empty)
            out["actions"].append(f"closed {len(empty)} empty ATA(s) (~◎{len(empty)*0.00204:.4f})")
        except Exception as e:
            out["alerts"].append(f"close_empty failed: {e}")

    # Auto-sell orphans at/above the SOL gate; alert on the rest
    for row, a in zip(out["orphans"], sorted(orphans, key=lambda x: -(val_sol.get(x["mint"],0)*x["ui"]))):
        if row["sol"] >= sell_min_sol:
            if do_sell:
                try:
                    ok = await sell_orphan(client, keypair, a)
                    out["actions"].append(
                        f"{'SOLD' if ok else 'SELL-FAILED'} orphan {a['mint'][:8]}… (~◎{row['sol']:.4f})")
                    if not ok:
                        out["alerts"].append(f"orphan sell failed {a['mint'][:8]}… (~◎{row['sol']:.4f})")
                    await asyncio.sleep(2.0)
                except Exception as e:
                    out["alerts"].append(f"orphan sell error {a['mint'][:8]}…: {e}")
            else:
                out["alerts"].append(f"orphan ~◎{row['sol']:.4f} {a['mint'][:8]}… (sell disabled)")
        elif row["sol"] > 0:
            out["alerts"].append(f"dust orphan ~◎{row['sol']:.4f} {a['mint'][:8]}… (< ◎{sell_min_sol} gate, left)")
        else:
            out["alerts"].append(f"unpriced orphan {a['mint'][:8]}… bal={a['ui']:.4g} (no SOL route?)")

    return out


# ── Fleet-wide liveness checks ──────────────────────────────────────────────────
def check_liveness() -> dict:
    out: dict = {"alerts": [], "bots": {}}

    # Bot status freshness
    for bot in BOTS:
        sp = ROOT / f"bots/bot{bot}/status.json"
        try:
            lu = json.load(open(sp)).get("last_update")
            age = (datetime.utcnow() - datetime.fromisoformat(lu)).total_seconds()
            out["bots"][bot] = round(age, 1)
            if age > STALE_BOT_S:
                out["alerts"].append(f"bot{bot} status stale ({age:.0f}s > {STALE_BOT_S:.0f}s) — frozen/offline?")
        except Exception as e:
            out["alerts"].append(f"bot{bot} status unreadable: {e}")

    # Sidecar heartbeat
    try:
        hb = json.load(open(ROOT / "shared_memory/sidecar_heartbeat.json"))
        age = time.time() - hb.get("unix_ts", 0.0)
        out["sidecar_age_s"] = round(age, 1)
        if age > STALE_HB_S:
            out["alerts"].append(f"sidecar heartbeat stale ({age:.0f}s > {STALE_HB_S:.0f}s)")
    except Exception as e:
        out["alerts"].append(f"sidecar heartbeat unreadable: {e}")

    # Evolution loops alive
    out["loops"] = {}
    for name, pat in LOOP_PROCS.items():
        alive = subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0
        out["loops"][name] = alive
        if not alive:
            out["alerts"].append(f"loop '{name}' not running (pattern: {pat})")

    return out


def check_ghost_streak() -> dict:
    # Stored ts is naive UTC (matches datetime.utcnow() used across the codebase).
    # Compare in naive UTC — NOT via .timestamp(), which would inject the local tz.
    out: dict = {"alerts": [], "ghosts_recent": {}}
    cutoff = datetime.utcnow() - timedelta(hours=GHOST_WINDOW_H)
    for bot in BOTS:
        db = ROOT / f"bots/bot{bot}/trades.db"
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n = 0
            for (raw,) in c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid DESC LIMIT 200"):
                d = json.loads(raw)
                if not d.get("ghost"):
                    continue
                ts = d.get("ts", "")
                try:
                    when = datetime.fromisoformat(ts.replace("Z", ""))
                except Exception:
                    when = cutoff + timedelta(seconds=1)   # unknown ts: count conservatively
                if when >= cutoff:
                    n += 1
            c.close()
            out["ghosts_recent"][bot] = n
            if n >= GHOST_ALERT_N:
                out["alerts"].append(f"bot{bot} {n} ghost closes in {GHOST_WINDOW_H:.0f}h — orphan source active?")
        except Exception as e:
            out["alerts"].append(f"bot{bot} ghost check failed: {e}")
    return out


# ── Brain live-readiness (closes the wire-to-live loop) ─────────────────────────
def check_brain_readiness() -> dict:
    """Surface the brain's wire-to-live verdict. When a rule clears the FULL bar this
    raises the actionable alert that tells the operator to flip main.py's
    _LIVE_RULE_ENABLED — the watchdog is how that moment gets noticed unattended."""
    out: dict = {"alerts": [], "ready_rule": None, "near": None}
    try:
        import live_rule  # uses strategy_brain's persisted last_eval + readiness selector
        name, cand = live_rule.ready_verdict()
        if name:
            out["ready_rule"] = {"name": name, **{k: cand.get(k) for k in ("n", "wr", "ev", "ev_lo")}}
            out["alerts"].append(
                f"🎉 LIVE-READY rule '{name}' (n={cand.get('n')}, EV {cand.get('ev'):+.2f}%, "
                f"WR {cand.get('wr')}%, EV-lo {cand.get('ev_lo'):+.2f}%) — review + flip "
                f"_LIVE_RULE_ENABLED in main.py (+ sig liq_mc enrichment), then ./run.sh")
        else:
            # No alert when not ready — just record the closest contender for the log trail.
            import strategy_brain as sb
            ev = sb._load_state().get("last_eval", {})
            if ev:
                d = sb.evolve(ev.get("horizon_min", 30), apply=False)
                out["near"] = f"{d.get('live_candidate')}: {d.get('live_reason')}"
    except Exception as e:
        out["alerts"].append(f"brain readiness check failed: {e}")
    return out


def check_balance_drops(current_recon: list) -> dict:
    """Compare each bot's SOL to the previous watchdog run; alert on a fast bleed
    between scheduled checks (the 6h gap could otherwise hide a bad run)."""
    out: dict = {"alerts": [], "prev": {}}
    try:
        if not LOG_JSONL.exists():
            return out
        last = None
        with open(LOG_JSONL) as f:
            for line in f:            # last complete record from a prior run
                line = line.strip()
                if line:
                    last = line
        if not last:
            return out
        prev = {r["bot"]: r.get("sol") for r in json.loads(last).get("recon", []) if r.get("sol") is not None}
        out["prev"] = prev
        for r in current_recon:
            b, now = r["bot"], r.get("sol")
            was = prev.get(b)
            if was is not None and now is not None and (was - now) >= BAL_DROP_SOL:
                out["alerts"].append(f"bot{b} SOL dropped ◎{was - now:.4f} since last run (◎{was}→◎{now})")
    except Exception as e:
        out["alerts"].append(f"balance-drop check failed: {e}")
    return out


# ── Reporting ───────────────────────────────────────────────────────────────────
def emit(report: dict) -> None:
    ts = report["ts"]
    alerts = report["alerts"]
    actions = report["actions"]

    lines = [f"\n════ fleet watchdog {ts} ════"]
    for b in report["recon"]:
        lines.append(
            f"  bot{b['bot']}: ◎{b.get('sol','?')}  orphans={b.get('n_orphans',0)} "
            f"empty={b.get('n_empty',0)} tracked={b.get('n_tracked',0)}")
        for o in b.get("orphans", []):
            lines.append(f"      orphan {o['mint'][:10]}… ~◎{o['sol']:.4f} [{o['program']}]")
    live = report["live"]
    lines.append(f"  liveness: bots={live.get('bots')} sidecar={live.get('sidecar_age_s','?')}s "
                 f"loops={live.get('loops')}")
    lines.append(f"  ghosts_{GHOST_WINDOW_H:.0f}h: {report['ghost'].get('ghosts_recent')}")
    _brain = report.get("brain", {})
    if _brain.get("ready_rule"):
        lines.append(f"  brain: ✅ LIVE-READY '{_brain['ready_rule']['name']}'")
    elif _brain.get("near"):
        lines.append(f"  brain: ⛔ {_brain['near']}")
    if actions:
        lines.append("  ── ACTIONS ──")
        lines += [f"      ✓ {a}" for a in actions]
    lines.append("  ── ALERTS ──" if alerts else "  ✅ all clear")
    lines += [f"      ⚠ {a}" for a in alerts]
    text = "\n".join(lines)
    print(text)

    try:
        LOG_TXT.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_TXT, "a") as f:
            f.write(text + "\n")
        with open(LOG_JSONL, "a") as f:
            f.write(json.dumps(report) + "\n")
    except Exception as e:
        print(f"[watchdog] log write failed: {e}")


async def run(do_sell: bool, do_close: bool, sell_min_sol: float) -> int:
    report: dict = {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "recon": [], "actions": [], "alerts": []}

    async with httpx.AsyncClient() as client:
        for bot in BOTS:
            try:
                r = await reconcile_bot(client, bot, do_sell=do_sell,
                                        do_close=do_close, sell_min_sol=sell_min_sol)
            except Exception as e:
                r = {"bot": bot, "alerts": [f"reconcile crashed: {e}"], "actions": []}
            report["recon"].append(r)
            report["actions"] += [f"bot{bot}: {a}" for a in r.get("actions", [])]
            report["alerts"]  += [f"bot{bot}: {a}" for a in r.get("alerts", [])]

    report["live"]    = check_liveness()
    report["ghost"]   = check_ghost_streak()
    report["brain"]   = check_brain_readiness()
    report["balance"] = check_balance_drops(report["recon"])   # reads prior run BEFORE emit() appends
    report["alerts"] += (report["live"]["alerts"] + report["ghost"]["alerts"]
                         + report["brain"]["alerts"] + report["balance"]["alerts"])

    emit(report)
    return 1 if report["alerts"] else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Recurring fleet health + orphan auto-cleanup.")
    ap.add_argument("--dry-run", action="store_true", help="report only, touch nothing on-chain")
    ap.add_argument("--no-sell", action="store_true", help="reclaim rent + alert, but never sell orphans")
    ap.add_argument("--sell-min-sol", type=float, default=SELL_MIN_SOL,
                    help=f"auto-sell orphans worth ≥ this many SOL (default {SELL_MIN_SOL})")
    args = ap.parse_args()

    do_close = not args.dry_run
    do_sell  = not args.dry_run and not args.no_sell

    if not args.dry_run and not acquire_lock():
        print("[watchdog] another run holds the lock — skipping this tick.")
        return
    try:
        code = asyncio.run(run(do_sell=do_sell, do_close=do_close, sell_min_sol=args.sell_min_sol))
    finally:
        if not args.dry_run:
            release_lock()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
