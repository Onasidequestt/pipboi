#!/usr/bin/env python3
"""
paper_lab/lab.py  —  10-hour paper-trading research workhorse.

PURE STDLIB. READ-ONLY on the real fleet: reads only shared_memory/forward_obs.jsonl
and shared_memory/discovery_snapshot.json. Never imports the core, never writes to
bots/ keys/ genes/ trades.db. All output goes to paper_lab/.

This module is the shared brain for both:
  - the backtest TOURNAMENT (replay forward_obs, large-sample, machine-independent), and
  - the LIVE paper engine (paper_engine.py imports the strategy primitives from here).

A STRATEGY is a JSON-serializable dict:
  {
    "name": str,
    "admit":  {predicate keys ...},          # entry gate
    "confidence": {"type": ..., "params": {}},# P(win)-ish, drives sizing
    "sizing": {"type": ..., ...},             # fraction of equity per trade
    "exit":   {"type": ..., ...},             # realized return given fwd/fwd_min/fwd_max
    "friction_pct": float                     # round-trip cost haircut
  }
"""
import json, math, os, time, heapq, statistics, argparse, glob
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FWD_OBS = os.path.join(ROOT, "shared_memory", "forward_obs.jsonl")
SNAP    = os.path.join(ROOT, "shared_memory", "discovery_snapshot.json")
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
CONFIGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
HORIZON_S = 1800  # 30-min forward horizon (matches signal_lab)

# ----------------------------------------------------------------------------- regime
def regime_of(agg):
    """S79 5-regime ladder, keyed off the global agg_vol_5m."""
    if agg is None: return "unknown"
    if agg >= 600000: return "euphoria"
    if agg >= 280000: return "aggressive"
    if agg >= 110000: return "normal"
    if agg >= 48000:  return "sniper"
    return "dead"

# ----------------------------------------------------------------------------- features
def featurize(r):
    """Derive a dense feature dict from one forward_obs / market_data record.
    Tolerant of missing fields (live snapshot has extra fields; history has fewer)."""
    g = r.get
    liq   = g("liq") or g("liquidity_usd") or 0.0
    mc    = g("mc")  or g("market_cap") or 0.0
    v5    = g("v5")  or g("volume_5m") or 0.0
    v1h   = g("v1h") or g("volume_1h") or 0.0
    m5    = g("m5")  if g("m5") is not None else (g("price_change_5m") or 0.0)
    m1h   = g("m1h") if g("m1h") is not None else (g("price_change_1h") or 0.0)
    bs    = g("bs")
    if bs is None:
        b5 = g("b5") or g("txns_5m_buys") or 0
        s5 = g("s5") or g("txns_5m_sells") or 0
        bs = (b5 / (b5 + s5)) if (b5 + s5) else 0.5
    lqv   = g("lqv") if g("lqv") is not None else 0.0
    liq_mc = g("liq_mc")
    if liq_mc is None:
        liq_mc = (liq / mc) if mc else 0.0
    agg   = g("agg") if g("agg") is not None else g("agg_vol_5m")
    vacc  = g("vacc") if g("vacc") is not None else 0.0
    b5 = g("b5") or g("txns_5m_buys") or 0
    s5 = g("s5") or g("txns_5m_sells") or 0
    # vol acceleration: actual 5m vol vs uniform expectation from 1h vol
    vol_accel = v5 / ((v1h / 12.0) + 1e-9) if v1h else 0.0
    # momentum persistence: do 5m and 1h agree in direction
    mom_persist = 1.0 if (m5 > 0 and m1h > 0) else (-1.0 if (m5 < 0 and m1h < 0) else 0.0)
    # CP1 new variables: turnover (vol vs pool depth) + price acceleration
    turnover_5m = v5 / (liq + 1e-9)
    turnover_1h = v1h / (liq + 1e-9)
    px_accel = m5 * 12.0 - m1h   # 5m pace (->hourly) minus actual hourly = price accelerating?
    return {
        "liq": liq, "mc": mc, "v5": v5, "v1h": v1h, "m5": m5, "m1h": m1h,
        "bs": bs, "lqv": lqv, "liq_mc": liq_mc, "agg": agg, "vacc": vacc,
        "b5": b5, "s5": s5, "txn5": b5 + s5, "whale": g("whale") or 0,
        "vol_accel": vol_accel, "mom_persist": mom_persist,
        "turnover_5m": turnover_5m, "turnover_1h": turnover_1h, "px_accel": px_accel,
        "score": g("score"), "conf": g("conf"),
        "regime": regime_of(agg),
        # live-only extras (None in history)
        "v6h": g("volume_6h"), "v24h": g("volume_24h"),
        "pair_age_h": g("pair_age_hours"),
        "has_socials": g("has_socials"), "has_website": g("has_website"),
    }

# ----------------------------------------------------------------------------- data load
_CACHE = None
def load_obs(require_minmax=False, limit=None):
    """Load forward_obs into a list of dicts: {ts, feat, fwd, fwd_min, fwd_max}."""
    global _CACHE
    if _CACHE is None:
        rows = []
        for line in open(FWD_OBS):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("fwd") is None: continue
            rows.append({
                "ts": r.get("ts", 0.0),
                "mint": r.get("mint"),
                "feat": featurize(r),
                "fwd": r["fwd"],
                "fwd_min": r.get("fwd_min"),
                "fwd_max": r.get("fwd_max"),
            })
        rows.sort(key=lambda x: x["ts"])
        _CACHE = rows
    out = _CACHE
    if require_minmax:
        out = [r for r in out if r["fwd_min"] is not None and r["fwd_max"] is not None]
    if limit:
        out = out[-limit:]
    return out

# ----------------------------------------------------------------------------- admission
def admit(feat, a):
    """Evaluate an admission predicate dict against a feature dict. All present keys ANDed."""
    if not a: return True
    r = feat
    def ge(key, fk):
        v = a.get(key)
        return v is None or (r.get(fk) is not None and r[fk] >= v)
    def le(key, fk):
        v = a.get(key)
        return v is None or (r.get(fk) is not None and r[fk] <= v)
    if "regimes" in a and a["regimes"] and r["regime"] not in a["regimes"]: return False
    if "skip_regimes" in a and r["regime"] in a["skip_regimes"]: return False
    checks = [
        ge("min_liq","liq"), le("max_liq","liq"),
        ge("min_liq_mc","liq_mc"), le("max_liq_mc","liq_mc"),
        ge("min_bs","bs"), le("max_bs","bs"),
        ge("min_lqv","lqv"), le("max_lqv","lqv"),
        ge("min_v5","v5"), ge("min_v1h","v1h"),
        ge("min_m5","m5"), le("max_m5","m5"),
        ge("min_m1h","m1h"), le("max_m1h","m1h"),
        ge("min_vol_accel","vol_accel"), le("max_vol_accel","vol_accel"),
        ge("min_turnover5","turnover_5m"), ge("min_turnover1h","turnover_1h"),
        ge("min_px_accel","px_accel"), le("max_px_accel","px_accel"),
        ge("min_vacc","vacc"),
        ge("min_txn5","txn5"),
        ge("min_score","score"), ge("min_conf","conf"),
        ge("min_mc","mc"), le("max_mc","mc"),
    ]
    if a.get("require_mom_persist") and r["mom_persist"] <= 0: return False
    if a.get("require_whale") and not r["whale"]: return False
    return all(checks)

# ----------------------------------------------------------------------------- confidence
def confidence(feat, c):
    """Return a P(win)-ish scalar in [0,1] used to scale sizing."""
    if not c: return 0.5
    t = c.get("type", "constant")
    p = c.get("params", {})
    if t == "constant":
        return float(p.get("value", 0.5))
    if t == "score":
        s = feat.get("score")
        return min(1.0, max(0.0, (s or 0) / 100.0))
    if t == "conf":
        return float(feat.get("conf") or 0.5)
    if t == "logistic":
        # stored standardized logistic: params = {bias, weights:{feat:w}, mean:{}, std:{}}
        z = p.get("bias", 0.0)
        mean = p.get("mean", {}); std = p.get("std", {}); w = p.get("weights", {})
        for k, wk in w.items():
            x = feat.get(k)
            if x is None: continue
            xn = (x - mean.get(k, 0.0)) / (std.get(k, 1.0) or 1.0)
            z += wk * xn
        try: return 1.0 / (1.0 + math.exp(-z))
        except OverflowError: return 0.0 if z < 0 else 1.0
    return 0.5

# ----------------------------------------------------------------------------- sizing
# regime EV multipliers from the brain's 5-band by_regime finding (S85)
_REGIME_MULT = {"euphoria": 1.0, "aggressive": 0.7, "sniper": 1.0,
                "normal": 0.3, "dead": 0.1, "unknown": 0.3}

def size_frac(feat, conf, s):
    """Fraction of CURRENT equity to deploy on this trade."""
    if not s: return 0.02
    t = s.get("type", "flat")
    cap = s.get("cap", 0.20)
    base = s.get("base_frac", 0.02)
    if t == "flat":
        f = base
    elif t == "conf":              # C²-like: scale by confidence
        f = base * (conf / 0.5)
    elif t == "conf_sq":           # confidence squared (convex in conviction)
        f = base * (conf / 0.5) ** 2
    elif t == "tiered":            # discrete confidence buckets
        tiers = s.get("tiers", [[0.55, 0.02], [0.65, 0.05], [0.75, 0.10]])
        f = 0.0
        for thr, fr in tiers:
            if conf >= thr: f = fr
    elif t == "kelly":             # fractional Kelly from conf (win prob) + payoff b
        b = s.get("payoff", 1.5)
        kfrac = s.get("kelly_frac", 0.25)
        edge = conf - (1 - conf) / b      # (bp - q)/b form simplified
        f = max(0.0, edge) * kfrac
    elif t == "regime_kelly":      # Kelly scaled by regime EV multiplier
        b = s.get("payoff", 1.5)
        kfrac = s.get("kelly_frac", 0.25)
        edge = conf - (1 - conf) / b
        f = max(0.0, edge) * kfrac * _REGIME_MULT.get(feat["regime"], 0.3)
    elif t == "voltarget":         # inverse-volatility (needs fwd spread proxy via lqv/liq)
        # proxy vol with inverse liq depth: deeper pool -> larger size
        depth = feat.get("liq") or 0.0
        scale = min(1.0, depth / s.get("liq_ref", 50000.0))
        f = base * (0.5 + scale) * (conf / 0.5)
    else:
        f = base
    return max(0.0, min(cap, f))

# ----------------------------------------------------------------------------- exit
def realized_return(row, e, friction_pct=0.0):
    """Return realized % given exit policy + (fwd, fwd_min, fwd_max).
    Path between min/max/final is unknown -> tp/sl uses the CONSERVATIVE assumption
    (if both tp and sl would trigger, assume SL hit first)."""
    fwd = row["fwd"]; fmin = row["fwd_min"]; fmax = row["fwd_max"]
    if not e or e.get("type", "hold") == "hold":
        gross = fwd
    elif e["type"] == "tpsl":
        if fmin is None or fmax is None:
            gross = fwd  # no path data -> fall back to final
        else:
            tp = e.get("tp", 999); sl = e.get("sl", 999)
            hit_tp = fmax >= tp
            hit_sl = fmin <= -sl
            if hit_tp and hit_sl:
                gross = -sl                      # conservative: SL first
            elif hit_tp:
                gross = tp
            elif hit_sl:
                gross = -sl
            else:
                gross = fwd
    elif e["type"] == "trail":
        # lock-gain approximation: once peak >= activation, keep retention*peak
        if fmax is None:
            gross = fwd
        else:
            act = e.get("act", 5.0); ret = e.get("retention", 0.6)
            sl = e.get("sl", 999)
            if fmin is not None and fmin <= -sl and (fmax < act):
                gross = -sl
            elif fmax >= act:
                gross = max(fwd, fmax * ret)
            else:
                gross = fwd
    else:
        gross = fwd
    return gross - friction_pct

# ----------------------------------------------------------------------------- EV metrics
def _se(xs):
    if len(xs) < 2: return 0.0
    return statistics.pstdev(xs) / math.sqrt(len(xs))

def ev_metrics(rows, strat):
    """Sizing-independent admission edge: stats over the admitted subset's realized return."""
    a = strat.get("admit"); e = strat.get("exit"); fr = strat.get("friction_pct", 0.0)
    rets = []; rets_h1 = []; rets_h2 = []; by_reg = defaultdict(list)
    n_total = len(rows); mid = n_total // 2
    for i, row in enumerate(rows):
        if not admit(row["feat"], a): continue
        ret = realized_return(row, e, fr)
        rets.append(ret)
        (rets_h1 if i < mid else rets_h2).append(ret)
        by_reg[row["feat"]["regime"]].append(ret)
    n = len(rets)
    if n == 0:
        return {"n": 0}
    mean = statistics.mean(rets)
    ev_lo = mean - 1.64 * _se(rets)
    out = {
        "n": n, "rate": round(n / n_total, 4),
        "mean": round(mean, 3), "ev_lo": round(ev_lo, 3),
        "median": round(statistics.median(rets), 3),
        "win": round(sum(1 for x in rets if x > 0) / n, 3),
        "h1_mean": round(statistics.mean(rets_h1), 3) if rets_h1 else None,
        "h2_mean": round(statistics.mean(rets_h2), 3) if rets_h2 else None,
        "stable": bool(rets_h1 and rets_h2 and statistics.mean(rets_h1) > 0 and statistics.mean(rets_h2) > 0),
        "by_regime": {k: {"n": len(v), "mean": round(statistics.mean(v), 2)} for k, v in by_reg.items() if len(v) >= 5},
    }
    return out

# ----------------------------------------------------------------------------- portfolio sim
def simulate(rows, strat, start_sol=1.0, max_concurrent=8, per_trade_cap=None, seed=0):
    """Cash-constrained portfolio sim: tests admission x confidence x sizing x exit together.
    Positions open at row ts, settle at ts+HORIZON. Returns equity curve + stats."""
    a = strat.get("admit"); c = strat.get("confidence"); s = strat.get("sizing")
    e = strat.get("exit"); fr = strat.get("friction_pct", 0.0)
    cap = per_trade_cap if per_trade_cap is not None else (s or {}).get("cap", 0.20)
    equity = start_sol
    committed = 0.0
    open_heap = []   # (close_ts, cost, ret_pct)
    n_trades = 0; wins = 0
    peak = equity; max_dd = 0.0
    curve = []  # (ts, equity)
    pnl_sum = 0.0
    for row in rows:
        ts = row["ts"]
        # settle matured positions
        while open_heap and open_heap[0][0] <= ts:
            _, cost, ret = heapq.heappop(open_heap)
            pnl = cost * (ret / 100.0)
            equity += pnl; committed -= cost; pnl_sum += pnl
            if pnl > 0: wins += 1
            peak = max(peak, equity)
            if peak > 0: max_dd = max(max_dd, (peak - equity) / peak)
        # consider entry
        if not admit(row["feat"], a): continue
        if len(open_heap) >= max_concurrent: continue
        conf = confidence(row["feat"], c)
        f = size_frac(row["feat"], conf, s)
        if f <= 0: continue
        free = equity - committed
        cost = min(f * equity, free, cap * equity)
        if cost <= 1e-6: continue
        ret = realized_return(row, e, fr)
        committed += cost
        heapq.heappush(open_heap, (ts + HORIZON_S, cost, ret))
        n_trades += 1
        curve.append((ts, equity))
    # settle remainder
    while open_heap:
        _, cost, ret = heapq.heappop(open_heap)
        pnl = cost * (ret / 100.0)
        equity += pnl; committed -= cost; pnl_sum += pnl
        if pnl > 0: wins += 1
        peak = max(peak, equity)
        if peak > 0: max_dd = max(max_dd, (peak - equity) / peak)
    return {
        "final_sol": round(equity, 4),
        "return_pct": round((equity / start_sol - 1) * 100, 2),
        "n_trades": n_trades,
        "win": round(wins / n_trades, 3) if n_trades else 0.0,
        "max_dd": round(max_dd, 3),
        "sol_per_trade": round(pnl_sum / n_trades, 5) if n_trades else 0.0,
    }

# ----------------------------------------------------------------------------- logistic fit
def fit_logistic(rows, feats, label_thresh=2.0, iters=400, lr=0.3, l2=1e-3):
    """Pure-python logistic regression: P(fwd > label_thresh). Returns a confidence config."""
    # standardize
    cols = {k: [] for k in feats}
    ys = []
    samp = []
    for row in rows:
        f = row["feat"]
        if any(f.get(k) is None for k in feats): continue
        samp.append(f); ys.append(1.0 if row["fwd"] > label_thresh else 0.0)
        for k in feats: cols[k].append(f[k])
    if len(samp) < 50:
        return None, 0
    mean = {k: statistics.mean(v) for k, v in cols.items()}
    std  = {k: (statistics.pstdev(v) or 1.0) for k, v in cols.items()}
    X = [[ (f[k]-mean[k])/std[k] for k in feats ] for f in samp]
    w = [0.0]*len(feats); b = 0.0
    n = len(X)
    for _ in range(iters):
        gw = [0.0]*len(feats); gb = 0.0
        for xi, yi in zip(X, ys):
            z = b + sum(wj*xj for wj, xj in zip(w, xi))
            z = max(-30, min(30, z))
            p = 1.0/(1.0+math.exp(-z))
            d = p - yi
            for j in range(len(w)): gw[j] += d*xi[j]
            gb += d
        for j in range(len(w)):
            w[j] -= lr*(gw[j]/n + l2*w[j])
        b -= lr*(gb/n)
    cfg = {"type": "logistic", "params": {
        "bias": b, "weights": {k: w[j] for j, k in enumerate(feats)},
        "mean": mean, "std": std}}
    return cfg, len(samp)

# ----------------------------------------------------------------------------- CLI helpers
def load_configs(pattern="*.json"):
    out = []
    for fp in sorted(glob.glob(os.path.join(CONFIGS, pattern))):
        try: out.append(json.load(open(fp)))
        except Exception as ex: print("  bad config", fp, ex)
    return out

def run_tournament(strats, rows=None, start_sol=1.0, tag="cp"):
    if rows is None: rows = load_obs()
    res = []
    for st in strats:
        ev = ev_metrics(rows, st)
        sim = simulate(rows, st, start_sol=start_sol)
        res.append({"name": st.get("name"), "ev": ev, "sim": sim, "strat": st})
    res.sort(key=lambda r: r["sim"]["final_sol"], reverse=True)
    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, f"{tag}_{int(time.time())}.json")
    json.dump({"ts": time.time(), "n_obs": len(rows), "results": res}, open(out, "w"), indent=1)
    return res, out

def print_table(res):
    print(f"{'STRATEGY':28} {'finalSOL':>8} {'ret%':>7} {'trades':>6} {'win':>5} {'maxDD':>6} | {'EVn':>5} {'EVmean':>7} {'ev_lo':>6} {'stbl':>4}")
    print("-"*104)
    for r in res:
        ev = r["ev"]; sim = r["sim"]
        print(f"{(r['name'] or '?')[:28]:28} {sim['final_sol']:>8.3f} {sim['return_pct']:>7.1f} "
              f"{sim['n_trades']:>6} {sim['win']:>5.2f} {sim['max_dd']:>6.2f} | "
              f"{ev.get('n',0):>5} {ev.get('mean',0):>7.2f} {ev.get('ev_lo',0):>6.2f} "
              f"{'Y' if ev.get('stable') else '·':>4}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="cp")
    ap.add_argument("--configs", default="*.json")
    ap.add_argument("--minmax", action="store_true", help="only rows with fwd_min/max (exit tests)")
    args = ap.parse_args()
    rows = load_obs(require_minmax=args.minmax)
    print(f"loaded {len(rows)} obs (minmax={args.minmax})")
    strats = load_configs(args.configs)
    print(f"loaded {len(strats)} strategies")
    if strats:
        res, out = run_tournament(strats, rows, tag=args.tag)
        print_table(res)
        print("\nwrote", out)
