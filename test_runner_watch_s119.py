#!/usr/bin/env python3
"""
test_runner_watch_s119.py — offline tests for runner_watch.py (S119).
No network. Temp DBs only. Exercises return-% math, ev_lo, ghost classification,
the concentration gate, every verdict boundary, and the goalpost-hash warning.
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import runner_watch as rw

_PASS = _FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✓  {name}")
    else:
        _FAIL += 1
        print(f"  ✗  {name}")


def _make_db(rows, start_rowid_padding=0):
    """rows = list of close-event dicts. Returns a ?mode=ro file: URI to a temp db.
    `start_rowid_padding` inserts N filler non-cohort rows first to push rowids up."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE trades (rowid INTEGER PRIMARY KEY, event TEXT, data TEXT)")
    for _ in range(start_rowid_padding):
        c.execute("INSERT INTO trades (event, data) VALUES ('close', ?)",
                  (json.dumps({"play": "gem", "size_sol": 0.01, "pnl_sol": 0.0}),))
    for r in rows:
        r = dict(r)
        r.setdefault("event", "close")
        c.execute("INSERT INTO trades (event, data) VALUES (?, ?)",
                  (r["event"], json.dumps(r)))
    c.commit()
    c.close()
    return f"file:{path}?mode=ro", path


def _close(mint, ret_pct, size=0.04, momentum_override=True, ghost=False, play="gem"):
    """Synthesize a close row with a target realized return-% = pnl_sol/size_sol*100."""
    pnl_sol = (ret_pct / 100.0) * size
    d = {"mint": mint, "size_sol": size, "pnl_sol": pnl_sol, "pnl": pnl_sol * 150.0,
         "play": play, "momentum_override": momentum_override, "regime": "normal"}
    if ghost:
        d.update({"ghost": True, "pnl": 0.0, "pnl_sol": -0.03})
    return d


def _cfg(**over):
    base = {
        "metric": "x", "trial_start_rowid": 0, "n_threshold": 20, "ev_lo_pass": 0.0,
        "ev_lo_kill": -5.0, "ghost_max": 0.10, "min_distinct_mints": 8,
        "max_mint_share": 0.40, "validated_floor_pct": 14.0, "actions": {},
    }
    base.update(over)
    return base


def _eval_with(rows, cfg, padding=0):
    uri, path = _make_db(rows, padding)
    old = rw.DB_URI
    rw.DB_URI = uri
    try:
        return rw.evaluate(cfg)
    finally:
        rw.DB_URI = old
        os.unlink(path)


# ---- 1. return-% + ev_lo math --------------------------------------------------
def test_return_pct_and_ev_lo():
    # all +20% closes → mean 20, ev_lo == mean (zero variance)
    rows = [_close(f"M{i:02d}", 20.0) for i in range(10)]
    res = _eval_with(rows, _cfg(n_threshold=5))
    check("return-% mean correct (+20%)", abs(res["mean_ret_pct"] - 20.0) < 1e-6)
    check("ev_lo == mean when variance 0", abs(res["ev_lo_pct"] - 20.0) < 1e-6)

    # known spread: ev_lo = mean - 1.64*pstdev/sqrt(n)
    vals = [10.0, 30.0]
    res2 = _eval_with([_close(f"M{i}", v) for i, v in enumerate(vals)], _cfg(n_threshold=2))
    mean = 20.0
    se = (((10 - 20) ** 2 + (30 - 20) ** 2) / 2) ** 0.5 / (2 ** 0.5)
    # code rounds ev_lo to 3dp → compare with that tolerance
    check("ev_lo formula matches mean-1.64*SE", abs(res2["ev_lo_pct"] - (mean - 1.64 * se)) < 1e-2)


# ---- 2. ghost classification (imported _is_ghost) ------------------------------
def test_ghost():
    rows = [_close(f"M{i:02d}", 15.0) for i in range(8)] + [_close("G1", 0, ghost=True), _close("G2", 0, ghost=True)]
    res = _eval_with(rows, _cfg(n_threshold=5))
    check("ghosts counted (explicit ghost flag)", res["ghosts"] == 2)
    check("ghost-rate computed over cohort", abs(res["ghost_rate"] - (2 / 10)) < 1e-6)
    # backstop signature: pnl~0 AND pnl_sol<-0.02 with no ghost flag
    g = {"mint": "GB", "size_sol": 0.04, "pnl": 0.0, "pnl_sol": -0.05, "momentum_override": True}
    res2 = _eval_with([g], _cfg(n_threshold=1))
    check("ghost backstop (pnl~0 & pnl_sol<-0.02) caught", res2["ghosts"] == 1)


# ---- 3. win/loss break-even-neutral (S89) --------------------------------------
def test_be_neutral():
    rows = [_close("A", 5.0), _close("B", -5.0), _close("C", 0.3), _close("D", -0.4)]
    res = _eval_with(rows, _cfg(n_threshold=1))
    check("win = ret>+1%", res["wins"] == 1)
    check("loss = ret<-1%", res["losses"] == 1)
    check("|ret|<1% is NEUTRAL not loss (S89)", res["neutral"] == 2)


# ---- 4. concentration gate (S105-audit ≥8 mints / ≤40%) ------------------------
def test_concentration():
    # 25 closes but only 1 mint → distinct=1, top_share=1.0 → blocks SCALE even if +EV
    rows = [_close("ONE", 20.0) for _ in range(25)]
    res = _eval_with(rows, _cfg())
    check("single-mint cohort → not SCALE (concentration block)", res["verdict"] != "SCALE-CANDIDATE")
    check("single-mint distinct=1", res["distinct_mints"] == 1)
    check("single-mint top_share=1.0", abs(res["top_share"] - 1.0) < 1e-6)
    # 25 closes, 10 distinct mints, balanced → diverse
    rows2 = [_close(f"M{i%10:02d}", 20.0) for i in range(25)]
    res2 = _eval_with(rows2, _cfg())
    check("10-mint balanced cohort is diverse", res2["distinct_mints"] == 10 and res2["top_share"] <= 0.40)


# ---- 5. verdict boundaries -----------------------------------------------------
def test_verdicts():
    # SCALE-CANDIDATE: n>=20, +EV, diverse, no ghosts
    rows = [_close(f"M{i%12:02d}", 18.0) for i in range(24)]
    check("SCALE-CANDIDATE on n>=20 +EV diverse clean", _eval_with(rows, _cfg())["verdict"] == "SCALE-CANDIDATE")

    # KILL: n>=20 but ev_lo < -5 (all -10%)
    rk = [_close(f"M{i%12:02d}", -10.0) for i in range(24)]
    check("KILL on n>=20 ev_lo<-5%", _eval_with(rk, _cfg())["verdict"] == "KILL")

    # PENDING: too few closes
    rp = [_close(f"M{i:02d}", 20.0) for i in range(5)]
    rpv = _eval_with(rp, _cfg())
    check("PENDING on n<threshold", rpv["verdict"] == "PENDING")
    check("PENDING names n as binding", "n:" in (rpv["binding"] or ""))

    # PENDING via concentration: n>=20, +EV, but one mint dominates (>40%)
    rc = [_close("BIG", 18.0) for _ in range(15)] + [_close(f"M{i:02d}", 18.0) for i in range(9)]
    rcv = _eval_with(rc, _cfg())
    check("PENDING when one mint >40% even if +EV", rcv["verdict"] == "PENDING" and "concentration" in (rcv["binding"] or ""))

    # PENDING via ghost-rate: n>=20 +EV diverse but ghost-rate >10%.
    # Ghosts are flat-pnl-but-ghost-FLAGGED so they don't tank ev_lo (would be KILL) —
    # isolates the ghost gate's SCALE-block from the EV path.
    flat_ghost = lambda i: {"mint": f"G{i}", "size_sol": 0.04, "pnl": 0.0, "pnl_sol": 0.0,
                            "ghost": True, "momentum_override": True}
    rg = [_close(f"M{i%12:02d}", 18.0) for i in range(24)] + [flat_ghost(i) for i in range(6)]
    rgv = _eval_with(rg, _cfg())
    check("PENDING when ghost-rate >10% (EV still +)", rgv["verdict"] == "PENDING"
          and "ghost" in (rgv["binding"] or ""))


# ---- 6. rowid trial-start filter -----------------------------------------------
def test_rowid_filter():
    # 5 filler rows (rowid 1-5), then 24 cohort rows (rowid 6-29). trial_start=5 → 24 in cohort.
    rows = [_close(f"M{i%12:02d}", 18.0) for i in range(24)]
    res = _eval_with(rows, _cfg(trial_start_rowid=5), padding=5)
    check("rowid>trial_start filter keeps post-start cohort", res["n_cohort"] == 24)
    # trial_start at the very end → 0 cohort
    res2 = _eval_with(rows, _cfg(trial_start_rowid=999), padding=5)
    check("rowid filter excludes everything at/below trial_start", res2["n_cohort"] == 0)


# ---- 7. non-cohort rows excluded (momentum_override=0) -------------------------
def test_cohort_filter():
    rows = [_close(f"M{i:02d}", 18.0, momentum_override=True) for i in range(12)] + \
           [_close(f"X{i:02d}", 18.0, momentum_override=False) for i in range(12)]
    res = _eval_with(rows, _cfg(n_threshold=1))
    check("only momentum_override=1 rows in cohort", res["n_cohort"] == 12)


# ---- 8. goalpost hash ----------------------------------------------------------
def test_goalpost_hash():
    h1 = rw._config_hash(_cfg())
    h2 = rw._config_hash(_cfg(ev_lo_pass=5.0))     # moved the bar
    check("config_hash changes when a criterion field moves", h1 != h2)
    h3 = rw._config_hash(_cfg(actions={"PENDING": "different text"}))  # non-criterion field
    check("config_hash stable when only non-criterion field changes", h1 == h3)


if __name__ == "__main__":
    print("=" * 60)
    print("  runner_watch.py — S119 offline test suite")
    print("=" * 60)
    test_return_pct_and_ev_lo()
    test_ghost()
    test_be_neutral()
    test_concentration()
    test_verdicts()
    test_rowid_filter()
    test_cohort_filter()
    test_goalpost_hash()
    print("=" * 60)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("  " + ("ALL PASS" if _FAIL == 0 else "*** FAILURES ***"))
    print("=" * 60)
    raise SystemExit(1 if _FAIL else 0)
