#!/usr/bin/env python3
"""
goldilocks.py — derive optimal TP/SL/filter thresholds from trades.db
Usage:
    python3 goldilocks.py              # all 3 bots combined
    python3 goldilocks.py --bot 1      # bot 1 only
    python3 goldilocks.py --bot 1 --verbose
    python3 goldilocks.py --emit-override   # write thresholds_override.json
"""
import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path
from statistics import mean, median, stdev
from typing import Optional

BASE = Path(__file__).parent

# ── Tier targets (current config values) ─────────────────────────────────────
TIER_DEFAULTS = {
    "quick":    {"tp": 5.0,  "sl": -3.0, "tp1": None, "tp1_frac": 0.0,  "hold_m": 45},
    "gem":      {"tp": 15.0, "sl": -4.0, "tp1": 7.0,  "tp1_frac": 0.35, "hold_m": 180},
    "highconv": {"tp": 25.0, "sl": -5.0, "tp1": 10.0, "tp1_frac": 0.30, "hold_m": 300},
    "wild":     {"tp": 6.0,  "sl": -2.8, "tp1": None, "tp1_frac": 0.0,  "hold_m": 180},
    "stoic":    {"tp": 10.0, "sl": -3.5, "tp1": None, "tp1_frac": 0.0,  "hold_m": 360},
}
# Vol gate baselines — read live from config_manager so goldilocks compares against
# whatever was last written to thresholds_override.json, not a frozen source value.
def _gem_vol5m_gate() -> int:
    from config_manager import cfg
    return cfg("vol_gates.gem_min_volume_5m", 3_500)

def _std_vol5m_gate() -> int:
    from config_manager import cfg
    return cfg("vol_gates.std_min_volume_5m", 5_000)

# ── Regex to parse signal rationale strings ───────────────────────────────────
_RAT = re.compile(
    r"(?P<mom>[\d.]+)% mom"
    r".*?1h=(?P<h1>[+-]?[\d.]+)%"
    r".*?liq \$(?P<liq>[\d.]+)k"
    r".*?b/s=(?P<bs>[\d.]+)"
    r".*?conf (?P<conf>[\d.]+)"
    r".*?wr (?P<wr>[\d.]+)%"
    r" \((?P<ntrades>\d+)"
)

def _parse_rationale(s: str) -> dict:
    m = _RAT.search(s or "")
    if not m:
        return {}
    return {
        "mom":     float(m.group("mom")),
        "h1":      float(m.group("h1")),
        "liq_k":   float(m.group("liq")),
        "bs":      float(m.group("bs")),
        "conf":    float(m.group("conf")),
        "wr_pct":  float(m.group("wr")),
        "ntrades": int(m.group("ntrades")),
    }


# ── Load and pair open/close events from a db ────────────────────────────────
def _load_db(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, ts, event, data FROM trades ORDER BY id"
    ).fetchall()
    conn.close()

    opens: dict[str, list[dict]] = {}
    pending_signals: dict[str, dict] = {}  # latest signal per mint, consumed by next open
    trades: list[dict] = []

    for row_id, ts, event, raw in rows:
        d = json.loads(raw)
        mint = d.get("mint", "")

        if event == "open":
            entry: dict = {
                "open_id":  row_id,
                "open_ts":  ts,
                "size_sol": d.get("size_sol", 0.0),
                "size_usd": d.get("size_usd", 0.0),
                "price":    d.get("price", 0.0),
                "tier":     d.get("tier"),       # present after session 28
                "sig_metrics": pending_signals.pop(mint, {}),
            }
            opens.setdefault(mint, []).append(entry)

        elif event == "signal":
            # Signals arrive before their open — stash and consume on next open
            sig = _parse_rationale(d.get("rationale", ""))
            sig["vol_5m"]   = d.get("vol_5m")    # present after the audit fix
            sig["sig_ts"]   = ts
            pending_signals[mint] = sig

        elif event == "close":
            pending = opens.get(mint, [])
            if not pending:
                continue
            op = pending.pop(0)  # FIFO: first open matched to first close

            pnl_usd = d.get("pnl", 0.0)
            pnl_sol = d.get("pnl_sol")
            size_sol = op["size_sol"]
            size_usd = op["size_usd"]

            # Compute % PnL — prefer sol-native path (more accurate)
            if pnl_sol is not None and size_sol and size_sol > 0:
                pnl_pct = pnl_sol / size_sol * 100
            elif size_usd and size_usd > 0:
                pnl_pct = pnl_usd / size_usd * 100
            else:
                pnl_pct = None

            # Tier: close record wins (session 26+), fallback to open record
            tier = d.get("tier") or op.get("tier")

            trades.append({
                "mint":       mint,
                "open_ts":    op["open_ts"],
                "close_ts":   ts,
                "pnl_pct":    pnl_pct,
                "pnl_sol":    pnl_sol,
                "pnl_usd":    pnl_usd,
                "size_sol":   size_sol,
                "tier":       tier,
                "entry_price": op["price"],
                "sig":        op.get("sig_metrics", {}),
            })

    return trades


def _load_all(bots: list[int]) -> list[dict]:
    all_trades: list[dict] = []
    for b in bots:
        path = BASE / f"bots/bot{b}/trades.db"
        t = _load_db(path)
        for tr in t:
            tr["bot"] = b
        all_trades.extend(t)
    return all_trades


MOM_GLITCH_MAX = 200.0  # mirrors config.py data glitch guard — exclude from analysis

def _clean_sig(sig: dict) -> dict:
    """Return sig with momentum capped at glitch threshold (mirrors entry guard)."""
    if sig.get("mom", 0) > MOM_GLITCH_MAX:
        return {k: v for k, v in sig.items() if k != "mom"}
    return sig


# ── Analysis helpers ──────────────────────────────────────────────────────────
def _ev(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return mean(t["pnl_pct"] for t in trades if t["pnl_pct"] is not None)

def _hr(trades: list[dict]) -> float:
    valid = [t for t in trades if t["pnl_pct"] is not None]
    if not valid:
        return 0.0
    return sum(1 for t in valid if t["pnl_pct"] > 0) / len(valid)

def _fmt(pct: float) -> str:
    return f"{pct:+.2f}%"

def _div(label: str = "", w: int = 70) -> None:
    if label:
        pad = (w - len(label) - 2) // 2
        print(f"\n{'─' * pad} {label} {'─' * (w - pad - len(label) - 2)}")
    else:
        print("─" * w)


# ── Section 1: Fleet summary ──────────────────────────────────────────────────
def section_summary(trades: list[dict]) -> None:
    _div("FLEET SUMMARY")
    valid = [t for t in trades if t["pnl_pct"] is not None]
    wins  = [t for t in valid if t["pnl_pct"] > 0]
    losses= [t for t in valid if t["pnl_pct"] <= 0]

    if not valid:
        print("  No completed trades found.")
        return

    wr = len(wins) / len(valid) * 100
    avg_w = mean(t["pnl_pct"] for t in wins)   if wins   else 0
    avg_l = mean(t["pnl_pct"] for t in losses) if losses else 0
    ev    = mean(t["pnl_pct"] for t in valid)
    rr    = abs(avg_w / avg_l) if avg_l != 0 else float("inf")

    print(f"  Trades : {len(valid)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate: {wr:.1f}%")
    print(f"  Avg win : {_fmt(avg_w)}   Avg loss: {_fmt(avg_l)}")
    print(f"  R:R     : {rr:.2f}x")
    print(f"  Edge (EV): {_fmt(ev)} per trade")

    if valid:
        pnls = sorted(t["pnl_pct"] for t in valid)
        print(f"  P10/P50/P90: {_fmt(pnls[len(pnls)//10])} / "
              f"{_fmt(pnls[len(pnls)//2])} / {_fmt(pnls[-len(pnls)//10])}")

    total_sol = sum(t["pnl_sol"] for t in valid if t["pnl_sol"] is not None)
    print(f"  Total SOL PnL (pnl_sol field): ◎{total_sol:+.4f}")


# ── Section 2: GEM tier win rate (Next Steps #5) ─────────────────────────────
def section_gem_wr(trades: list[dict]) -> Optional[float]:
    _div("GEM TIER WIN RATE (Next Steps #5)")

    tier_map: dict[str, list[dict]] = {}
    untiered = 0
    for t in trades:
        if t["pnl_pct"] is None:
            continue
        tier = (t.get("tier") or "").lower().strip()
        if tier:
            tier_map.setdefault(tier, []).append(t)
        else:
            untiered += 1

    if not tier_map:
        print(f"  No tier-tagged trades yet ({untiered} untiered). Tier logging added in")
        print("  session 28 — run again after 20+ INSANE trades.")
        return None

    gem_wr: Optional[float] = None
    for tier in ["quick", "gem", "highconv", "wild", "stoic"]:
        bucket = tier_map.get(tier, [])
        if not bucket:
            continue
        wr = _hr(bucket) * 100
        ev = _ev(bucket)
        avg_w = mean(t["pnl_pct"] for t in bucket if t["pnl_pct"] > 0) if any(t["pnl_pct"] > 0 for t in bucket) else 0
        avg_l = mean(t["pnl_pct"] for t in bucket if t["pnl_pct"] <= 0) if any(t["pnl_pct"] <= 0 for t in bucket) else 0
        flag = ""
        if tier == "gem" and wr < 45 and len(bucket) >= 10:
            flag = "  ⚠ BELOW 45% THRESHOLD — see vol_5m analysis below"
        print(f"  {tier.upper():10s}  n={len(bucket):3d}  WR={wr:.1f}%  "
              f"avg_win={_fmt(avg_w)}  avg_loss={_fmt(avg_l)}  EV={_fmt(ev)}{flag}")
        if tier == "gem":
            gem_wr = wr

    if untiered:
        print(f"  ({untiered} trades without tier field — pre-session-28 data)")

    return gem_wr


# ── Section 3: Realized TP/SL optimizer ──────────────────────────────────────
def section_tp_sl_optimizer(trades: list[dict]) -> None:
    _div("REALIZED TP/SL OPTIMIZER")
    print("  Sweeps candidate TP/SL pairs and picks the combo that maximizes expected")
    print("  value on your actual exit PnL distribution.\n")

    def _simulate_ev(bucket: list[dict], tp: float, sl: float) -> tuple[float, float, float]:
        hits = []
        for t in bucket:
            p = t["pnl_pct"]
            if p is None:
                continue
            # Treat the actual exit as a sample of where price went.
            # If exit pnl >= tp  → would have hit TP (capped at tp)
            # If exit pnl <= sl  → would have hit SL
            # Else               → held to time-limit, exit at actual pnl
            if p >= tp:
                hits.append(tp)
            elif p <= sl:
                hits.append(sl)
            else:
                hits.append(p)
        if not hits:
            return 0.0, 0.0, 0.0
        wins = [h for h in hits if h > 0]
        wr = len(wins) / len(hits)
        ev = mean(hits)
        return wr, ev, len(hits)

    # Group by tier (or mode for untiered)
    for tier, cfg in TIER_DEFAULTS.items():
        bucket = [t for t in trades if (t.get("tier") or "").lower() == tier
                  and t["pnl_pct"] is not None]
        # Fallback for untiered: wild/stoic by size of position (heuristic)
        if not bucket and tier in ("wild", "stoic"):
            bucket = [t for t in trades if not t.get("tier")
                      and t["pnl_pct"] is not None]

        if len(bucket) < 5:
            print(f"  {tier.upper():10s}  — fewer than 5 trades, skipping optimizer")
            continue

        cur_wr, cur_ev, _ = _simulate_ev(bucket, cfg["tp"], cfg["sl"])

        best_ev, best_tp, best_sl, best_wr = cur_ev, cfg["tp"], cfg["sl"], cur_wr
        tp_range = [t * 0.5 for t in range(int(cfg["tp"]*2 - 4), int(cfg["tp"]*2 + 10))]  # ±5% in 0.5 steps
        sl_range = [s * 0.5 for s in range(int(cfg["sl"]*2 - 4), int(cfg["sl"]*2 + 4))]   # ±2% in 0.5 steps

        for tp in tp_range:
            for sl in sl_range:
                if tp <= 0 or sl >= 0:
                    continue
                wr, ev, _ = _simulate_ev(bucket, tp, sl)
                if ev > best_ev:
                    best_ev, best_tp, best_sl, best_wr = ev, tp, sl, wr

        delta_ev = best_ev - cur_ev
        changed = best_tp != cfg["tp"] or best_sl != cfg["sl"]
        marker = "  ★ CHANGE" if changed and abs(delta_ev) > 0.1 else ""
        print(f"  {tier.upper():10s}  n={len(bucket):3d}")
        print(f"    Current  TP={cfg['tp']:+.1f}% SL={cfg['sl']:+.1f}%  "
              f"→ WR={cur_wr*100:.1f}%  EV={_fmt(cur_ev)}")
        print(f"    Optimal  TP={best_tp:+.1f}% SL={best_sl:+.1f}%  "
              f"→ WR={best_wr*100:.1f}%  EV={_fmt(best_ev)}  ΔEV={_fmt(delta_ev)}{marker}")
        print()


# ── Section 4: Almost-winner analysis ─────────────────────────────────────────
def section_almost_winners(trades: list[dict], gem_wr: Optional[float]) -> None:
    _div("ALMOST-WINNER ANALYSIS")

    gem_trades = [t for t in trades if (t.get("tier") or "").lower() == "gem"
                  and t["pnl_pct"] is not None]
    all_with_pnl = [t for t in trades if t["pnl_pct"] is not None]

    # For untiered data use entire fleet (best proxy when tier not logged)
    analysis_bucket = gem_trades if len(gem_trades) >= 5 else all_with_pnl
    label = "GEM tier" if gem_trades else "ALL tiers (GEM tier needs 5+ tagged trades)"
    tp1_gate = 7.0 if gem_trades else 6.0  # GEM TP1 = +7%, WILD TP = +6%

    print(f"  Analysing {len(analysis_bucket)} trades [{label}]")
    print(f"  TP1 reference gate: +{tp1_gate}%\n")

    winners    = [t for t in analysis_bucket if t["pnl_pct"] > 0]
    almost     = [t for t in analysis_bucket if 0 < t["pnl_pct"] < tp1_gate]   # won but below TP1
    real_wins  = [t for t in analysis_bucket if t["pnl_pct"] >= tp1_gate]        # hit or passed TP1
    losers     = [t for t in analysis_bucket if t["pnl_pct"] <= 0]

    print(f"  Category breakdown:")
    print(f"    Real winners (≥TP1 gate +{tp1_gate}%): {len(real_wins)}")
    print(f"    Almost winners (0% to +{tp1_gate}%):   {len(almost)}")
    print(f"    Losers (≤0%):                          {len(losers)}")

    if almost:
        avg_aw = mean(t["pnl_pct"] for t in almost)
        print(f"\n  Almost-winner avg exit: {_fmt(avg_aw)}")

        # Extract signal metrics for each group (apply glitch filter to momentum)
        def _grp_metrics(group: list[dict], name: str) -> None:
            with_sig = [t for t in group if t["sig"]]
            if not with_sig:
                print(f"\n  {name}: no signal metrics (pre-audit-fix trades)")
                return
            sigs  = [_clean_sig(t["sig"]) for t in with_sig]
            moms  = [s["mom"]   for s in sigs if "mom"   in s]
            liqs  = [s["liq_k"] for s in sigs if "liq_k" in s]
            bss   = [s["bs"]    for s in sigs if "bs"    in s]
            confs = [s["conf"]  for s in sigs if "conf"  in s]
            vol5  = [s["vol_5m"] for s in sigs if s.get("vol_5m") is not None]
            glitch_ct = len(with_sig) - len(moms)
            glitch_note = f"  ({glitch_ct} data-glitch signals excluded)" if glitch_ct else ""
            print(f"\n  {name} (n={len(with_sig)}){glitch_note}:")
            if moms:  print(f"    momentum:  avg={mean(moms):.1f}%  median={median(moms):.1f}%")
            if liqs:  print(f"    liq_k$:    avg={mean(liqs):.0f}k  median={median(liqs):.0f}k")
            if bss:   print(f"    buy/sell:  avg={mean(bss):.2f}  median={median(bss):.2f}")
            if confs: print(f"    conf:      avg={mean(confs):.2f}  median={median(confs):.2f}")
            if vol5:
                print(f"    vol_5m$:   avg={mean(vol5):,.0f}  median={median(vol5):,.0f}")
            else:
                print(f"    vol_5m$:   ⚠ not in DB yet — see audit fix below")

        _grp_metrics(almost,    "Almost winners (0% < pnl < TP1)")
        _grp_metrics(real_wins, "Real winners   (pnl ≥ TP1)")
        _grp_metrics(losers,    "Losers         (pnl ≤ 0)")

    # vol_5m gate delta — only meaningful once vol_5m is logged
    vol5_almost = [t["sig"]["vol_5m"] for t in almost
                   if t["sig"].get("vol_5m") is not None]
    vol5_wins   = [t["sig"]["vol_5m"] for t in real_wins
                   if t["sig"].get("vol_5m") is not None]

    _div("vol_5m GATE ANALYSIS")
    _gem_gate = _gem_vol5m_gate()   # live gate from config_manager (was undefined _gem_gate)
    if vol5_almost and vol5_wins:
        avg_almost = mean(vol5_almost)
        avg_wins   = mean(vol5_wins)
        delta      = avg_wins - avg_almost
        suggested  = round((_gem_gate + avg_almost) / 2 / 500) * 500  # midpoint, rounded $500

        print(f"  Almost-winner avg vol_5m: ${avg_almost:,.0f}")
        print(f"  Real-winner   avg vol_5m: ${avg_wins:,.0f}")
        print(f"  Delta: ${delta:,.0f}")
        print(f"\n  Current GEM_MIN_VOLUME_5M gate: ${_gem_gate:,}")
        print(f"  Suggested GEM_MIN_VOLUME_5M:    ${suggested:,}  "
              f"(midpoint between current gate and almost-winner avg)")
        if suggested > _gem_gate:
            print(f"\n  config.py change:")
            print(f"    GEM_MIN_VOLUME_5M = {suggested:,}  # was {_gem_gate:,}")
    else:
        print("  vol_5m not yet logged in trades.db.")
        print("  Apply the audit fix below, run for 20+ trades, then re-run this script.")
        print(f"\n  The analysis will then compute:")
        print(f"    avg vol_5m of almost-winners vs ${_gem_gate:,} current gate")
        print(f"    → suggested gate = midpoint (avoids filtering out valid tokens)")


# ── Section 5: Entry metric thresholds from winners vs losers ─────────────────
def section_entry_thresholds(trades: list[dict]) -> None:
    _div("ENTRY METRIC THRESHOLDS")
    print("  Compares signal metrics of winning vs losing trades to suggest filter tightening.\n")

    all_with_sig = [t for t in trades if t["sig"] and t["pnl_pct"] is not None]
    if not all_with_sig:
        print("  No signal-paired trades found (all trades pre-date audit fix).")
        return

    wins   = [t for t in all_with_sig if t["pnl_pct"] > 0]
    losses = [t for t in all_with_sig if t["pnl_pct"] <= 0]

    if not wins or not losses:
        print("  Need both wins and losses to compare.")
        return

    metrics = [
        ("momentum",  "mom",   "MOMENTUM_THRESHOLD",  0.01),
        ("liq_k$",    "liq_k", "GEM_MIN_LIQUIDITY",   1.0),
        ("buy/sell",  "bs",    "GEM_MIN_BUY_RATIO",   0.05),
        ("conf",      "conf",  "CONFIDENCE_MIN_*",    0.01),
    ]

    for label, key, config_key, precision in metrics:
        wv = [_clean_sig(t["sig"])[key] for t in wins   if key in _clean_sig(t["sig"])]
        lv = [_clean_sig(t["sig"])[key] for t in losses if key in _clean_sig(t["sig"])]
        if not wv or not lv:
            continue
        w_med = median(wv)
        l_med = median(lv)
        diff  = w_med - l_med
        sign  = "↑" if diff > 0 else "↓"
        print(f"  {label:12s}  wins_median={w_med:.2f}  losses_median={l_med:.2f}  "
              f"diff={diff:+.2f} {sign}  [{config_key}]")


# ── Section 6: Momentum distribution — optimal entry floor ───────────────────
def section_momentum_floor(trades: list[dict]) -> None:
    _div("MOMENTUM FLOOR ANALYSIS (entry gate optimization)")
    # Exclude data-glitch signals (mom > 200%) — mirrors the entry guard
    all_valid = [t for t in trades
                 if t["pnl_pct"] is not None
                 and t["sig"].get("mom") is not None
                 and t["sig"]["mom"] <= MOM_GLITCH_MAX]

    if len(all_valid) < 10:
        print("  Need 10+ signal-paired trades (glitch-filtered).")
        return

    best_wr, best_threshold, best_n = 0.0, 0.0, 0
    thresholds = sorted({round(t["sig"]["mom"] * 2) / 2 for t in all_valid})  # 0.5% buckets

    # Pre-compute optimal WR for marker
    max_wr_thr = max(
        (_hr([t for t in all_valid if t["sig"]["mom"] >= x]) * 100, x)
        for x in thresholds
        if len([t for t in all_valid if t["sig"]["mom"] >= x]) >= 3
    )[1] if thresholds else 0

    print(f"  {'Threshold':>12}  {'Trades':>7}  {'WR':>7}  {'EV':>8}")
    for thr in thresholds:
        subset = [t for t in all_valid if t["sig"]["mom"] >= thr]
        if len(subset) < 3:
            continue
        wr = _hr(subset) * 100
        ev = _ev(subset)
        marker = " ← optimal" if thr == max_wr_thr else ""
        if wr > best_wr:
            best_wr, best_threshold, best_n = wr, thr, len(subset)
        print(f"  mom ≥ {thr:5.1f}%  {len(subset):>7}  {wr:>6.1f}%  {_fmt(ev):>8}{marker}")

    print(f"\n  Best threshold (highest WR): mom ≥ {max_wr_thr:.1f}%")
    print(f"  Best threshold (highest EV): mom ≥ {best_threshold:.1f}% → WR={best_wr:.1f}% EV={_fmt(_ev([t for t in all_valid if t['sig']['mom'] >= best_threshold]))} over {best_n} trades")
    print(f"  (compare with INSANE gate: 0.3%  WILD: 0.7%  STOIC: 1.5%)")


# ── Config injection: compute + emit thresholds_override.json ────────────────

def _optimize_tier(bucket: list[dict], cfg: dict) -> tuple[float, float, float, float]:
    """Return (best_tp, best_sl, cur_ev, best_ev) for a trade bucket."""
    def _sim(tp: float, sl: float) -> float:
        hits = []
        for t in bucket:
            p = t["pnl_pct"]
            if p is None:
                continue
            hits.append(tp if p >= tp else (sl if p <= sl else p))
        return mean(hits) if hits else 0.0

    cur_ev = _sim(cfg["tp"], cfg["sl"])
    best_ev, best_tp, best_sl = cur_ev, cfg["tp"], cfg["sl"]
    tp_range = [t * 0.5 for t in range(int(cfg["tp"] * 2 - 4), int(cfg["tp"] * 2 + 10))]
    sl_range = [s * 0.5 for s in range(int(cfg["sl"] * 2 - 4), int(cfg["sl"] * 2 + 4))]
    for tp in tp_range:
        for sl in sl_range:
            if tp <= 0 or sl >= 0:
                continue
            ev = _sim(tp, sl)
            if ev > best_ev:
                best_ev, best_tp, best_sl = ev, tp, sl
    return best_tp, best_sl, cur_ev, best_ev


def compute_overrides(trades: list[dict]) -> dict:
    """Derive optimal TP/SL/momentum from realized trade data.
    Returns a dict suitable for writing to thresholds_override.json.
    Only includes tiers with ≥ 5 trades — thin samples are left at defaults.
    """
    valid = [t for t in trades if t["pnl_pct"] is not None]
    fleet_ev = mean(t["pnl_pct"] for t in valid) if valid else 0.0

    result: dict = {
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trade_count":  len(valid),
        "ev_per_trade": round(fleet_ev, 4),
        "tiers": {},
    }

    # Per-tier TP/SL optimizer
    for tier, cfg in TIER_DEFAULTS.items():
        bucket = [t for t in trades
                  if (t.get("tier") or "").lower() == tier and t["pnl_pct"] is not None]
        if not bucket and tier in ("wild", "stoic"):
            bucket = [t for t in trades if not t.get("tier") and t["pnl_pct"] is not None]
        if len(bucket) < 5:
            continue
        best_tp, best_sl, cur_ev, best_ev = _optimize_tier(bucket, cfg)
        delta_ev = best_ev - cur_ev
        result["tiers"][tier] = {
            "take_profit_pct": best_tp,
            "stop_loss_pct":   best_sl,
            "ev_current":      round(cur_ev, 4),
            "ev_optimal":      round(best_ev, 4),
            "delta_ev":        round(delta_ev, 4),
            "n_trades":        len(bucket),
        }

    # Momentum floor: best EV threshold (min 5 trades at that level)
    sig_trades = [t for t in trades
                  if t["pnl_pct"] is not None
                  and t["sig"].get("mom") is not None
                  and t["sig"]["mom"] <= MOM_GLITCH_MAX]
    if len(sig_trades) >= 10:
        thresholds = sorted({round(t["sig"]["mom"] * 2) / 2 for t in sig_trades})
        best_ev_val, best_thr = fleet_ev, 0.0
        for thr in thresholds:
            subset = [t for t in sig_trades if t["sig"]["mom"] >= thr]
            if len(subset) < 5:
                continue
            ev = mean(t["pnl_pct"] for t in subset)
            if ev > best_ev_val:
                best_ev_val, best_thr = ev, thr
        # Cap at 4.0% — a floor above this starves the bot of entries and causes a
        # self-reinforcing deadlock (no trades → no data → floor never updated).
        _FLOOR_CAP = 4.0
        if 0 < best_thr <= _FLOOR_CAP:
            result["momentum_floor_pct"] = best_thr
        elif best_thr > _FLOOR_CAP:
            result["momentum_floor_pct"] = _FLOOR_CAP
            print(f"  [Goldilocks] momentum_floor capped at {_FLOOR_CAP}% (raw best={best_thr:.1f}% would deadlock)")

    # Vol_5m gate recommendation: 25th percentile of winners' vol_5m at signal time.
    # This sets the GEM_MIN_VOLUME_5M gate so ~75% of past winners still qualify.
    # Requires ≥10 winners with vol_5m logged (audit fix active since session 30).
    # Only emits a recommendation when it would raise the current gate — never lowers.
    vol_winners = [
        t for t in trades
        if t["pnl_pct"] is not None and t["pnl_pct"] > 0
        and t["sig"].get("vol_5m") is not None
    ]
    if len(vol_winners) >= 10:
        sorted_vols = sorted(t["sig"]["vol_5m"] for t in vol_winners)
        p25_idx = max(0, int(len(sorted_vols) * 0.25) - 1)
        p25_vol = sorted_vols[p25_idx]
        recommended = int(round(p25_vol / 500) * 500)
        if recommended > _gem_vol5m_gate():
            result["vol_gates"] = {
                "gem_min_volume_5m": recommended,
                "n_winners_sampled": len(vol_winners),
            }

    return result


def emit_override_file(overrides: dict, path: Path) -> None:
    # Atomic write: bots polling the file never see a half-written JSON.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(overrides, indent=2))
    tmp.replace(path)
    print(f"[Goldilocks] Override written → {path.name}")
    print(f"  Based on {overrides['trade_count']} trades  "
          f"fleet EV={overrides['ev_per_trade']:+.2f}%")
    for tier, d in overrides.get("tiers", {}).items():
        print(f"  {tier:10s}  TP={d['take_profit_pct']:+.1f}%  SL={d['stop_loss_pct']:+.1f}%  "
              f"EV {d['ev_current']:+.3f}% → {d['ev_optimal']:+.3f}%  (n={d['n_trades']})")
    mom = overrides.get("momentum_floor_pct")
    if mom:
        print(f"  momentum_floor = {mom:.1f}%")


# ── Section 7: Pending audit fix reminder ─────────────────────────────────────
def section_audit_fix_reminder() -> None:
    _div("AUDIT FIX — add vol_5m to signal log")
    print("""  The vol_5m field is available at signal evaluation time but was never written
  to trades.db. Without it, the vol_5m gate delta analysis (Section 4) can only
  show ⚠ placeholders.

  Fix is already applied if this block appears:
    [Audit] {'event': 'signal', ..., 'vol_5m': 5432.0, ...}

  If not, apply the two-file patch below and restart:

  ── audit.py ────────────────────────────────────────────────────────────
  def log_signal(mint, momentum, price, rationale, vol_5m=None):
      record = {"event": "signal", "mint": mint, "momentum": momentum,
                "price": price, "rationale": rationale}
      if vol_5m is not None:
          record["vol_5m"] = round(vol_5m, 2)
      _write(record)

  ── main.py (line ~1495) ────────────────────────────────────────────────
  audit.log_signal(
      sig["mint"], sig["momentum_5m"], sig["price"], sig["rationale"],
      vol_5m=sig.get("volume_5m"),
  )
  ────────────────────────────────────────────────────────────────────────

  After 20+ trades re-run:  python3 goldilocks.py
""")


# ── SQL snippets ──────────────────────────────────────────────────────────────
def section_sql_snippets() -> None:
    _div("READY-TO-RUN SQL QUERIES")
    print("""  Run in any terminal:
    sqlite3 bots/bot1/trades.db

  -- GEM tier win rate (Next Steps #5):
  SELECT json_extract(data,'$.tier') AS tier,
         COUNT(*) AS trades,
         ROUND(100.0*SUM(CASE WHEN json_extract(data,'$.pnl')>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS wr,
         ROUND(AVG(json_extract(data,'$.pnl_sol')),5) AS avg_pnl_sol
  FROM trades
  WHERE event='close'
    AND json_extract(data,'$.tier') IS NOT NULL
  GROUP BY 1;

  -- vol_5m of almost-winners vs real winners (after audit fix):
  SELECT
    CASE WHEN json_extract(data,'$.pnl_sol') / (
           SELECT json_extract(o.data,'$.size_sol')
           FROM trades o WHERE o.event='open'
             AND json_extract(o.data,'$.mint')=json_extract(c.data,'$.mint')
             AND o.ts < c.ts ORDER BY o.id DESC LIMIT 1
         ) BETWEEN 0 AND 0.07 THEN 'almost_winner'
         WHEN json_extract(data,'$.pnl_sol') > 0 THEN 'real_winner'
         ELSE 'loser'
    END AS category,
    COUNT(*) AS n,
    ROUND(AVG(json_extract(
      (SELECT s.data FROM trades s WHERE s.event='signal'
       AND json_extract(s.data,'$.mint')=json_extract(c.data,'$.mint')
       AND s.ts < c.ts ORDER BY s.id DESC LIMIT 1),
      '$.vol_5m'
    )),0) AS avg_vol_5m_at_signal
  FROM trades c WHERE event='close'
  GROUP BY 1;
""")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Goldilocks threshold deriver")
    parser.add_argument("--bot", type=int, choices=[1, 2, 3],
                        help="Analyse a single bot (default: all)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print individual trades in almost-winner group")
    parser.add_argument("--emit-override", action="store_true",
                        help="Write thresholds_override.json with computed optimal values")
    args = parser.parse_args()

    bots = [args.bot] if args.bot else [1, 2, 3]
    trades = _load_all(bots)

    label = f"Bot {args.bot}" if args.bot else "All bots combined"
    print(f"\n{'═'*70}")
    print(f"  GOLDILOCKS — threshold optimizer")
    print(f"  Source: {label}  |  Trades loaded: {len(trades)}")
    print(f"{'═'*70}")

    section_summary(trades)
    gem_wr = section_gem_wr(trades)
    section_tp_sl_optimizer(trades)
    section_almost_winners(trades, gem_wr)
    section_entry_thresholds(trades)
    section_momentum_floor(trades)
    section_audit_fix_reminder()
    section_sql_snippets()
    _div()

    if args.emit_override:
        _div("EMITTING thresholds_override.json")
        overrides = compute_overrides(trades)
        override_path = BASE / "thresholds_override.json"
        emit_override_file(overrides, override_path)

    if args.verbose:
        _div("ALMOST-WINNER TRADE LIST (--verbose)")
        aw = [t for t in trades
              if t["pnl_pct"] is not None and 0 < t["pnl_pct"] < 7.0]
        for t in sorted(aw, key=lambda x: x["pnl_pct"], reverse=True):
            print(f"  bot={t['bot']} {t['mint'][:10]}..  "
                  f"pnl={_fmt(t['pnl_pct'])}  tier={t.get('tier','?'):8s}  "
                  f"mom={t['sig'].get('mom','?')}%  conf={t['sig'].get('conf','?')}  "
                  f"vol5m={t['sig'].get('vol_5m','no-data')}")


if __name__ == "__main__":
    main()
