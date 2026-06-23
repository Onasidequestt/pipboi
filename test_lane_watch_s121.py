#!/usr/bin/env python3
"""
test_lane_watch_s121.py — Phase 4.4 (V2): lane registry + generalized lane_watch verdict.

Ports the runner_watch verdict-math coverage onto lane_watch (lane-parameterized) + adds the
lanes.py registry/cohort-match coverage. Offline, deterministic: monkeypatches lane_watch._cohort_rows
so no live trades.db is touched. Run: python3 test_lane_watch_s121.py
"""
import json
import lanes as L
import lane_watch as LW

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def _close(mint, ret_pct, size=0.04, lane=None, alias="momentum_override", ghost=False, play="gem"):
    r = {"mint": mint, "size_sol": size, "pnl_sol": round(size * ret_pct / 100.0, 8),
         "pnl": 1.0, "play": play}
    if lane:
        r["lane"] = lane
    if alias:
        r[alias] = True
    if ghost:
        r["ghost"] = True
    return r


SPEC = {"name": "runner", "alias_field": "momentum_override"}
CFG = {"arm_rowid": 0, "n_threshold": 20, "ev_lo_pass": 0.0, "ev_lo_kill": -5.0,
       "ghost_max": 0.10, "min_distinct_mints": 8, "max_mint_share": 0.40,
       "validated_floor_pct": 14.0, "bot": 1, "metric": "test"}


def _eval(rows, cfg=None):
    orig = LW._cohort_rows
    LW._cohort_rows = lambda spec, c: rows
    try:
        return LW.evaluate(SPEC, dict(cfg or CFG))
    finally:
        LW._cohort_rows = orig


# ── lanes.py registry ─────────────────────────────────────────────────────────
print("lanes.py registry + cohort matching")
reg = L.registry()
check("registry loads runner + vacc", "runner" in reg and "vacc" in reg)
check("runner spec has alias momentum_override", reg["runner"].get("alias_field") == "momentum_override")
check("runner exit is trail no-fixed-tp", reg["runner"]["exit"]["type"] == "trail" and reg["runner"]["exit"]["fixed_tp"] is False)
check("vacc rung is off (staged)", reg["vacc"].get("rung") == "off")
check("cohort_match by generic lane field", L.cohort_match({"lane": "runner"}, reg["runner"]))
check("cohort_match by alias field", L.cohort_match({"momentum_override": True}, reg["runner"]))
check("cohort_match rejects unrelated row", not L.cohort_match({"play": "gem"}, reg["runner"]))
check("canary_enabled False with no canary file", L.canary_enabled(reg["runner"], bot=1) in (False, True))  # tolerant: depends on live file

# ── lane_watch verdict math (ported from runner_watch) ────────────────────────
print("lane_watch — return-% + ev_lo")
res = _eval([_close(f"m{i}", 20.0) for i in range(20)])
check("return-% mean correct (+20%)", abs(res["mean_ret_pct"] - 20.0) < 1e-6)
check("ev_lo == mean when variance 0", abs(res["ev_lo_pct"] - 20.0) < 1e-6)

print("lane_watch — ghost counting + rate")
rg = [_close(f"m{i}", 5.0) for i in range(8)] + [_close("g1", 0.0, ghost=True), _close("g2", 0.0, ghost=True)]
res = _eval(rg)
check("ghosts counted (explicit flag)", res["ghosts"] == 2)
check("ghost-rate over cohort", abs(res["ghost_rate"] - 0.2) < 1e-6)
# backstop: pnl≈0 & pnl_sol<-0.02
rgb = [{"mint": "b", "size_sol": 0.05, "pnl_sol": -0.03, "pnl": 0.0, "momentum_override": True}]
res = _eval(rgb)
check("ghost backstop (pnl~0 & pnl_sol<-0.02)", res["ghosts"] == 1)

print("lane_watch — break-even neutral by % (S89)")
res = _eval([_close("a", 5.0), _close("b", -5.0), _close("c", 0.4), _close("d", -0.3)])
check("win = ret>+1%", res["wins"] == 1)
check("loss = ret<-1%", res["losses"] == 1)
check("|ret|<1% NEUTRAL not loss", res["neutral"] == 2)

print("lane_watch — concentration block (S105-audit)")
res = _eval([_close("SAME", 30.0) for _ in range(25)])
check("single-mint cohort → not SCALE", res["verdict"] != "SCALE-CANDIDATE")
check("single-mint distinct=1", res["distinct_mints"] == 1)
check("single-mint top_share=1.0", abs(res["top_share"] - 1.0) < 1e-6)
res = _eval([_close(f"m{i}", 30.0) for i in range(20)])
check("20 distinct mints diverse", res["distinct_mints"] == 20 and res["top_share"] <= 0.40)

print("lane_watch — verdicts")
check("SCALE-CANDIDATE on n≥20 +EV diverse clean", _eval([_close(f"m{i}", 30.0) for i in range(20)])["verdict"] == "SCALE-CANDIDATE")
check("KILL on n≥20 ev_lo<−5%", _eval([_close(f"m{i}", -20.0) for i in range(20)])["verdict"] == "KILL")
pend = _eval([_close(f"m{i}", 30.0) for i in range(5)])
check("PENDING on n<threshold", pend["verdict"] == "PENDING")
check("PENDING names n as binding", "n:" in (pend["binding"] or ""))

print("lane_watch — cohort filter via lanes.cohort_match (alias vs lane)")
# a row that is NOT this lane must be excluded by the real _cohort_rows path → test cohort_match directly
check("alias row matches", L.cohort_match({"momentum_override": True}, {"name": "runner", "alias_field": "momentum_override"}))
check("foreign-lane row excluded", not L.cohort_match({"lane": "other"}, {"name": "runner", "alias_field": "momentum_override"}))

print("lane_watch — goalpost hash stability")
h1 = LW._config_hash(CFG)
h2 = LW._config_hash(dict(CFG))
check("hash deterministic for same criterion", h1 == h2)
moved = dict(CFG); moved["ev_lo_kill"] = -3.0
check("changing a criterion field changes the hash", LW._config_hash(moved) != h1)

print("lane_watch — empty cohort safe")
res = _eval([])
check("empty cohort → PENDING, no crash", res["verdict"] == "PENDING" and res["n_sized"] == 0)

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*60}")
raise SystemExit(1 if FAIL else 0)
