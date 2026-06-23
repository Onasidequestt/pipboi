#!/usr/bin/env python3
"""
regime_ev.py  (S84) — per-REGIME × per-PLAY realized-edge tracker.

READ-ONLY. Buckets every CLEAN (ghost-free) close across the fleet by (play, regime)
and reports, per cell: n · win-rate · net SOL · EV/trade (SOL) · mean realized
return-% (pnl_sol / size_sol, when size_sol was logged — S80+).

WHY: the whole S84 investigation showed the edge is REGIME-CONDITIONAL, but every
one-off cut disagreed on *which* regime each play is +EV in (small-n noise). This is
the durable, accumulating measurement that replaces those cuts — the evidence layer
the regime-conditional admission table and (eventually, gen-3) per-regime SIZE genes
both need before any change can be made with confidence. It changes NO trading logic.

  python3 regime_ev.py            # fleet table, clean era
  python3 regime_ev.py --all      # all-time (pre-clean-era rows included)
  python3 regime_ev.py --json     # machine-readable (what the dashboard serves)
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BOTS = (1, 2, 3, 4, 5, 6)
# 5-regime ladder order (+ '?' bucket for pre-tag rows), matches validation.py / main.py.
REGIME_ORDER = ["euphoria", "aggressive", "normal", "sniper", "dead", "?"]
CLEAN_ERA = "2026-06-05"   # session-57 ghost-close fixes landed here (matches edge_report.py)
# n below which a cell's EV is statistically untrustworthy (small-n noise — the trap S84 hit).
MIN_TRUST_N = 8


def _is_ghost(d: dict) -> bool:
    """Mirror edge_report._is_ghost: a close is a ghost if explicitly flagged, exited via a
    ghost path, or carries a ghost-signature."""
    if d.get("ghost") or d.get("exit_reason") in ("ghost_close", "ghost_prune"):
        return True
    return str(d.get("sig", "")).startswith("ghost")


def collect(all_time: bool = False, since: str = CLEAN_ERA) -> dict:
    """Return {(play, regime): stats} aggregated over clean fleet closes."""
    cells: dict = {}
    for bot in BOTS:
        db = Path(f"bots/bot{bot}/trades.db")
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = [json.loads(r[0]) for r in
                    c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid")]
            c.close()
        except Exception:
            continue
        for d in rows:
            if _is_ghost(d):
                continue
            if not all_time and str(d.get("ts", ""))[:10] < since:
                continue
            play   = d.get("play") or d.get("tier") or "?"
            regime = d.get("regime") or "?"
            s = cells.setdefault((play, regime),
                                 {"n": 0, "w": 0, "net": 0.0, "ret_sum": 0.0, "ret_n": 0})
            ps = d.get("pnl_sol", 0) or 0.0
            s["n"]   += 1
            s["w"]   += 1 if ps > 0 else 0
            s["net"] += ps
            sz = d.get("size_sol")
            if sz:
                s["ret_sum"] += ps / sz
                s["ret_n"]   += 1
    return cells


def payload(all_time: bool = False) -> dict:
    """Dashboard-ready JSON: matrix of (play × regime) cells + axis orders + totals."""
    cells = collect(all_time=all_time)
    plays   = sorted({p for (p, _r) in cells})
    regimes = [r for r in REGIME_ORDER if any(rr == r for (_p, rr) in cells)]
    out_cells = []
    for (p, r), s in cells.items():
        n = s["n"]
        out_cells.append({
            "play": p, "regime": r, "n": n,
            "wr": round(s["w"] / n * 100, 1) if n else 0.0,
            "net": round(s["net"], 4),
            "ev": round(s["net"] / n, 5) if n else 0.0,
            "ret_pct": round(s["ret_sum"] / s["ret_n"] * 100, 1) if s["ret_n"] else None,
            "trusted": n >= MIN_TRUST_N,
        })
    # per-regime column totals (across all plays) — the "is this regime +EV at all" read
    reg_tot = []
    for r in regimes:
        sub = [c for c in out_cells if c["regime"] == r]
        n = sum(c["n"] for c in sub)
        net = sum(c["net"] for c in sub)
        w = sum(round(c["wr"] / 100 * c["n"]) for c in sub)
        reg_tot.append({"regime": r, "n": n, "net": round(net, 4),
                        "ev": round(net / n, 5) if n else 0.0,
                        "wr": round(w / n * 100, 1) if n else 0.0,
                        "trusted": n >= MIN_TRUST_N})
    return {
        "plays": plays, "regimes": regimes, "cells": out_cells,
        "regime_totals": reg_tot, "min_trust_n": MIN_TRUST_N,
        "all_time": all_time,
        "generated": datetime.now(timezone.utc).isoformat(),
    }


def _fmt_cli(all_time: bool) -> str:
    p = payload(all_time=all_time)
    if not p["cells"]:
        return "No clean closes yet — nothing to bucket."
    cell = {(c["play"], c["regime"]): c for c in p["cells"]}
    w = max((len(x) for x in p["plays"]), default=4) + 1
    lines = []
    span = "ALL-TIME" if all_time else f"clean era (since {CLEAN_ERA})"
    lines.append(f"  PER-REGIME × PER-PLAY realized edge — {span}  (ghosts excluded)")
    lines.append(f"  cell = n · WR% · EV◎/trade   ·   (·) = n<{p['min_trust_n']} (untrusted, noise)")
    cw = 16  # cell width
    hdr = " " * w + "".join(f"{r[:11]:>{cw}}" for r in p["regimes"])
    lines.append(hdr)
    for pl in p["plays"]:
        row = f"{pl[:w-1]:<{w}}"
        for r in p["regimes"]:
            c = cell.get((pl, r))
            if not c:
                row += f"{'—':>{cw}}"
            else:
                tag = "" if c["trusted"] else "·"
                txt = f"{c['n']}/{c['wr']:.0f}%/{c['ev']:+.3f}{tag}"
                row += f"{txt:>{cw}}"
        lines.append(row)
    lines.append("")
    lines.append("  REGIME TOTALS (all plays):")
    for rt in p["regime_totals"]:
        edge = "+EV" if rt["ev"] > 0 else "-EV"
        tag = "" if rt["trusted"] else f"  (n<{p['min_trust_n']} — noise)"
        lines.append(f"    {rt['regime']:<11} n={rt['n']:<4} WR {rt['wr']:>4.0f}%  "
                     f"net ◎{rt['net']:+.4f}  EV ◎{rt['ev']:+.5f}/tr  {edge}{tag}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Per-regime × per-play realized edge (read-only).")
    ap.add_argument("--all", action="store_true", help="all-time instead of clean era")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON (dashboard payload)")
    a = ap.parse_args()
    if a.json:
        print(json.dumps(payload(all_time=a.all), indent=2))
    else:
        print(_fmt_cli(all_time=a.all))


if __name__ == "__main__":
    main()
