#!/usr/bin/env python3
"""Read-only audit of the deep_pool / brain_rule LIVE results vs the brain's SHADOW paper-EV.

The handoff's #1 question: is the deep_pool family's *measured live* edge converging to the
brain's *paper* edge, or is the gap (ghosts + execution) eating it? This pairs each live close
with its open to get hold time + realized %, aggregates by play, and prints it next to the
brain's current paper EV for the matching rule. Pure read — touches nothing, risks nothing.

    python3 deep_pool_audit.py
"""
import glob
import json
import sqlite3
from datetime import datetime

PLAYS = ("deep_pool", "brain_rule")
# live play tag → brain candidate whose paper-EV is the shadow expectation
SHADOW_RULE = {"deep_pool": "deep_pool_quality", "brain_rule": "deep_pool_strict"}


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _play(d):
    return d.get("insane_tier") or d.get("play")


def collect():
    """Return list of paired closes: {bot, play, pnl_sol, pnl_pct, ghost, hold_min, regime}."""
    out = []
    for f in sorted(glob.glob("bots/bot*/trades.db")):
        bot = f.split("/")[1]
        try:
            c = sqlite3.connect(f)
            rows = [json.loads(r[0]) for r in c.execute(
                "SELECT data FROM trades WHERE event IN ('open','close') ORDER BY rowid")]
        except Exception:
            continue
        opens = {}  # mint -> list of open dicts (FIFO)
        for d in rows:
            ev = d.get("event")
            mint = d.get("mint")
            if ev == "open":
                opens.setdefault(mint, []).append(d)
            elif ev == "close" and _play(d) in PLAYS:
                # Pair to the MOST RECENT open before this close (not FIFO) — a mint reused
                # across sessions otherwise matches a stale prior open (the bogus 47h hold).
                t1 = _ts(d.get("ts"))
                op = opens.get(mint, [])
                cand = [(i, o) for i, o in enumerate(op)
                        if _ts(o.get("ts")) and (t1 is None or _ts(o.get("ts")) <= t1)]
                if cand:
                    idx, o = max(cand, key=lambda io: _ts(io[1].get("ts")))
                    op.pop(idx)
                else:
                    o = op.pop() if op else {}
                size = o.get("size_sol") or 0.0
                pnl_sol = d.get("pnl_sol") or 0.0
                pnl_pct = (pnl_sol / size * 100.0) if size else None
                t0 = _ts(o.get("ts"))
                hold_min = ((t1 - t0).total_seconds() / 60.0) if (t0 and t1) else None
                # Mispair guard: no play holds past ~5h, so a >6h "hold" means the matching
                # open is stale/missing (e.g. a position restored across a restart logs no fresh
                # open event). Mark realized-%/hold n/a rather than report a bogus number.
                if hold_min is not None and (hold_min < 0 or hold_min > 360):
                    hold_min = None
                    pnl_pct = None
                out.append({
                    "bot": bot, "play": _play(d), "pnl_sol": pnl_sol,
                    "pnl_pct": pnl_pct, "ghost": bool(d.get("ghost")),
                    "hold_min": hold_min, "regime": d.get("regime"),
                })
    return out


def brain_paper_ev():
    try:
        j = json.load(open("shared_memory/strategy_brain.json"))
        return (j.get("last_eval", {}) or {}).get("candidates", {}) or {}
    except Exception:
        return {}


def _stat(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def main():
    rows = collect()
    cands = brain_paper_ev()
    print("═" * 66)
    print("  DEEP_POOL / BRAIN_RULE — LIVE realized vs SHADOW paper-EV")
    print("═" * 66)
    if not rows:
        print("  no deep_pool/brain_rule closes yet")
        return
    by = {}
    for r in rows:
        by.setdefault(r["play"], []).append(r)
    for play in sorted(by):
        rs = by[play]
        n = len(rs)
        wins = sum(1 for r in rs if r["pnl_sol"] > 0)
        gh = sum(1 for r in rs if r["ghost"])
        net = sum(r["pnl_sol"] for r in rs)
        ev_pct = _stat([r["pnl_pct"] for r in rs])
        hold = _stat([r["hold_min"] for r in rs])
        rule = SHADOW_RULE.get(play, "")
        c = cands.get(rule, {})
        paper = c.get("ev")
        paper_lo = c.get("ev_lo")
        print(f"\n  {play}  (shadow rule: {rule})")
        print(f"    LIVE : n={n}  WR={wins/n*100:.0f}%  ghost={gh} ({gh/n*100:.0f}%)  "
              f"net={net:+.5f}◎")
        print(f"           realized EV={ev_pct:+.2f}%/trade  " if ev_pct is not None else
              "           realized EV=n/a  ", end="")
        print(f"avg hold={hold:.0f}min" if hold is not None else "avg hold=n/a")
        if paper is not None:
            gap = (ev_pct - paper) if ev_pct is not None else None
            print(f"    SHADOW: paper EV={paper:+.2f}%  (ev_lo {paper_lo:+.2f}%)  "
                  f"n={c.get('n')}  WR={c.get('wr')}%")
            if gap is not None:
                verdict = ("LIVE ABOVE paper" if gap > 1 else
                           "LIVE ~= paper" if abs(gap) <= 1 else
                           "LIVE BELOW paper (ghosts+exec eating edge)")
                print(f"    GAP  : live − paper = {gap:+.2f}%  → {verdict}")
    # ghost detail
    ghosts = [r for r in rows if r["ghost"]]
    if ghosts:
        print("\n  ── GHOST CLOSES ──")
        for r in ghosts:
            print(f"    {r['bot']} {r['play']} pnl={r['pnl_sol']:+.5f}◎ "
                  f"hold={r['hold_min']:.0f}min" if r["hold_min"] is not None
                  else f"    {r['bot']} {r['play']} pnl={r['pnl_sol']:+.5f}◎ hold=n/a")
    print("\n" + "═" * 66)
    print("  NOTE: realized % uses pnl_sol/size_sol; paper EV is winsorized forward return.")
    print("  Not identical metrics, but the SIGN + magnitude of the gap is the signal.")
    print("═" * 66)


if __name__ == "__main__":
    main()
