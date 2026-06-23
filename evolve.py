#!/usr/bin/env python3
"""
evolve.py — S85 evolution driver/tick  (run on demand; safe — never forces the gate, never writes genes)

"Evolving the bots" honestly = drive the existing evidence→brain→gate loop forward and capture the
trajectory. Each tick:
  1. Triggers the brain's evolution step (`strategy_brain.py --evolve`) — the sanctioned, evidence-gated
     rule-promotion mechanism (the same thing the hourly loop runs). Detects live_rule promotions.
  2. Reads the deploy gate (prestige_tracker — single source of truth). REPORT-ONLY: never passes --arm,
     never writes ev_sizing.json. gene_arm_loop arms genes automatically when the gate passes.
  3. Shows the per-regime SHADOW EV (from forward_obs) — the cross-regime "data for the genes" WITHOUT
     trading −EV: this is how we learn which regimes are +EV without bleeding SOL on the bad ones.
  4. admit_guard proven cells + the Bot1(sidecar-lvel ON) vs Bot2/3 brain_rule A/B + balances.
  5. Appends a timestamped snapshot to shared_memory/evolve_log.jsonl and prints the delta vs the last tick.

    python3 evolve.py                 # one tick
    python3 evolve.py --loop 6 --interval 600   # 6 ticks over the hour (use run_in_background)
    python3 evolve.py --no-brain      # skip the brain trigger (pure measurement)
"""
import json, subprocess, sqlite3, time, argparse, math, statistics as st
from pathlib import Path
from datetime import datetime, timezone

import regime_ev as R
from prestige_tracker import _fleet_deep_pool_stats, MIN_CLOSES, GHOST_MAX
import admit_guard as AG

LOG = Path("shared_memory/evolve_log.jsonl")
BRAIN = Path("shared_memory/strategy_brain.json")
# S79 regime bands (keyed on aggregate 5m vol — same driver the live regime selector uses)
REGIME_BANDS = [("euphoria", 600_000, 1e18), ("aggressive", 280_000, 600_000),
                ("normal", 110_000, 280_000), ("sniper", 48_000, 110_000), ("dead", 0, 48_000)]

def _regime(agg):
    for name, lo, hi in REGIME_BANDS:
        if lo <= agg < hi:
            return name
    return "dead"

def _ev_lo(xs):
    if len(xs) < 2: return float("nan")
    return st.mean(xs) - 1.64 * st.pstdev(xs) / math.sqrt(len(xs))

def _brain_live_rule():
    try:
        return json.loads(BRAIN.read_text()).get("live_rule")
    except Exception:
        return None

def trigger_brain():
    before = _brain_live_rule()
    try:
        p = subprocess.run(["python3", "strategy_brain.py", "--evolve"],
                           capture_output=True, text=True, timeout=180)
        tail = (p.stdout or "").strip().splitlines()[-3:]
    except Exception as e:
        tail = [f"(brain run failed: {e})"]
    after = _brain_live_rule()
    return before, after, tail

def regime_shadow_ev():
    """Per-regime deep_pool-cohort EV from forward_obs (SHADOW — observed, not traded). Answers
    'is deep_pool +EV in normal / dead / etc.' WITHOUT risking SOL."""
    recs = []
    try:
        with open("shared_memory/forward_obs.jsonl") as f:
            for L in f:
                try:
                    r = json.loads(L)
                    if r.get("fwd") is not None:
                        recs.append(r)
                except Exception:
                    pass
    except Exception:
        return {}
    g = lambda r, k, d=0.0: (r.get(k) if r.get(k) is not None else d)
    out = {}
    for name, _lo, _hi in REGIME_BANDS:
        # deep_pool proxy: deep, sellable pool with momentum (mirrors _DEEP_POOL_M5_MIN≈1)
        broad = [g(r, "fwd") for r in recs if _regime(g(r, "agg")) == name and g(r, "liq") > 2000 and g(r, "m5") >= 1]
        # strict/filling = the BRAIN's deep_pool_strict_filling predicate ON THE REALIZABLE set (liq≥$50k —
        # the live deep_pool admission requires deep/sellable pools, so this is the decision-relevant basis).
        strict = [g(r, "fwd") for r in recs if _regime(g(r, "agg")) == name and g(r, "liq", 0) >= 50_000
                  and g(r, "m5") >= 2 and g(r, "liq_fdv", 0) >= 0.10 and g(r, "bs") >= 1 and g(r, "lqv") > 0.01]
        out[name] = {
            "broad_n": len(broad), "broad_evlo": round(_ev_lo(broad), 2) if len(broad) >= 2 else None,
            "broad_mean": round(st.mean(broad), 2) if broad else None,
            "strict_n": len(strict), "strict_evlo": round(_ev_lo(strict), 2) if len(strict) >= 2 else None,
            "strict_mean": round(st.mean(strict), 2) if strict else None,
        }
    return out

def ab_brain_rule(since_ts=None):
    """Per-bot brain_rule activity (the sidecar-lvel A/B) + balance."""
    out = {}
    for b in R.BOTS:
        if not Path(f"bots/bot{b}/trades.db").exists():
            continue   # skip unfunded/not-yet-provisioned bots (4-6)
        d = {"brain_signals": 0, "brain_tx": 0, "bal": None}
        try:
            c = sqlite3.connect(f"file:bots/bot{b}/trades.db?mode=ro", uri=True)
            d["brain_signals"] = c.execute(
                "SELECT COUNT(*) FROM trades WHERE event='signal' AND data LIKE '%brain_rule%'").fetchone()[0]
            d["brain_tx"] = c.execute(
                "SELECT COUNT(*) FROM trades WHERE data LIKE '%brain_rule%' AND event IN ('tx_result','close')").fetchone()[0]
            c.close()
        except Exception:
            pass
        try:
            last = Path(f"bots/bot{b}/balance_history.jsonl").read_text().strip().splitlines()[-1]
            d["bal"] = round(json.loads(last).get("sol_balance", 0), 4)
        except Exception:
            pass
        out[f"bot{b}"] = d
    return out

def snapshot(run_brain=True):
    n, net, ghosts, gr = _fleet_deep_pool_stats()
    before = after = None; brain_tail = []
    if run_brain:
        before, after, brain_tail = trigger_brain()
    else:
        after = _brain_live_rule()
    ag_stats = AG.cell_stats()
    proven = {f"{p}×{r}": AG.verdict_for(p, r, ag_stats).action
              for (p, r) in ag_stats if AG.verdict_for(p, r, ag_stats).action != "NEUTRAL"}
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "gate": {"n": n, "net": round(net, 5), "ghosts": ghosts, "ghost_rate": round(gr, 4),
                 "n_need": MIN_CLOSES, "passed": (net >= 0 and gr <= GHOST_MAX and n >= MIN_CLOSES)},
        "brain_live_rule": after, "brain_promoted": (before != after and before is not None),
        "brain_tail": brain_tail,
        "regime_shadow_ev": regime_shadow_ev(),
        "admit_proven": proven,
        "ab": ab_brain_rule(),
    }

def _last_snapshot():
    if not LOG.exists():
        return None
    try:
        return json.loads(LOG.read_text().strip().splitlines()[-1])
    except Exception:
        return None

def _print(snap, prev):
    g = snap["gate"]
    print("=" * 74)
    print(f"  EVOLVE TICK · {snap['ts'][:19]}Z")
    print("=" * 74)
    # gate + delta
    dn = dnet = ""
    if prev:
        dn = f"  (Δ {g['n']-prev['gate']['n']:+d})"
        dnet = f"  (Δ {g['net']-prev['gate']['net']:+.5f})"
    print(f"  GATE: n={g['n']}/{g['n_need']}{dn} · net ◎{g['net']:+.5f}{dnet} · ghost {g['ghost_rate']:.0%} "
          f"→ {'🚀 PASS' if g['passed'] else 'DORMANT'}")
    # brain
    pr = " ★PROMOTED" if snap["brain_promoted"] else ""
    print(f"  BRAIN live_rule: {snap['brain_live_rule']}{pr}")
    # regime shadow EV (the operator's 'data for the genes' — safe/observed)
    print("  REGIME SHADOW EV (forward_obs, observed-not-traded · ev_lo ◎%/30min):")
    print(f"    {'regime':10} {'deep_pool(broad)':>22} {'strict/filling':>22}   live?")
    for name, _lo, _hi in REGIME_BANDS:
        r = snap["regime_shadow_ev"].get(name, {})
        bw = f"n={r.get('broad_n',0)} ev_lo={r.get('broad_evlo')}" if r.get('broad_n') else "n=0"
        st_ = f"n={r.get('strict_n',0)} ev_lo={r.get('strict_evlo')}" if r.get('strict_n') else "n=0"
        live = "SKIPPED(−EV)" if name in ("dead", "normal") else "✓ live (+EV)"
        print(f"    {name:10} {bw:>22} {st_:>22}   {live}")
    # admit_guard
    print(f"  ADMIT-GUARD proven: {snap['admit_proven'] or 'none (all NEUTRAL — inert until n≥8)'}")
    # A/B
    print("  A/B (Bot1 sidecar-lvel ON vs Bot2/3 OFF) — brain_rule activity + balance:")
    for bot, d in snap["ab"].items():
        flag = " ←canary" if bot == "bot1" else ""
        print(f"    {bot}: brain_signals={d['brain_signals']} brain_tx={d['brain_tx']} bal=◎{d['bal']}{flag}")
    print("=" * 74)

def tick(run_brain=True):
    prev = _last_snapshot()
    snap = snapshot(run_brain=run_brain)
    with open(LOG, "a") as f:
        f.write(json.dumps(snap) + "\n")
    _print(snap, prev)
    return snap

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--no-brain", action="store_true")
    a = ap.parse_args()
    for i in range(a.loop):
        tick(run_brain=not a.no_brain)
        if i < a.loop - 1:
            time.sleep(a.interval)
