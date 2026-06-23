#!/usr/bin/env python3
"""ride_ab.py — S98 · READ-ONLY conclusiveness tracker for the deep_pool RIDE-exit A/B.

THE QUESTION (operator, S98)
  Is the RIDE exit (SL ~-10% + smart-trail, NO fixed-TP bank) CONCLUSIVELY the right exit
  for the deep_pool/brain_rule gate cohort — proven to a *real n* on the live cohort — so
  that EV-sizing may arm? "The exit is the one place the evidence is directionally clean
  across multiple independent analyses — concentrate there. Get it to a real n BEFORE
  sizing touches it. Don't expand it on faith."

WHY THIS EXISTS (the gap it closes)
  The deploy gate (prestige_tracker / arm_genes) arms EV-sizing on deep_pool net≥0,
  ghost≤10%, n≥15 — but it does NOT check that the RIDE exit is the proven policy. The gate
  is at n=6/15 with net already +; a modest fire-rate bump would push n past 15 and arm
  sizing on an exit policy confirmed only on the counterfactual (live n=4). This tool defines
  "ride proven" rigorously and exposes ride_exit_proven() so arm_genes can SEQUENCE correctly:
  prove the exit live → THEN let sizing arm. Fail-closed (any error ⇒ NOT proven).

THREE INDEPENDENT READS, ONE VERDICT
  [1] COUNTERFACTUAL (prove_edge replay of forward_obs, KEPT cohort, hundreds of obs):
      the exit-DEPENDENCE — ride > hold > tpsl — is the robust, window-stable signal.
  [2] LIVE A/B: bot1 (RIDE arm) vs bot2+bot3 (BANK arm) realized deep_pool/brain_rule
      closes (non-ghost, size_sol present, gate-aligned regime cohort).
  [3] CALIBRATION: does the shadow ride-model match live? (the trust bridge)

CONCLUSIVE (ride proven, sizing may arm) iff ALL of:
  (a) ORDERING   cf mean(ride) > mean(hold) AND cf mean(ride) > mean(tpsl)   [exit-dependence]
  (b) LIVE       ride-arm n ≥ N_LIVE_MIN  AND  ride-arm mean ≥ bank-arm mean [real n, ride≥bank]
  (c) CALIBRATION |shadow_ride − live_ride| < CAL_TOL                        [model trusted]
  (d) CONCENTRATION  ≥MIN_DISTINCT_MINTS distinct mints AND no single mint    [not one lucky token]
                     >MAX_MINT_SHARE of the live cohort

CONCENTRATION GUARD (S105-audit) — why (d) exists
  An early live read had ONE mint (B6f27ETGcj) supply 8 of 13 cohort closes (62%), with the
  bank arm's two "+18.3%" wins being the SAME correlated cross-bot entry counted twice. A
  verdict built on a single token is noise, not proof — so the live arms also report distinct-
  mint count + the dominant mint's share, a per-mint-collapsed ("deduped") mean that neutralises
  one mint contributing many samples, and the verdict is held NOT-CONCLUSIVE while the cohort is
  single-token dominated. Safe-direction: can only DELAY arming, never force it.

  python3 ride_ab.py            # full report + verdict
  python3 ride_ab.py --json     # machine-readable verdict
  from ride_ab import ride_exit_proven   # (bool, detail) — fail-closed, for arm_genes
"""
import os, sys, json, argparse, statistics
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import prove_edge as pe          # counterfactual replay + admit predicate (read-only)
import lab                        # _se (prove_edge already put paper_lab on the path)

# ── conclusiveness thresholds (the bar "a real n on the proven cohort" must clear) ──
N_LIVE_MIN = 12        # ride-arm live closes needed before the live A/B is a "real n"
CAL_TOL    = 1.5       # |shadow − live| mean %% within which the shadow model is trusted
RIDE_BOT   = 1                 # bot1 carries deep_pool_ride.json (the RIDE arm)
BANK_BOTS  = (2, 3)            # bot2/3 = fixed-TP-bank control

# ── concentration guard (S105-audit): a green that rests on ONE token is not a real green ──
MAX_MINT_SHARE     = 0.40   # one mint may be ≤40% of the live cohort's closes; above = too concentrated
MIN_DISTINCT_MINTS = 5      # need ≥5 distinct mints across the live arms before the A/B can conclude


def _live_arm_rows():
    """Per-arm realized closes on the live, gate-aligned deep_pool/brain_rule cohort.
    Returns (ride_rows, bank_rows, ride_ghost, bank_ghost) where each *_rows is a list of
    (mint, return%%) tuples. Mirrors the deploy gate's cohort definition (skip-regime closes
    excluded, ghosts excluded from the mean but counted) so the A/B reads the SAME edge the
    gate sizes. The mint is carried so the verdict can refuse to conclude on one lucky token."""
    from prestige_tracker import _closes, _is_ghost, _GATE_SKIP_REGIMES, _DP_PLAYS
    ride_rows, bank_rows = [], []
    ride_ghost = bank_ghost = 0
    for b in (RIDE_BOT, *BANK_BOTS):
        for r in _closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play not in _DP_PLAYS:
                continue
            # S98-reconcile: count the deliberately-traded tight `normal` slice (normal_slice=True);
            # only the STALE skip-regime bulk is excluded — mirrors prestige_tracker's gate cohort.
            if r.get("regime") in _GATE_SKIP_REGIMES and not r.get("normal_slice"):
                continue
            is_ghost = _is_ghost(r) or str(r.get("exit_reason", "")).startswith("ghost")
            if is_ghost:
                if b == RIDE_BOT: ride_ghost += 1
                else:             bank_ghost += 1
                continue
            sz = r.get("size_sol") or 0.0
            if sz <= 0:                                     # pre-S80 rows lack the denom
                continue
            mint = r.get("mint") or "(unknown)"             # unknowns lump → conservative (more concentrated)
            ret = (r.get("pnl_sol", 0.0) or 0.0) / sz * 100.0
            (ride_rows if b == RIDE_BOT else bank_rows).append((mint, ret))
    return ride_rows, bank_rows, ride_ghost, bank_ghost


def _dedup_mean(rows):
    """Per-mint-collapsed mean: average each mint's closes first, then average those — so one
    mint contributing many (often correlated) samples counts ONCE, not N times."""
    g = defaultdict(list)
    for m, ret in rows:
        g[m].append(ret)
    permint = [statistics.mean(v) for v in g.values()]
    return statistics.mean(permint) if permint else None


def _concentration(ride_rows, bank_rows):
    """Cohort-wide concentration read on the live arms (ride+bank combined).
    Returns distinct-mint count, the dominant mint + its count-share, and that mint's signed
    net contribution (◎-return-points) so a single-token-driven verdict is visible + blockable."""
    allrows = ride_rows + bank_rows
    n = len(allrows)
    cnt = Counter(m for m, _ in allrows)
    distinct = len(cnt)
    top_mint, top_n = cnt.most_common(1)[0] if cnt else (None, 0)
    top_share = (top_n / n) if n else 0.0
    top_ret_pts = sum(ret for m, ret in allrows if m == top_mint)     # this mint's Σreturn%% (signed)
    return {
        "n": n, "distinct": distinct, "top_mint": top_mint, "top_n": top_n,
        "top_share": top_share, "top_ret_pts": top_ret_pts,
    }


def _cf_kept_means(friction=1.5):
    """Counterfactual KEPT-cohort mean%% under each exit model (the exit-dependence read)."""
    rows = pe.load_rows()
    out = {x: [] for x in pe.EXITS}
    for r in rows:
        ok, _ = pe.deep_pool_admit(r)
        if not ok:
            continue
        if lab.regime_of(r.get("agg")) not in pe.KEPT:
            continue
        for x in pe.EXITS:
            out[x].append(pe.realized(r, x, friction))
    means = {x: (statistics.mean(v) if v else None) for x, v in out.items()}
    n = len(out["ride"])
    ev_lo_ride = (means["ride"] - 1.64 * lab._se(out["ride"])) if out["ride"] else None
    return means, n, ev_lo_ride


def evaluate(friction=1.5):
    """Compute the reads + the conclusive verdict. Returns a detail dict."""
    cf, cf_n, cf_ev_lo = _cf_kept_means(friction)
    ride_rows, bank_rows, ride_gh, bank_gh = _live_arm_rows()
    ride_rets = [ret for _, ret in ride_rows]
    bank_rets = [ret for _, ret in bank_rows]
    ride_n, bank_n = len(ride_rets), len(bank_rets)
    ride_mean = statistics.mean(ride_rets) if ride_rets else None
    bank_mean = statistics.mean(bank_rets) if bank_rets else None
    conc = _concentration(ride_rows, bank_rows)

    # (a) ordering — exit-dependence holds (ride is the best exit on the counterfactual)
    ok_order = (cf["ride"] is not None and cf["hold"] is not None and cf["tpsl"] is not None
                and cf["ride"] > cf["hold"] and cf["ride"] > cf["tpsl"])
    # (b) live — a real n on the ride arm AND ride ≥ bank
    ok_live = (ride_n >= N_LIVE_MIN and ride_mean is not None and bank_mean is not None
               and ride_mean >= bank_mean)
    # (c) calibration — shadow ride-model ≈ live ride arm
    ok_cal = (ride_mean is not None and cf["ride"] is not None
              and abs(cf["ride"] - ride_mean) < CAL_TOL)
    # (d) concentration — the live cohort is not one lucky token (S105-audit)
    ok_conc = (conc["distinct"] >= MIN_DISTINCT_MINTS and conc["top_share"] <= MAX_MINT_SHARE)

    conclusive = bool(ok_order and ok_live and ok_cal and ok_conc)
    # the single binding constraint to report (order = cheapest-to-fix first)
    if not ok_order:
        binding = "ordering: counterfactual ride is not > hold AND > tpsl (exit-dependence broke)"
    elif ride_n < N_LIVE_MIN:
        binding = f"live n: ride arm has {ride_n}/{N_LIVE_MIN} closes (need more deep_pool fires)"
    elif not ok_conc:
        if conc["distinct"] < MIN_DISTINCT_MINTS:
            binding = (f"concentration: only {conc['distinct']}/{MIN_DISTINCT_MINTS} distinct mints "
                       f"in the live cohort (need more DISTINCT tokens, not more re-trades)")
        else:
            binding = (f"concentration: one mint ({(conc['top_mint'] or '?')[:10]}) is "
                       f"{conc['top_share']:.0%} of the cohort (>{MAX_MINT_SHARE:.0%}) — verdict rests on one token")
    elif not ok_live:
        binding = "live ride arm mean < bank arm mean (ride not yet ahead live)"
    elif not ok_cal:
        binding = "calibration: shadow ride-model disagrees with live (model gap)"
    else:
        binding = "—  CONCLUSIVE"

    return {
        "conclusive": conclusive,
        "binding": binding,
        "checks": {"ordering": ok_order, "live": ok_live, "calibration": ok_cal, "concentration": ok_conc},
        "cf_means": cf, "cf_n": cf_n, "cf_ev_lo_ride": cf_ev_lo,
        "ride": {"n": ride_n, "mean": ride_mean, "dedup_mean": _dedup_mean(ride_rows),
                 "distinct": len({m for m, _ in ride_rows}), "ghost": ride_gh},
        "bank": {"n": bank_n, "mean": bank_mean, "dedup_mean": _dedup_mean(bank_rows),
                 "distinct": len({m for m, _ in bank_rows}), "ghost": bank_gh},
        "concentration": conc,
        "N_LIVE_MIN": N_LIVE_MIN, "MAX_MINT_SHARE": MAX_MINT_SHARE,
        "MIN_DISTINCT_MINTS": MIN_DISTINCT_MINTS,
    }


def ride_exit_proven():
    """(bool, detail) for arm_genes — FAIL-CLOSED: any exception ⇒ NOT proven (don't arm)."""
    try:
        d = evaluate()
        return d["conclusive"], d
    except Exception as e:
        return False, {"error": repr(e), "conclusive": False, "binding": f"error: {e!r}"}


def _f(x, pct=True):
    if x is None:
        return "  —  "
    return f"{x:+.2f}%" if pct else f"{x}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction", type=float, default=1.5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = evaluate(args.friction)
    if args.json:
        print(json.dumps(d, indent=2, default=lambda o: None))
        return

    cf = d["cf_means"]
    print("═" * 76)
    print("  RIDE-EXIT A/B — is the ride exit conclusively proven (may sizing arm)?")
    print("═" * 76)
    print("  [1] COUNTERFACTUAL exit-dependence (forward_obs KEPT cohort, the robust signal)")
    print(f"        ride {_f(cf['ride'])}   ≫   hold {_f(cf['hold'])}   ≫   tpsl {_f(cf['tpsl'])}"
          f"    (n={d['cf_n']}, ev_lo_ride {_f(d['cf_ev_lo_ride'])})")
    ok = d["checks"]["ordering"]
    print(f"        → ordering ride>hold>tpsl: {'✓ holds' if ok else '✗ broke'}")
    print()
    print("  [2] LIVE A/B — realized deep_pool/brain_rule closes (gate-aligned, non-ghost)")
    rd, bk = d["ride"], d["bank"]
    print(f"        RIDE arm (bot1): n={rd['n']:>2} ({rd['distinct']} mints)  mean {_f(rd['mean'])}"
          f"  ·  per-mint {_f(rd['dedup_mean'])}  (ghost {rd['ghost']})")
    print(f"        BANK arm (b2/3): n={bk['n']:>2} ({bk['distinct']} mints)  mean {_f(bk['mean'])}"
          f"  ·  per-mint {_f(bk['dedup_mean'])}  (ghost {bk['ghost']})")
    gap = (rd['mean'] - bk['mean']) if (rd['mean'] is not None and bk['mean'] is not None) else None
    print(f"        → ride − bank gap: {_f(gap)}   ·   real-n bar: ride n≥{d['N_LIVE_MIN']}"
          f"  {'✓' if rd['n'] >= d['N_LIVE_MIN'] else '✗ (' + str(d['N_LIVE_MIN']-rd['n']) + ' short)'}")
    print()
    print("  [3] CALIBRATION — shadow ride-model vs live ride arm")
    print(f"        shadow {_f(cf['ride'])}  vs  live {_f(rd['mean'])}   "
          f"{'✓ ≈' if d['checks']['calibration'] else '✗ gap (or n=0)'}")
    print()
    c = d["concentration"]
    okc = d["checks"]["concentration"]
    tm = (c["top_mint"] or "—")[:10]
    print("  [4] CONCENTRATION — is the live cohort one lucky token? (S105-audit)")
    print(f"        distinct mints: {c['distinct']:>2} (need ≥{d['MIN_DISTINCT_MINTS']})   ·   "
          f"top mint {tm} = {c['top_share']:.0%} of {c['n']} closes (cap {d['MAX_MINT_SHARE']:.0%})")
    print(f"        top mint Σreturn contribution: {_f(c['top_ret_pts'])}   "
          f"→ {'✓ diversified' if okc else '✗ single-token dominated'}")
    print("  " + "─" * 72)
    verdict = "✅ CONCLUSIVE — ride proven; sizing MAY arm" if d["conclusive"] else \
              "⏳ NOT CONCLUSIVE — sizing stays OFF"
    print(f"  {verdict}")
    print(f"  binding constraint: {d['binding']}")
    if not d["conclusive"]:
        print("  note: more deep_pool fires are the only way to settle the live arms. The")
        print("        quality-preserving accelerator is the gate_unfreeze normal-slice")
        print("        (filling & bs≥1.5, gate-judged) — operator's call; NOT applied here.")
    print("═" * 76)


if __name__ == "__main__":
    main()
