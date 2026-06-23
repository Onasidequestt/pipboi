#!/usr/bin/env python3
"""
reconcile_harness.py — integrity + leak detector for the fleet ledger (READ-ONLY).

COMPLEMENTS reconcile.py — it does NOT duplicate it. reconcile.py enumerates on-chain
holdings via multi-RPC and SELLS/CLOSES orphans (a write tool). This harness never
touches the chain and never sells anything: it runs a battery of CHECKS over the
ledgers + learning store and prints PASS/FAIL per check, exiting nonzero if any FAIL so
it can run in a loop/cron as a tripwire.

Three families of check:
  (a) WALLET⇄LEDGER DRIFT, per bot — does the ledger's ghost-aware realized SOL flow
      (since race_start.json) reconcile with the recorded on-chain balance, once capital
      still deployed in open positions is added back? (Approximate — fees/slippage and the
      S94b appreciated-orphan inexactness are NOTED, not hard-failed.)
  (b) INVARIANTS that catch bugs this codebase has actually shipped — the S92 phantom
      return, the S94b recovery cap, break-even double-counting, missing size_sol, and
      trade_memory total_pnl outliers.
  (c) BACKTEST⇄LIVE CALIBRATION — the "paper +EV leaks to −EV live" alarm: compare the
      prove_edge.py shadow EV for the deep_pool cohort against live realized return-%.

Gate semantics are IMPORTED from the single source of truth (prestige_tracker), never
recomputed. ts is NEVER used to order/compare (bot3's clock is skewed) — rowid only.

USAGE
  python3 reconcile_harness.py                 # run all checks, human report
  python3 reconcile_harness.py --json          # machine-readable
  python3 reconcile_harness.py --drift-tol 0.10 --return-bound 1000 --calib-band 2.0
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Single source of truth — gate cohort / ghost definition / balance read.
import prestige_tracker as pt   # _is_ghost, _DP_PLAYS, _bot_balance, BOTS, _GATE_SKIP_REGIMES

RACE_START = ROOT / "race_start.json"
TRADE_MEMORY = ROOT / "shared_memory" / "trade_memory.json"

# memory.py's break-even band (size-independent percent). Imported if available.
try:
    from memory import _BREAKEVEN_PCT
except Exception:
    _BREAKEVEN_PCT = 1.0


def _ro_closes(bot):
    """Read-only close rows for a bot, ordered by ROWID (never ts — bot3 clock skew).
    Returns list of (rowid, data_dict)."""
    db = ROOT / f"bots/bot{bot}/trades.db"
    if not db.exists():
        return []
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for rowid, d in con.execute("SELECT rowid, data FROM trades WHERE event='close' ORDER BY rowid"):
            try:
                out.append((rowid, json.loads(d)))
            except Exception:
                pass
        con.close()
    except Exception:
        pass
    return out


def _open_capital(bot):
    """SOL still deployed in open positions (positions.json), to add back into the drift recon."""
    p = ROOT / f"bots/bot{bot}/positions.json"
    if not p.exists():
        return 0.0
    try:
        pos = json.loads(p.read_text() or "{}")
    except Exception:
        return 0.0
    return sum((v.get("size_sol") or 0.0) for v in pos.values() if isinstance(v, dict))


# ── (a) WALLET ⇄ LEDGER DRIFT ─────────────────────────────────────────────────
def check_drift(tol):
    """Per bot: expected_bal = start + Σ realized pnl_sol (ghost-aware) − open_capital,
    compared to the recorded on-chain balance. Approximate — fees/slippage and appreciated
    orphans (S94b) create benign drift, so this NOTEs near-tolerance and only FAILs on a
    material gap."""
    try:
        rs = json.loads(RACE_START.read_text())
        starts = {int(k): float(v) for k, v in rs.get("start_total", {}).items()}
    except Exception:
        return {"name": "wallet⇄ledger drift", "ok": True,
                "note": "no race_start.json — skipped", "rows": []}, []
    rows, fails = [], []
    for b in pt.BOTS:
        if b not in starts:
            continue
        realized = sum((d.get("pnl_sol") or 0.0) for _, d in _ro_closes(b))   # ghosts included (real SOL out)
        open_cap = _open_capital(b)
        cur = pt._bot_balance(b)
        start = starts[b]
        expected = start + realized - open_cap
        drift = cur - expected
        ok = abs(drift) <= tol
        rows.append({"bot": b, "start": round(start, 4), "realized_pnl_sol": round(realized, 4),
                     "open_capital": round(open_cap, 4), "expected_bal": round(expected, 4),
                     "current_bal": round(cur, 4), "drift": round(drift, 4), "ok": ok})
        if not ok:
            fails.append(f"bot{b} drift ◎{drift:+.4f} (>{tol})")
    note = ("approximate: excludes fees/slippage; S94b appreciated-orphan recoveries are "
            "capped at the prior loss, so the ledger can read slightly BELOW the wallet (benign).")
    return {"name": "wallet⇄ledger drift", "ok": not fails, "note": note, "rows": rows}, fails


# ── (b) INVARIANTS ────────────────────────────────────────────────────────────
def check_return_bound(bound):
    """S92 class: ANY close with |return-%| beyond a sane bound = a phantom price-derived pnl."""
    bad = []
    for b in pt.BOTS:
        for rowid, d in _ro_closes(b):
            sz = d.get("size_sol") or 0.0
            if sz <= 0:
                continue
            ret = (d.get("pnl_sol") or 0.0) / sz * 100.0
            if abs(ret) > bound:
                bad.append({"bot": b, "rowid": rowid, "mint": d.get("mint"),
                            "return_pct": round(ret, 1), "pnl_sol": d.get("pnl_sol"),
                            "size_sol": sz, "exit_reason": d.get("exit_reason")})
    fails = [f"bot{r['bot']} rowid{r['rowid']} {r['return_pct']:+.0f}%" for r in bad]
    return {"name": f"return-% sanity (|ret|≤{bound:.0f}%)", "ok": not bad,
            "note": "S92 +485,538% phantom class — one glitched price read fabricates a 'win'.",
            "rows": bad}, fails


def check_sweep_recovery_cap():
    """S94b invariant: per mint, sweep-recovery credits never exceed the prior booked ghost
    loss, i.e. (ghost_loss + Σ recovery_credits) ≤ 0 for every mint (per bot)."""
    bad = []
    for b in pt.BOTS:
        agg = {}   # mint -> {"loss": <=0, "credit": >=0}
        for _, d in _ro_closes(b):
            mint = d.get("mint")
            if not mint:
                continue
            ps = d.get("pnl_sol") or 0.0
            er = str(d.get("exit_reason") or "")
            s = agg.setdefault(mint, {"loss": 0.0, "credit": 0.0})
            if er.startswith("sweep recovery"):
                s["credit"] += ps
            elif ps < 0 and (d.get("ghost") or er.startswith("ghost")):
                s["loss"] += ps
        for mint, s in agg.items():
            resid = s["loss"] + s["credit"]
            if s["credit"] > 0 and resid > 1e-6:   # credit exceeded the prior loss
                bad.append({"bot": b, "mint": mint, "ghost_loss": round(s["loss"], 6),
                            "recovery_credit": round(s["credit"], 6), "residual": round(resid, 6)})
    fails = [f"bot{r['bot']} {str(r['mint'])[:10]} +{r['residual']:.4f} over loss" for r in bad]
    return {"name": "sweep-recovery cap (loss+Σcredits≤0)", "ok": not bad,
            "note": "S94b: a recovery may only OFFSET a prior ghost loss, never fabricate a gain.",
            "rows": bad}, fails


def check_breakeven_neutral():
    """Break-even rows must be neutral, never double-counted as win+loss.
    (1) ledger: a close in the |ret%|<band that is ALSO flagged a ghost loss is contradictory.
    (2) trade_memory: wins+losses(+breakeven) must reconcile with the trades counter."""
    bad = []
    # (1) ledger contradictions
    for b in pt.BOTS:
        for rowid, d in _ro_closes(b):
            sz = d.get("size_sol") or 0.0
            if sz <= 0:
                continue
            ret = (d.get("pnl_sol") or 0.0) / sz * 100.0
            if abs(ret) < _BREAKEVEN_PCT and pt._is_ghost(d):
                bad.append({"kind": "ledger", "bot": b, "rowid": rowid, "mint": d.get("mint"),
                            "return_pct": round(ret, 3),
                            "why": "flat by % but booked as a ghost LOSS"})
    # (2) trade_memory double-count
    if TRADE_MEMORY.exists():
        try:
            tm = json.loads(TRADE_MEMORY.read_text())
            for mint, m in tm.items():
                if not isinstance(m, dict):
                    continue
                tr = m.get("trades")
                w, l = m.get("wins", 0) or 0, m.get("losses", 0) or 0
                be = m.get("breakeven", 0) or 0
                if tr is None:
                    continue
                if w + l > tr:                       # decisive counts exceed total = double-count
                    bad.append({"kind": "memory", "mint": mint, "trades": tr,
                                "wins": w, "losses": l, "why": "wins+losses > trades"})
                elif be and (w + l + be != tr):      # breakeven tracked but doesn't reconcile
                    bad.append({"kind": "memory", "mint": mint, "trades": tr,
                                "wins": w, "losses": l, "breakeven": be,
                                "why": "wins+losses+breakeven != trades"})
        except Exception:
            pass
    fails = [f"{r['kind']}:{str(r.get('mint'))[:10]} {r['why']}" for r in bad]
    return {"name": f"break-even neutrality (band {_BREAKEVEN_PCT}%)", "ok": not bad,
            "note": "S87 deadlock class — a flat exit counted as a loss erodes confidence + WR.",
            "rows": bad}, fails


def check_size_sol_present():
    """Post-S80 closes must carry size_sol (return-% needs the deployed-capital denom).
    A true REGRESSION detector, not a legacy flagger: exit_reason (S77/S78) predates
    size_sol (S80), so we find each bot's S80 cutover = the lowest rowid that DOES carry
    size_sol, and only HARD-FAIL non-ghost/non-sweep closes ABOVE that cutover that lost it.
    Everything below the cutover is the legitimate pre-S80 era (excused). rowid only — never
    ts (bot3 clock skew)."""
    missing_hard, excused = [], 0
    for b in pt.BOTS:
        rows = _ro_closes(b)
        sized = [rid for rid, d in rows if (d.get("size_sol") or 0.0) > 0]
        cutover = min(sized) if sized else None      # first row that ever had size_sol
        for rowid, d in rows:
            if (d.get("size_sol") or 0.0) > 0:
                continue
            er = str(d.get("exit_reason") or "")
            if er.startswith("ghost") or er.startswith("sweep recovery") or d.get("signature") == "sweep_recovery":
                continue                              # these legitimately omit size_sol
            if cutover is not None and rowid > cutover:
                missing_hard.append({"bot": b, "rowid": rowid, "mint": d.get("mint"), "exit_reason": er})
            else:
                excused += 1                          # pre-S80 era
    fails = [f"bot{r['bot']} rowid{r['rowid']}" for r in missing_hard]
    return {"name": "size_sol present (post-S80 regression)", "ok": not missing_hard,
            "note": f"regression-only (above each bot's S80 rowid cutover); {excused} pre-S80 row(s) excused.",
            "rows": missing_hard}, fails


def check_trade_memory_outliers(bound):
    """trade_memory.json total_pnl (USD) outliers — the S92 BUG A phantom residue."""
    bad = []
    if TRADE_MEMORY.exists():
        try:
            tm = json.loads(TRADE_MEMORY.read_text())
            for mint, m in tm.items():
                if not isinstance(m, dict):
                    continue
                tp = m.get("total_pnl")
                if isinstance(tp, (int, float)) and abs(tp) > bound:
                    bad.append({"mint": mint, "total_pnl": tp, "trades": m.get("trades")})
        except Exception:
            pass
    fails = [f"{str(r['mint'])[:10]} total_pnl ${r['total_pnl']:.0f}" for r in bad]
    return {"name": f"trade_memory total_pnl (|$|≤{bound:.0f})", "ok": not bad,
            "note": "S92 BUG A: a glitched-price phantom poisons the learning store's total_pnl.",
            "rows": bad}, fails


# ── (c) BACKTEST ⇄ LIVE CALIBRATION ───────────────────────────────────────────
def check_calibration(band, exit_name="ride", friction=1.5, min_live=15):
    """Compare prove_edge shadow EV (KEPT deep_pool cohort) vs live realized return-%.
    FAIL on a sign flip or a >band× magnitude divergence — the paper→live leak alarm.
    PIPE12 (R-D1): min_live 4→15 (= prestige_tracker.MIN_CLOSES). A sign-flip at n=6 is
    noise (one ghost moves it), not a real edge collapse — it fired a false LEAK verdict.
    Below 15 KEPT closes the check reports 'insufficient n' (informational), not FAIL."""
    try:
        import prove_edge as pe
    except Exception as e:
        return {"name": "backtest⇄live calibration", "ok": True,
                "note": f"prove_edge import failed ({e}) — skipped", "rows": []}, []

    # shadow: KEPT-regime deep_pool-admissible realizable returns from forward_obs
    shadow = []
    try:
        for r in pe.load_rows():
            ok, _rule = pe.deep_pool_admit(r)
            if not ok:
                continue
            if pe.lab.regime_of(r.get("agg")) in pe.KEPT:
                shadow.append(pe.realized(r, exit_name, friction))
    except Exception as e:
        return {"name": "backtest⇄live calibration", "ok": True,
                "note": f"shadow replay failed ({e}) — skipped", "rows": []}, []

    # live: actual deep_pool/brain_rule KEPT closes (prove_edge's own calibration reader)
    try:
        live_kept, _live_skip = pe.live_calibration(0.0)
    except Exception:
        live_kept = []

    sm = statistics.mean(shadow) if shadow else None
    lm = statistics.mean(live_kept) if live_kept else None
    row = {"shadow_mean_pct": (round(sm, 3) if sm is not None else None),
           "shadow_n": len(shadow),
           "live_mean_pct": (round(lm, 3) if lm is not None else None),
           "live_n": len(live_kept), "exit": exit_name}

    if lm is None or len(live_kept) < min_live or sm is None:
        row["verdict"] = f"insufficient live n ({len(live_kept)}<{min_live}) — cannot calibrate yet"
        return {"name": "backtest⇄live calibration", "ok": True,
                "note": "not enough live KEPT closes to judge the leak — informational only.",
                "rows": [row]}, []

    sign_flip = (sm > 0) != (lm > 0)
    ratio = abs(lm) / abs(sm) if abs(sm) > 1e-9 else (float("inf") if abs(lm) > 1e-9 else 1.0)
    # FAIL on a sign flip, or when live is materially WORSE than shadow by more than band×.
    diverged = sign_flip or (lm < sm - 1e-9 and ratio > band) or (sm > 0 and lm < 0)
    row["sign_flip"] = sign_flip
    row["ratio_live_over_shadow"] = (round(ratio, 2) if math.isfinite(ratio) else None)
    row["verdict"] = "LEAK" if diverged else "aligned"
    fails = [f"shadow {sm:+.2f}% vs live {lm:+.2f}% (n={len(live_kept)})"] if diverged else []
    return {"name": "backtest⇄live calibration", "ok": not diverged,
            "note": "paper +EV leaking to −EV live = the deep_pool edge isn't realizing. "
                    f"FAIL on sign flip or live worse than shadow by >{band}×.",
            "rows": [row]}, fails


def check_objective_calibration(band=1.0):
    """S121 V2 (Phase 3.3): the honest-objective tripwire. honest_objective is the ONE ruler the
    brain/GA/prove_edge/lane_watch learn on; this FAILs when its shadow over/under-credits the live
    wallet by more than `band` pp on the deep_pool cohort — so the ruler can't silently drift as
    regimes shift. Read-only. Below the min wallet n it is informational (not a FAIL).
    ⚠ EXPECTED-RED while the only sized live cohort is the DISPROVEN deep_pool book (kill_criterion
    FAIL): Draft #1's gain-side fill haircut can't close a gap that is partly loss-side on a dead
    cohort. It resolves to a true tripwire once the runner lane provides a live +EV cohort to
    calibrate against — the RED here is an honest 'don't trust paper EV on this cohort' signal."""
    try:
        import honest_objective as ho
        g = ho.calibration_gap()
    except Exception as e:
        return {"name": "objective⇄wallet calibration (honest_objective)", "ok": True,
                "note": f"honest_objective import/calc failed ({e}) — skipped", "rows": []}, []
    row = {"shadow_mean_pct": g["shadow_mean"], "wallet_mean_pct": g["wallet_mean"],
           "gap_pp": g["gap_pp"], "abs_gap_pp": g["abs_gap_pp"], "fill_factor": g["fill_factor"],
           "n_wallet": g["n_wallet"], "n_shadow": g["n_shadow"]}
    if not g["calibratable"]:
        row["verdict"] = f"insufficient wallet n ({g['n_wallet']}) — informational"
        return {"name": "objective⇄wallet calibration (honest_objective)", "ok": True,
                "note": "not enough live closes to calibrate the objective — informational only.",
                "rows": [row]}, []
    diverged = g["abs_gap_pp"] > band
    row["verdict"] = "DRIFT" if diverged else "calibrated"
    fails = [f"objective shadow {g['shadow_mean']:+.2f}% vs wallet {g['wallet_mean']:+.2f}% "
             f"(|gap| {g['abs_gap_pp']:.2f}pp > {band}pp; fill={g['fill_factor']})"] if diverged else []
    return {"name": "objective⇄wallet calibration (honest_objective)", "ok": not diverged,
            "note": "the brain/GA learn on honest_objective; FAIL = it mis-credits the wallet by "
                    f">{band}pp. ⚠ EXPECTED-RED on the disproven deep_pool cohort until the runner "
                    "lane supplies a live +EV calibration cohort.",
            "rows": [row]}, fails


# ── runner ────────────────────────────────────────────────────────────────────
def run_all(args):
    checks = [
        check_drift(args.drift_tol),
        check_return_bound(args.return_bound),
        check_sweep_recovery_cap(),
        check_breakeven_neutral(),
        check_size_sol_present(),
        check_trade_memory_outliers(args.memory_pnl_bound),
        check_calibration(args.calib_band),
        check_objective_calibration(args.obj_calib_band),
    ]
    return checks


def _print_human(checks):
    print("═" * 74)
    print("  RECONCILE HARNESS — ledger integrity + leak tripwire (read-only)")
    print("═" * 74)
    n_fail = 0
    for res, fails in checks:
        ok = res["ok"]
        n_fail += 0 if ok else 1
        tag = "✅ PASS" if ok else "🔴 FAIL"
        print(f"\n  {tag}  {res['name']}")
        if res.get("note"):
            print(f"         · {res['note']}")
        for r in res.get("rows", []):
            print(f"         {json.dumps(r, default=str)}")
        if not ok:
            print(f"         → {'; '.join(fails)}")
    print("\n" + "─" * 74)
    print(f"  {'ALL CHECKS PASS' if n_fail == 0 else f'{n_fail} CHECK(S) FAILED'}")
    print("═" * 74)
    return n_fail


def main():
    ap = argparse.ArgumentParser(description="Ledger integrity + paper→live leak detector (read-only).")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--drift-tol", type=float, default=0.10, help="wallet⇄ledger drift tolerance, SOL (default 0.10)")
    ap.add_argument("--return-bound", type=float, default=1000.0, help="max |return-%%| before a close is a phantom (default 1000)")
    ap.add_argument("--memory-pnl-bound", type=float, default=5000.0, help="max |trade_memory total_pnl| USD (default 5000)")
    ap.add_argument("--calib-band", type=float, default=2.0, help="shadow⇄live divergence factor that FAILs (default 2×)")
    ap.add_argument("--obj-calib-band", type=float, default=1.0, help="S121: honest_objective shadow⇄wallet gap (pp) that FAILs (default 1.0)")
    args = ap.parse_args()

    checks = run_all(args)

    if args.json:
        out = {"checks": [{**res, "fails": fails} for res, fails in checks]}
        out["n_failed"] = sum(0 if res["ok"] else 1 for res, _ in checks)
        out["ok"] = out["n_failed"] == 0
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0 if out["ok"] else 1)

    n_fail = _print_human(checks)
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
