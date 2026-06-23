#!/usr/bin/env python3
"""
vol_accel_validate.py  ·  READ-ONLY  ·  paper-lab quest Stage-0 validator

Does the LIVE vol-acceleration edge (S87 made vaccel the lead scorer) realize the
per-regime / per-liquidity-band shape the 10-h paper quest predicted — BEFORE we lean
on it with sizing? Compares:

  PAPER  : forward_obs.jsonl, vol_accel = v5/(v1h/12) cohort (the quest's definition),
           per regime × liq-band — mean fwd% + ev_lo (mean − 1.64·SE).
  LIVE   : every CLEAN fleet close (ghost-aware), ret% = pnl_sol/size_sol, per regime ×
           liq-band — n, mean ret%, ghost-rate, ◎/trade.
  CHECK  : (a) small-liq still beats big-liq live?  (b) kept regimes +EV live?
           (c) live/paper realization ratio (quest saw ~15-20%, entry-lag haircut).

Honest caveat baked in: the live scorer's _vol_accel uses a rolling-window ratio while
this paper side uses v5/(v1h/12); the comparison is DIRECTIONAL (does live match the
SHAPE), not a same-definition apples-to-apples. NEVER writes anything. NEVER touches the
fleet. Mirrors prestige_tracker's ghost notion for clean-era consistency.

Usage:  python3 vol_accel_validate.py [--days N] [--va 3] [--clean-era 2026-06-05]
"""
import json, sqlite3, math, argparse, statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOTS = (1, 2, 3)
FWD_OBS = ROOT / "shared_memory" / "forward_obs.jsonl"
REGIMES = ["euphoria", "aggressive", "sniper", "normal", "dead"]
LIQ_BANDS = [("<50k", 0, 50_000), ("50-300k", 50_000, 300_000), (">300k", 300_000, 9e18)]

def regime_of(agg):
    if agg is None: return "?"
    if agg >= 600000: return "euphoria"
    if agg >= 280000: return "aggressive"
    if agg >= 110000: return "normal"
    if agg >= 48000:  return "sniper"
    return "dead"

def band_of(liq):
    for name, lo, hi in LIQ_BANDS:
        if lo <= (liq or 0) < hi: return name
    return "?"

def ev_lo(xs):
    if len(xs) < 2: return (len(xs), 0.0, 0.0)
    m = statistics.mean(xs); se = statistics.pstdev(xs) / len(xs) ** 0.5
    return (len(xs), round(m, 2), round(m - 1.64 * se, 2))

# ── PAPER side: forward_obs, quest vol_accel = v5/(v1h/12) ───────────────────
def paper_side(va_min):
    cells = {}  # (regime, band) -> [fwd]
    for line in open(FWD_OBS):
        try: r = json.loads(line)
        except Exception: continue
        if r.get("fwd") is None: continue
        v5 = r.get("v5") or 0.0; v1h = r.get("v1h") or 0.0
        va = v5 / (v1h / 12.0) if v1h else 0.0
        if va < va_min: continue
        reg = regime_of(r.get("agg"))
        band = band_of(r.get("liq"))
        cells.setdefault((reg, band), []).append(r["fwd"])
    return cells

# ── LIVE side: clean fleet closes, ret% = pnl_sol/size_sol ───────────────────
def live_side(clean_era, days):
    import time
    cutoff_iso = clean_era
    cells = {}   # (regime, band) -> {"ret":[], "ghost":int}
    by_reg = {}  # regime -> {"ret":[], "ghost":int}
    for b in BOTS:
        db = ROOT / f"bots/bot{b}/trades.db"
        if not db.exists(): continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT data FROM trades WHERE event='close' AND ts >= ? ORDER BY ts",
                (cutoff_iso,)).fetchall()
            con.close()
        except Exception:
            continue
        for (data,) in rows:
            try: d = json.loads(data)
            except Exception: continue
            size = d.get("size_sol"); pnl = d.get("pnl_sol")
            if not size or size <= 0 or pnl is None: continue
            ret = pnl / size * 100.0
            reg = d.get("regime") or "?"
            band = band_of(d.get("entry_liq"))
            exitr = (d.get("exit_reason") or "").lower()
            # NB: the dead-pool exit reason contains "...exit before it ghosts" — that is NOT
            # a ghost. A real ghost = a catastrophic realized loss or an explicit ghost_close.
            ghost = ret <= -50.0 or exitr.startswith("ghost")
            cells.setdefault((reg, band), {"ret": [], "ghost": 0})
            cells[(reg, band)]["ret"].append(ret)
            if ghost: cells[(reg, band)]["ghost"] += 1
            by_reg.setdefault(reg, {"ret": [], "ghost": 0})
            by_reg[reg]["ret"].append(ret); by_reg[reg]["ghost"] += int(ghost)
    return cells, by_reg

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--va", type=float, default=3.0, help="paper vol_accel threshold")
    ap.add_argument("--clean-era", default="2026-06-05")
    ap.add_argument("--days", type=int, default=0)
    args = ap.parse_args()

    paper = paper_side(args.va)
    live, live_reg = live_side(args.clean_era, args.days)

    print(f"\n=== vol_accel VALIDATOR — paper (va≥{args.va}) vs LIVE (clean era {args.clean_era}) ===\n")
    print(f"{'regime':11} {'liq-band':9} | {'PAPER n':>7} {'fwd%':>6} {'ev_lo':>6} | {'LIVE n':>6} {'ret%':>6} {'ghost':>5} {'◎/tr':>8}")
    print("-" * 90)
    realiz = []
    for reg in REGIMES:
        for band, _, _ in LIQ_BANDS:
            p = paper.get((reg, band), [])
            pn, pm, pevlo = ev_lo(p)
            lv = live.get((reg, band))
            if not lv and pn == 0: continue
            ln = len(lv["ret"]) if lv else 0
            lm = round(statistics.mean(lv["ret"]), 2) if ln else 0.0
            lsol = round(sum(x/100.0 for x in lv["ret"]) , 4) if ln else 0.0  # not size-weighted; rough
            gh = f"{100*lv['ghost']/ln:.0f}%" if ln else "-"
            opt = round(lm/pm, 2) if (ln >= 5 and pm > 0) else None
            if opt is not None: realiz.append(opt)
            print(f"{reg:11} {band:9} | {pn:>7} {pm:>6} {pevlo:>6} | {ln:>6} {lm:>6} {gh:>5} {lsol:>8}")
    print("-" * 90)

    print("\n— CHECKS —")
    # (a) small-liq vs big-liq live (kept regimes)
    sl = [x for (r, b), v in live.items() if b == "<50k" and r in ("euphoria","aggressive","sniper") for x in v["ret"]]
    bl = [x for (r, b), v in live.items() if b == ">300k" and r in ("euphoria","aggressive","sniper") for x in v["ret"]]
    if sl and bl:
        print(f"(a) small-liq<50k mean {statistics.mean(sl):+.2f}% (n{len(sl)}) vs >300k {statistics.mean(bl):+.2f}% (n{len(bl)})"
              f"  → {'✓ small-liq edge holds live' if statistics.mean(sl) > statistics.mean(bl) else '✗ NOT holding — revisit liq filter'}")
    else:
        print(f"(a) insufficient live n by liq-band (small {len(sl)} / big {len(bl)}) — need more closes")
    # (b) kept regimes +EV live
    for reg in ("euphoria", "aggressive", "sniper"):
        v = live_reg.get(reg)
        if v and len(v["ret"]) >= 3:
            n, m, el = ev_lo(v["ret"])
            verdict = "TRUST" if n >= 8 else "THIN"
            print(f"(b) {reg:11} live n={n:3} mean={m:+.2f}% ev_lo={el:+.2f} ghost={100*v['ghost']/n:.0f}%  [{verdict}]"
                  f"  {'+EV ✓' if m > 0 else '−EV ✗'}")
        else:
            print(f"(b) {reg:11} live n<3 — PAPER-ONLY still")
    # (c) realization ratio
    if realiz:
        print(f"(c) live/paper realization ratio (cells n≥5): median {statistics.median(realiz):.2f} "
              f"(quest expected ~0.15-0.20 from entry-lag; production observer should beat the paper-engine floor)")
    else:
        print("(c) realization ratio: not enough matched cells yet (need live n≥5 in a +EV paper cell)")
    print()

if __name__ == "__main__":
    main()
