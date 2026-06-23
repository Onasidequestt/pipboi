#!/usr/bin/env python3
"""edge_report.py — is the fleet's REAL trading edge positive yet?

Session 58. The prestige gate is profitability, not the dashboard. This tool answers
the only two questions that matter before scaling:
  1. How many CLEAN (ghost-free) closes has each bot logged since the fixes?
  2. Is the clean win-rate / EV positive?

Ghosts (bought-but-couldn't-sell) are excluded — they were an execution bug, not the
strategy. goldilocks needs ~20 clean closes per bot to retune meaningfully; this report
tells you when you're there.

Usage:
    python3 edge_report.py                 # since the session-57 clean-era cutoff
    python3 edge_report.py --since 2026-06-06
    python3 edge_report.py --all           # all-time, not just clean era
"""
import sqlite3, json, argparse
from pathlib import Path

CLEAN_ERA = "2026-06-05"          # session-57 ghost-close fixes landed here
RETUNE_MIN = 20                   # clean closes goldilocks wants before a retune is trustworthy
BOTS = (1, 2, 3, 4, 5, 6)


def _is_ghost(d: dict) -> bool:
    if d.get("ghost") or d.get("exit_reason") in ("ghost_close", "ghost_prune"):
        return True
    # backstop for pre-tag closes: ~0% pnl fraction but a large negative SOL pnl
    pf = d.get("pnl", 0) or 0
    ps = d.get("pnl_sol", 0) or 0
    return abs(pf) < 1e-9 and ps < -0.02


def analyse(bot: int, since: str, all_time: bool):
    db = Path(f"bots/bot{bot}/trades.db")
    if not db.exists():
        return None
    c = sqlite3.connect(db)
    rows = [json.loads(r[0]) for r in
            c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid")]
    c.close()
    tot = len(rows)
    ghosts = sum(1 for d in rows if _is_ghost(d))
    clean = [d for d in rows if not _is_ghost(d)]
    if not all_time:
        clean = [d for d in clean if str(d.get("ts", ""))[:10] >= since]
    w = sum(1 for d in clean if (d.get("pnl_sol", 0) or 0) > 0)
    l = len(clean) - w
    net = sum((d.get("pnl_sol", 0) or 0) for d in clean)
    wr = (w / len(clean) * 100) if clean else 0.0
    ev = (net / len(clean)) if clean else 0.0
    return dict(tot=tot, ghosts=ghosts, n=len(clean), w=w, l=l, wr=wr, net=net, ev=ev)


def play_breakdown(since: str, all_time: bool):
    """Aggregate clean closes across the fleet by (mode, play, regime).

    This is the S64 "what works when" intel: which personality + trade-type + market
    regime combinations are actually +EV. Rows opened before S64 have no tags and group
    under '?'. Returns a dict keyed by (mode, play, regime) → stats.
    """
    agg: dict = {}
    for bot in BOTS:
        db = Path(f"bots/bot{bot}/trades.db")
        if not db.exists():
            continue
        c = sqlite3.connect(db)
        rows = [json.loads(r[0]) for r in
                c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid")]
        c.close()
        for d in rows:
            if _is_ghost(d):
                continue
            if not all_time and str(d.get("ts", ""))[:10] < since:
                continue
            key = (d.get("mode") or "?", d.get("play") or d.get("tier") or "?", d.get("regime") or "?")
            s = agg.setdefault(key, {"n": 0, "w": 0, "net": 0.0})
            ps = d.get("pnl_sol", 0) or 0
            s["n"] += 1
            s["w"] += 1 if ps > 0 else 0
            s["net"] += ps
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=CLEAN_ERA)
    ap.add_argument("--all", action="store_true", help="all-time instead of clean era")
    ap.add_argument("--by-play", action="store_true", help="ONLY show the (mode,play,regime) breakdown")
    a = ap.parse_args()
    span = "ALL-TIME" if a.all else f"clean era (since {a.since})"

    if a.by_play:
        _print_breakdown(a.since, a.all, span)
        return

    print("═" * 66)
    print(f"  FLEET EDGE REPORT — {span}")
    print(f"  Retune ready at ≥{RETUNE_MIN} clean closes/bot · ghosts excluded")
    print("═" * 66)
    any_ready = False
    fleet_net = 0.0
    for b in BOTS:
        r = analyse(b, a.since, a.all)
        if r is None:
            continue
        fleet_net += r["net"]
        ready = r["n"] >= RETUNE_MIN
        any_ready |= ready
        edge = "🟢 +EV" if r["ev"] > 0 else ("🔴 -EV" if r["n"] else "⚪ n/a")
        bar = "RETUNE READY ✓" if ready else f"need {RETUNE_MIN - r['n']} more"
        print(f"\n  BOT {b}")
        print(f"    clean   {r['w']}W/{r['l']}L  ({r['wr']:.0f}% WR)   net ◎{r['net']:+.4f}   EV ◎{r['ev']:+.5f}/trade  {edge}")
        print(f"    closes  {r['tot']} total · {r['ghosts']} ghost(s) · {r['n']} clean in window  →  {bar}")
    print("\n" + "─" * 66)
    print(f"  FLEET clean net: ◎{fleet_net:+.4f}")
    if any_ready:
        print("  ▶ At least one bot has enough clean data — run ./run_optimizer.sh and review.")
    else:
        print("  ▶ Not enough clean data to retune yet. Let trades accumulate.")
    print("═" * 66)

    _print_breakdown(a.since, a.all, span)


def _print_breakdown(since: str, all_time: bool, span: str):
    """Print the (mode, play, regime) WR/EV/n table — 'what works when'."""
    agg = play_breakdown(since, all_time)
    print()
    print("═" * 66)
    print(f"  WHAT WORKS WHEN — by (mode · play · regime) — {span}")
    print("═" * 66)
    if not agg:
        print("  (no clean closes in window)")
        print("═" * 66)
        return
    print(f"  {'mode':<7}{'play':<14}{'regime':<11}{'n':>4}  {'WR':>5}  {'EV/trade':>10}  {'net◎':>9}")
    print("  " + "─" * 62)
    for key, s in sorted(agg.items(), key=lambda kv: kv[1]["n"], reverse=True):
        mode, play, regime = key
        n   = s["n"]
        wr  = (s["w"] / n * 100) if n else 0.0
        ev  = (s["net"] / n) if n else 0.0
        tag = "🟢" if ev > 0 else "🔴"
        print(f"  {mode:<7}{play:<14}{regime:<11}{n:>4}  {wr:>4.0f}%  ◎{ev:>+8.5f}  ◎{s['net']:>+7.4f} {tag}")
    print("═" * 66)
    print("  Untagged pre-S64 closes group under '?'. Tags populate as new trades close.")
    print("═" * 66)


if __name__ == "__main__":
    main()
