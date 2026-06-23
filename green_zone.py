#!/usr/bin/env python3
"""green_zone.py — find the admission predicate with the highest STABLE win-rate. (read-only)

"What can we trade to get the green-trade % up?" — answered from evidence, not intuition.
Scans the brain's durable matured observations (`shared_memory/forward_obs.jsonl`: every scored
token's features + its WINSORIZED 30-min forward return) and, over a curated grid of admission
predicates (the deep_pool family + tighter score/filling/liq/regime stacks), reports for each:

    n · WR(fwd>0) · solid-WR(fwd≥+3%) · mean/median fwd · ev_lo · H1→H2 stability · admit-rate

then RECOMMENDS the predicate with the best win-rate that HOLDS IN BOTH chronological halves
(an edge that only worked in the older half is an artifact / has decayed — we don't want it).
Win-rate without EV is a trap (many tiny wins, one big loss), so EV is shown alongside and the
recommendation requires BOTH halves' EV ≥ 0. Changes NOTHING — it prints the rule to apply.

    python3 green_zone.py                 # full table + recommendation (30-min horizon, fwd>0 = win)
    python3 green_zone.py --win 3         # define "win" as fwd ≥ +3% (solid green, not just >0)
    python3 green_zone.py --min-n 40      # require ≥N matched obs for a predicate to be ranked
    python3 green_zone.py --regime        # also break the top stacks down by market regime
"""
import json, sys, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS  = ROOT / "shared_memory" / "forward_obs.jsonl"

# 5-regime ladder bands (S79) on aggregate $ volume — same thresholds as validation.py.
def _regime(agg):
    a = agg or 0
    if a >= 600_000: return "euphoria"
    if a >= 280_000: return "aggressive"
    if a >= 110_000: return "normal"
    if a >=  48_000: return "sniper"
    return "dead"

# ── Base feature filters (each: row -> bool). None-safe. ──────────────────────────
def _f(r, k, d=0.0):
    v = r.get(k)
    return d if v is None else v

BASE = {
    "sellable":   lambda r: _f(r, "liq") >= 50_000,            # the hard exitability floor
    "deep":       lambda r: _f(r, "liq_mc") >= 0.10,           # deep pool relative to mcap
    "filling":    lambda r: _f(r, "lqv") > 0.01,               # LP ADDING while price moves (the edge)
    "mom1":       lambda r: _f(r, "m5") >= 1.0,
    "mom2":       lambda r: _f(r, "m5") >= 2.0,
    "buy":        lambda r: _f(r, "bs") >= 1.0,
    "buy_strong": lambda r: _f(r, "bs") >= 1.3,
    "vacc":       lambda r: _f(r, "vacc") >= 1.5,
    "score65":    lambda r: r.get("score") is not None and r["score"] >= 65,
    "score80":    lambda r: r.get("score") is not None and r["score"] >= 80,
    "scoreband":  lambda r: r.get("score") is not None and 80 <= r["score"] < 95,
    "whale":      lambda r: _f(r, "whale") >= 1,
}

# ── Curated predicate STACKS (name -> list of base-filter keys, ANDed) ────────────
# Mirrors the live deep_pool family, then progressively tighter. The point is to find
# where the green concentrates without brute-forcing an uninterpretable combo space.
STACKS = {
    "deep_pool_quality (live tail)":        ["deep", "mom1", "buy"],
    "deep_pool_strict":                     ["deep", "mom2", "buy"],
    "deep_pool_filling":                    ["deep", "filling", "buy"],
    "deep_pool_strict_filling":             ["deep", "mom2", "filling", "buy"],
    "filling + sellable":                   ["deep", "filling", "buy", "sellable"],
    "strict_filling + sellable":            ["deep", "mom2", "filling", "buy", "sellable"],
    "filling + buy_strong":                 ["deep", "filling", "buy_strong"],
    "filling + score65":                    ["deep", "filling", "buy", "score65"],
    "strict_filling + score65":             ["deep", "mom2", "filling", "buy", "score65"],
    "filling + sellable + buy_strong":      ["deep", "filling", "buy_strong", "sellable"],
    "strict_filling + sellable + buy_strong": ["deep", "mom2", "filling", "buy_strong", "sellable"],
    "filling + vacc":                       ["deep", "filling", "buy", "vacc"],
    "scoreband only":                       ["scoreband"],
    "scoreband + deep":                     ["scoreband", "deep"],
}


def _match(r, keys):
    return all(BASE[k](r) for k in keys)


def _stats(rows, win):
    """n, WR, solid-WR, mean, median, ev_lo over fwd (already winsorized)."""
    fwds = [r["fwd"] for r in rows if isinstance(r.get("fwd"), (int, float))]
    n = len(fwds)
    if n == 0:
        return None
    wr   = sum(1 for x in fwds if x > 0) / n * 100
    solid = sum(1 for x in fwds if x >= win) / n * 100
    mean = sum(fwds) / n
    med  = sorted(fwds)[n // 2]
    if n > 1:
        var = sum((x - mean) ** 2 for x in fwds) / (n - 1)
        se  = math.sqrt(var) / math.sqrt(n)
    else:
        se = 0.0
    ev_lo = mean - 1.64 * se
    return dict(n=n, wr=wr, solid=solid, mean=mean, med=med, ev_lo=ev_lo)


def _halves(rows):
    """Chronological split by ts → (older, newer)."""
    s = sorted(rows, key=lambda r: r.get("ts", 0))
    mid = len(s) // 2
    return s[:mid], s[mid:]


def analyse(rows, keys, win):
    matched = [r for r in rows if _match(r, keys)]
    st = _stats(matched, win)
    if not st:
        return None
    h1, h2 = _halves(matched)
    s1, s2 = _stats(h1, win), _stats(h2, win)
    st["h1_ev"] = s1["mean"] if s1 else None
    st["h2_ev"] = s2["mean"] if s2 else None
    st["h1_wr"] = s1["wr"] if s1 else None
    st["h2_wr"] = s2["wr"] if s2 else None
    st["admit_rate"] = len(matched) / len(rows) * 100
    # stable = both halves exist, both EV ≥ 0 AND both WR ≥ 50
    st["stable"] = bool(s1 and s2 and s1["mean"] >= 0 and s2["mean"] >= 0
                        and s1["wr"] >= 50 and s2["wr"] >= 50)
    return st


def main(argv):
    win    = 0.0
    min_n  = 30
    do_reg = "--regime" in argv
    if "--win" in argv:
        try: win = float(argv[argv.index("--win") + 1])
        except Exception: win = 0.0
    if "--min-n" in argv:
        try: min_n = int(argv[argv.index("--min-n") + 1])
        except Exception: min_n = 30

    rows = []
    with open(OBS) as f:
        for ln in f:
            try:
                r = json.loads(ln)
                if isinstance(r.get("fwd"), (int, float)):
                    rows.append(r)
            except Exception:
                pass

    span_d = 1.0
    ts = [r.get("ts") for r in rows if r.get("ts")]
    if len(ts) > 1:
        span_d = (max(ts) - min(ts)) / 86400

    print("=" * 92)
    print(f"  GREEN-ZONE ANALYZER — highest STABLE win-rate predicate  (n_obs={len(rows)} · "
          f"{span_d:.1f}d · win=fwd≥{win:.0f}% · 30-min horizon)")
    print("=" * 92)
    print(f"  {'predicate':<34}{'n':>5}{'WR':>6}{'solid':>7}{'medEV':>7}{'ev_lo':>7}"
          f"{'  H1→H2 EV':>13}{'admit%':>8}  ok")
    print("  " + "─" * 88)

    results = []
    for name, keys in STACKS.items():
        st = analyse(rows, keys, win)
        if not st:
            continue
        results.append((name, keys, st))

    # Print ranked by WR (then ev_lo), flagging which clear the n + stability bar.
    for name, keys, st in sorted(results, key=lambda x: (-x[2]["wr"], -x[2]["ev_lo"])):
        h12 = (f"{st['h1_ev']:+.1f}→{st['h2_ev']:+.1f}"
               if st["h1_ev"] is not None and st["h2_ev"] is not None else "—")
        ok = "✅" if (st["n"] >= min_n and st["stable"]) else ("·" if st["n"] >= min_n else "n<")
        print(f"  {name:<34}{st['n']:>5}{st['wr']:>5.0f}%{st['solid']:>6.0f}%"
              f"{st['med']:>+7.1f}{st['ev_lo']:>+7.1f}{h12:>13}{st['admit_rate']:>7.1f}%  {ok}")
    print("  " + "─" * 88)
    print("  ok: ✅ = n≥min AND both halves +EV & WR≥50 (STABLE green)  ·  · = enough n but not stable"
          "  ·  n< = too few obs")

    # ── Recommendation ──
    elig = [(nm, ky, st) for nm, ky, st in results if st["n"] >= min_n and st["stable"]]
    print("\n  RECOMMENDATION:")
    if not elig:
        print("    ⚠ NO predicate is stably +EV/≥50% WR in BOTH halves at the current min-n.")
        # show the best-by-recent-half as the least-bad / watch candidate
        recent = sorted([r for r in results if r[2]["n"] >= min_n and r[2]["h2_ev"] is not None],
                        key=lambda x: -x[2]["h2_ev"])
        if recent:
            nm, ky, st = recent[0]
            print(f"    Best RECENT half: '{nm}' (H2 EV {st['h2_ev']:+.1f}%, WR {st['h2_wr']:.0f}%, "
                  f"n={st['n']}). The edge is decaying/regime-bound — see --regime, or wait for the")
            print("    tape to wake up. Tightening selection won't manufacture an edge that isn't there.")
    else:
        nm, ky, st = max(elig, key=lambda x: x[2]["wr"])
        per_day = st["n"] / max(span_d, 0.5) * (st["admit_rate"] / 100) / max(st["admit_rate"]/100, 1e-9)
        adm_per_day = (st["admit_rate"] / 100) * (len(rows) / max(span_d, 0.5))
        print(f"    ▶ ADMIT: {nm}")
        print(f"      filters: {' AND '.join(ky)}")
        print(f"      WR {st['wr']:.0f}% · solid {st['solid']:.0f}% · ev_lo {st['ev_lo']:+.1f}% · "
              f"stable (H1 {st['h1_ev']:+.1f} / H2 {st['h2_ev']:+.1f})")
        print(f"      admits {st['admit_rate']:.1f}% of the feed (~{adm_per_day:.0f} candidates/day) "
              f"— the frequency cost of the higher WR.")

    if do_reg:
        print("\n  REGIME BREAKDOWN (deep_pool_strict_filling — where does the green live?):")
        keys = STACKS["deep_pool_strict_filling"]
        by = {}
        for r in rows:
            if _match(r, keys):
                by.setdefault(_regime(r.get("agg")), []).append(r)
        print(f"    {'regime':<12}{'n':>5}{'WR':>6}{'medEV':>8}{'ev_lo':>8}")
        for rg in ("euphoria", "aggressive", "normal", "sniper", "dead"):
            st = _stats(by.get(rg, []), win)
            if st:
                print(f"    {rg:<12}{st['n']:>5}{st['wr']:>5.0f}%{st['med']:>+8.1f}{st['ev_lo']:>+8.1f}")
    print("=" * 92)


if __name__ == "__main__":
    main(sys.argv)
