#!/usr/bin/env python3
"""
DATBOI Execution Audit

Default:        Fleet Health Score + compact per-bot table + recommendations.
--report-full:  Everything above + per-token slippage breakdown with penalty
                correlation + full CSV trade log (sortable in any spreadsheet).

Usage:
    python3 slippage_audit.py
    python3 slippage_audit.py --report-full
    python3 slippage_audit.py --report-full > audit_2026-06-05.txt
"""

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

BOT_DBS = {
    "Bot1": "bots/bot1/trades.db",
    "Bot2": "bots/bot2/trades.db",
    "Bot3": "bots/bot3/trades.db",
}

LATENCY_BUCKETS = [
    (0,          500,  "FAST   (<500ms)"),
    (500,       1500,  "OK     (500ms-1.5s)"),
    (1500,      3000,  "SLOW   (1.5-3s)"),
    (3000, 9_999_999,  "LAGGED (>3s)"),
]

SIGNAL_MATCH_WINDOW_S = 90  # signal must precede open by at most this many seconds
_MIN_TOKEN_TRADES     = 2   # minimum Jupiter-quoted closed trades for token breakdown


# ── Data loading ───────────────────────────────────────────────────────────────

def load_events(db_path: str) -> dict:
    """Load all events from trades.db, grouped by event type."""
    events: dict = defaultdict(list)
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute("SELECT event, data FROM trades ORDER BY id"):
            event_type, data_str = row
            try:
                rec = json.loads(data_str)
                rec["_ts"] = datetime.fromisoformat(rec["ts"]).replace(tzinfo=timezone.utc)
                events[event_type].append(rec)
            except Exception:
                pass
    return events


def _sym(rec: dict, mint: str) -> str:
    """Best available symbol from an event record."""
    return rec.get("symbol") or rec.get("sym") or mint[:8]


def pair_signal_to_open(signals: list, opens: list) -> list:
    """
    For each open event, find the most recent signal for the same mint
    within SIGNAL_MATCH_WINDOW_S seconds before it.

    Drift measurement (two sources, clearly attributed):
      Jupiter  — quote_out/quote_in present in open (session 43+).
                 Measures how much worse Jupiter's fill rate was vs the DexScreener
                 market rate at signal time.  Positive = adverse (front-run or
                 thin pool consumed by order size).
      DexScr.  — fallback when Jupiter quote data is absent.  Because open.price
                 equals signal.price (both pulled from DexScreener at signal time),
                 this ALWAYS returns 0% drift.  It is NOT a real measurement.
    """
    by_mint: dict = defaultdict(list)
    for s in signals:
        by_mint[s["mint"]].append(s)

    pairs = []
    for op in opens:
        mint    = op["mint"]
        open_ts = op["_ts"]
        sig_price = op.get("price")
        if not sig_price or sig_price <= 0:
            continue

        candidates = [
            s for s in by_mint.get(mint, [])
            if 0 <= (open_ts - s["_ts"]).total_seconds() <= SIGNAL_MATCH_WINDOW_S
        ]
        if not candidates:
            continue

        sig    = max(candidates, key=lambda s: s["_ts"])
        lag_ms = (open_ts - sig["_ts"]).total_seconds() * 1000

        # ── Jupiter quote slippage (accurate) ─────────────────────────────
        # quote_out = Jupiter outAmount in RAW token units (includes decimals).
        # sig_price  = DexScreener USD price per WHOLE token.
        # size_usd   = size_sol × sol_price_at_signal = USD value of SOL spent.
        #
        # Decimal inference: size_usd / sig_price ≈ whole-tokens expected.
        # quote_out / 10^d = whole-tokens filled.
        # 10^d ≈ quote_out × sig_price / size_usd  (round to nearest integer power of 10).
        #
        # drift_pct = (expected_tokens - actual_tokens) / expected_tokens × 100
        #   + = adverse (got fewer tokens than market implied)
        #   - = favorable (over-fill — market moved in our favor between quote and fill)
        quote_out = op.get("quote_out", 0)
        quote_in  = op.get("quote_in",  0)
        size_usd  = op.get("size_usd",  0)
        has_quote = bool(quote_out and quote_in and sig_price > 0 and size_usd > 0)
        if has_quote:
            raw_d = quote_out * sig_price / size_usd
            d = int(round(math.log10(raw_d))) if raw_d > 0 else 6
            d = max(0, min(d, 12))          # clamp to sane token decimal range
            token_decimals   = 10 ** d
            expected_tokens  = size_usd / sig_price
            actual_tokens    = quote_out / token_decimals
            drift_pct        = ((expected_tokens - actual_tokens) / expected_tokens * 100
                                if expected_tokens > 0 else 0.0)
            # Sanity cap: > ±50% is almost certainly a data artifact (wrong sig_price unit etc.)
            if abs(drift_pct) > 50.0:
                has_quote    = False
                drift_pct    = 0.0
                drift_source = "DexScreener"
            else:
                drift_source = "Jupiter"
        if not has_quote:
            # Fallback — always 0% on pre-session-43 data (not a real measurement)
            open_price   = op.get("price", sig_price)
            drift_pct    = (open_price - sig_price) / sig_price * 100 if sig_price else 0.0
            drift_source = "DexScreener"

        symbol = _sym(op, mint) or _sym(sig, mint)
        pairs.append({
            "mint":         mint,
            "symbol":       symbol,
            "tier":         op.get("tier") or op.get("insane_tier") or "?",
            "signal_price": sig_price,
            "drift_pct":    drift_pct,
            "drift_source": drift_source,
            "exec_lag_ms":  lag_ms,
            "open_ts":      open_ts,
            "has_quote":    has_quote,
        })
    return pairs


def attach_close_data(pairs: list, closes: list) -> list:
    """
    Attach PnL and exec_penalty from the matching close event.

    exec_penalty is present on close events from session 53 onward.
    Older rows will have None, not 0.0, to distinguish "not recorded"
    from "no slippage penalty accumulated".
    """
    by_mint: dict = defaultdict(list)
    for c in closes:
        by_mint[c["mint"]].append(c)

    for p in pairs:
        mint, open_ts = p["mint"], p["open_ts"]
        after = [c for c in by_mint.get(mint, []) if c["_ts"] > open_ts]
        if after:
            closest       = min(after, key=lambda c: (c["_ts"] - open_ts).total_seconds())
            p["pnl_sol"]      = closest.get("pnl_sol") or closest.get("pnl", 0.0)
            p["won"]          = p["pnl_sol"] > 0
            p["exec_penalty"] = closest.get("exec_penalty")  # None = pre-session-53
        else:
            p["pnl_sol"]      = None
            p["won"]          = None
            p["exec_penalty"] = None
    return pairs


# ── Analysis ───────────────────────────────────────────────────────────────────

def bucket_label(lag_ms: float) -> str:
    for lo, hi, label in LATENCY_BUCKETS:
        if lo <= lag_ms < hi:
            return label
    return "LAGGED (>3s)"


def compute_health_score(all_pairs: list, all_latencies: list,
                         n_confirmed: int, n_failed: int) -> dict:
    """
    0-100 Fleet Health Score from three equally-important pillars:

      Drift quality  (40 pts): mean adverse drift + % trades >0.5% adverse.
        Measured only on Jupiter-quoted pairs — DexScreener pairs are excluded
        because their drift is always 0% (measurement artifact, not real data).
        100 pts at 0% mean adverse drift; 50 pts at 1%; 0 pts at 2%.

      TX latency     (30 pts): median confirmation time.
        100 pts at 0ms; 50 pts at 2.5s; 0 pts at 5s+.

      TX success     (30 pts): transaction failure rate.
        100 pts at 0% failure; 50 pts at 15%; 0 pts at 30%+.

    Pillars with insufficient data (<3/5 samples) default to 100 pts
    so a new fleet isn't penalised for missing history.
    """
    # Drift quality (40%)
    jup_pairs = [p for p in all_pairs if p.get("has_quote")]
    if jup_pairs:
        adverse      = [p["drift_pct"] for p in jup_pairs if p["drift_pct"] > 0]
        mean_adverse = mean(adverse) if adverse else 0.0
        pct_adv      = sum(1 for p in jup_pairs if p["drift_pct"] > 0.5) / len(jup_pairs) * 100
        drift_score  = max(0.0, min(100.0, 100.0 - mean_adverse * 50.0 - pct_adv * 0.5))
        drift_note   = f"mean +{mean_adverse:.2f}%  |  {pct_adv:.0f}% trades >0.5% adverse"
    else:
        drift_score = 100.0
        drift_note  = "no Jupiter quote data yet"

    # TX latency (30%)
    if len(all_latencies) >= 3:
        med_lat    = median(all_latencies)
        lat_score  = max(0.0, min(100.0, 100.0 * (1.0 - med_lat / 5000.0)))
        lat_note   = f"median {med_lat:.0f}ms"
    else:
        lat_score = 100.0
        lat_note  = "insufficient data"

    # TX success (30%)
    total_tx = n_confirmed + n_failed
    if total_tx >= 5:
        fail_rate     = n_failed / total_tx
        success_score = max(0.0, min(100.0, 100.0 * (1.0 - fail_rate / 0.30)))
        success_note  = f"{fail_rate*100:.1f}% failure rate  ({n_failed}/{total_tx} tx)"
    else:
        success_score = 100.0
        success_note  = "insufficient data"

    health = round(0.4 * drift_score + 0.3 * lat_score + 0.3 * success_score, 1)
    if   health >= 90: grade, label = "A", "Excellent"
    elif health >= 75: grade, label = "B", "Good"
    elif health >= 60: grade, label = "C", "Fair — review recommendations"
    elif health >= 45: grade, label = "D", "Poor — action required"
    else:              grade, label = "F", "Critical — execution degraded"

    return {
        "score":         health,
        "grade":         grade,
        "grade_label":   label,
        "drift_score":   round(drift_score, 1),
        "drift_note":    drift_note,
        "lat_score":     round(lat_score, 1),
        "lat_note":      lat_note,
        "success_score": round(success_score, 1),
        "success_note":  success_note,
    }


def build_token_groups(all_pairs: list) -> list:
    """
    Group paired trades by symbol.  For each symbol (min _MIN_TOKEN_TRADES
    Jupiter-quoted closed trades) compute slippage stats and the
    Penalty-to-Drift (P/D) ratio.

    P/D ratio mechanics
    ───────────────────
    exec_penalty is recorded on sell events: penalty 1.0 = 5% sell drift.
    drift_pct is measured on buy events: Jupiter fill vs DexScreener market.

    pen_pct_equiv = exec_penalty × 5%   (sell slippage expressed as a percent)
    P/D ratio     = pen_pct_equiv / drift_pct

    >2.0  → sell slippage much worse than buy slippage (thin exit liquidity)
    ~1.0  → symmetric; both legs pay similar slippage costs
    <0.5  → buy slippage is the bigger problem; penalty is under-measuring
             real entry cost — consider tighter slippage or smaller size

    Flags
    ─────
    HIGH_DRIFT      mean buy drift > 0.5% — pool consistently thin or front-run on entry
    MEV?            >50% of trades show adverse drift — sandwich bot pattern
    SELL_WORSE      P/D > 2.0 — exit conditions markedly worse than entry
    ENTRY_WORSE     P/D < 0.5 — entry is the bigger slippage problem
    """
    by_sym: dict = defaultdict(list)
    for p in all_pairs:
        if p.get("has_quote") and p.get("won") is not None:
            by_sym[p["symbol"]].append(p)

    groups = []
    for sym, trades in by_sym.items():
        if len(trades) < _MIN_TOKEN_TRADES:
            continue

        drifts = [t["drift_pct"] for t in trades]
        drifts_s = sorted(drifts)
        p90     = drifts_s[int(len(drifts_s) * 0.9)]
        pct_adv = sum(1 for d in drifts if d > 0.5) / len(drifts) * 100

        pen_vals   = [t["exec_penalty"] for t in trades if t.get("exec_penalty") is not None]
        mean_pen   = mean(pen_vals) if pen_vals else None
        mean_drift = mean(drifts)

        if mean_pen is not None and mean_pen > 0 and mean_drift > 0:
            pen_pct  = mean_pen * 5.0
            pd_ratio = pen_pct / mean_drift
        else:
            pen_pct  = None
            pd_ratio = None

        flags = []
        if mean_drift > 0.5:               flags.append("HIGH_DRIFT")
        if pct_adv > 50:                    flags.append("MEV?")
        if pd_ratio is not None:
            if pd_ratio > 2.0:              flags.append("SELL_WORSE")
            elif pd_ratio < 0.5:            flags.append("ENTRY_WORSE")

        groups.append({
            "symbol":     sym,
            "trades":     len(trades),
            "pen_n":      len(pen_vals),
            "mean_drift": round(mean_drift, 3),
            "p90_drift":  round(p90, 3),
            "pct_adv":    round(pct_adv, 1),
            "mean_pen":   round(mean_pen, 3) if mean_pen is not None else None,
            "pen_pct":    round(pen_pct, 3) if pen_pct is not None else None,
            "pd_ratio":   round(pd_ratio, 2) if pd_ratio is not None else None,
            "flags":      flags,
        })

    return sorted(groups, key=lambda g: g["mean_drift"], reverse=True)


# ── Reporting helpers ─────────────────────────────────────────────────────────

def _hdr(title: str) -> None:
    print(f"\n{'─'*64}")
    print(f"  {title}")
    print(f"{'─'*64}")


def _recommendations(all_pairs: list, all_latencies: list) -> None:
    jup_pairs = [p for p in all_pairs if p.get("has_quote")]
    drifts    = [p["drift_pct"] for p in jup_pairs] if jup_pairs else []
    issues    = 0

    if drifts:
        md       = mean(drifts)
        pct_half = sum(1 for d in drifts if d > 0.5) / len(drifts) * 100
        if md > 0.3:
            print(f"  ⚠  Mean drift {md:+.3f}% — price moving against you between signal and fill.")
            print(f"     → Reduce cycle time or tighten CONFIDENCE_MIN to fire faster on hot signals.")
            issues += 1
        if pct_half > 30:
            print(f"  ⚠  {pct_half:.0f}% of trades have >0.5% adverse drift — consistent sandwich risk.")
            print(f"     → Tighten slippage on STOIC/WILD (1-3% → 0.5-1.5%).")
            print(f"     → Raise Jito tip to 50k-100k lamports in NORMAL/HOT regimes.")
            issues += 1

    if all_latencies and mean(all_latencies) > 2000:
        print(f"  ⚠  Mean TX confirmation {mean(all_latencies):.0f}ms — Jito bundles may be under-tipped.")
        print(f"     → Raise config/base.json jito.tip_normal_lamports to 75k lamports.")
        issues += 1

    if not issues:
        print(f"  ✓  Execution looks healthy — drift and latency within acceptable bounds.")
        print(f"     Losses appear driven by market conditions, not execution quality.")


# ── Report modes ──────────────────────────────────────────────────────────────

def print_default_report(results: list) -> None:
    """
    Fleet Health Score + source attribution + compact per-bot table +
    win-rate-by-lag bucket + recommendations.
    """
    all_pairs     = [p for r in results for p in r.get("pairs", [])]
    all_latencies = [l for r in results for l in r.get("latencies", [])]
    n_confirmed   = sum(r.get("n_confirmed", 0) for r in results)
    n_failed      = sum(r.get("n_failed", 0) for r in results)

    # ── Fleet Health Score ─────────────────────────────────────────────────
    _hdr("FLEET HEALTH SCORE")
    if not all_pairs and not all_latencies:
        print("  No paired trade data yet — run after 20+ trades.")
        return

    hs = compute_health_score(all_pairs, all_latencies, n_confirmed, n_failed)
    filled = int(hs["score"] / 5)
    bar    = "█" * filled + "░" * (20 - filled)
    print(f"\n  [{bar}]  {hs['score']:.1f}/100  Grade: {hs['grade']} — {hs['grade_label']}")
    print(f"\n    Drift quality  {hs['drift_score']:>5.1f} pts  ({hs['drift_note']})")
    print(f"    TX latency     {hs['lat_score']:>5.1f} pts  ({hs['lat_note']})")
    print(f"    TX success     {hs['success_score']:>5.1f} pts  ({hs['success_note']})")

    # ── Source attribution ─────────────────────────────────────────────────
    _hdr("SOURCE ATTRIBUTION")
    jup_pairs = [p for p in all_pairs if p.get("has_quote")]
    dex_pairs = [p for p in all_pairs if not p.get("has_quote")]
    n_total   = len(all_pairs) or 1

    print(f"\n  Jupiter Quote   {len(jup_pairs):>4}/{n_total}  ({len(jup_pairs)/n_total*100:.0f}%)")
    print(f"    Accurate slippage data — captures fill-rate vs market rate at signal time.")
    if jup_pairs:
        jd   = [p["drift_pct"] for p in jup_pairs]
        jabs = sorted(abs(d) for d in jd)
        adv  = [d for d in jd if d > 0]
        print(f"    mean drift  {mean(jd):+.3f}%  |  p90 |drift|  {jabs[int(len(jabs)*0.9)]:.3f}%"
              f"  |  mean adverse  {mean(adv):+.3f}%" if adv else
              f"    mean drift  {mean(jd):+.3f}%  |  p90 |drift|  {jabs[int(len(jabs)*0.9)]:.3f}%")

    print(f"\n  DexScreener     {len(dex_pairs):>4}/{n_total}  ({len(dex_pairs)/n_total*100:.0f}%)")
    print(f"    Pre-session-43 trades.  Drift is always 0% — open.price == signal.price")
    print(f"    (same DexScreener source).  Not a real slippage measurement.")

    # ── Per-bot compact summary ────────────────────────────────────────────
    _hdr("PER-BOT SUMMARY")
    print(f"\n  {'BOT':<6}  {'PAIRS':>5}  {'DRIFT(avg)':>10}  {'LAT(med)':>8}  "
          f"{'FAIL%':>6}  {'WR':>5}  {'NET PNL':>10}")
    for r in results:
        if not r:
            continue
        pairs = r.get("pairs", [])
        lats  = r.get("latencies", [])
        nc, nf = r.get("n_confirmed", 0), r.get("n_failed", 0)
        with_out = [p for p in pairs if p.get("won") is not None]
        jup_p    = [p for p in pairs if p.get("has_quote")]

        avg_d  = f"{mean(p['drift_pct'] for p in jup_p):+.3f}%" if jup_p else "     n/a"
        med_l  = f"{median(lats):.0f}ms"   if lats           else "     n/a"
        fail_s = f"{nf/(nc+nf)*100:.1f}%"  if (nc + nf) > 0  else "   n/a"
        wr_s   = (f"{sum(p['won'] for p in with_out)/len(with_out)*100:.0f}%"
                  if with_out else "  n/a")
        net    = sum(p["pnl_sol"] for p in with_out if p.get("pnl_sol") is not None)
        print(f"  {r['name']:<6}  {len(pairs):>5}  {avg_d:>10}  {med_l:>8}  "
              f"{fail_s:>6}  {wr_s:>5}  {net:>+9.4f}◎")

    # ── Win rate by latency bucket ─────────────────────────────────────────
    with_out = [p for p in all_pairs if p.get("won") is not None]
    if with_out:
        _hdr("WIN RATE BY EXECUTION LAG")
        bucket_data: dict = defaultdict(list)
        for p in with_out:
            bucket_data[bucket_label(p["exec_lag_ms"])].append(p)
        print(f"\n  {'BUCKET':<20}  {'N':>4}  {'WR':>6}  {'AVG PNL':>10}  {'AVG DRIFT':>10}")
        for lo, hi, label in LATENCY_BUCKETS:
            grp = bucket_data.get(label, [])
            if not grp:
                continue
            wr        = sum(1 for p in grp if p["won"]) / len(grp) * 100
            avg_pnl   = mean(p["pnl_sol"] for p in grp)
            avg_drift = mean(p["drift_pct"] for p in grp)
            flag = " ⚠" if wr < 40 or avg_drift > 0.5 else ""
            print(f"  {label:<20}  {len(grp):>4}  {wr:>5.1f}%  "
                  f"{avg_pnl:>+9.4f}◎  {avg_drift:>+9.3f}%{flag}")

    # ── Recommendations ────────────────────────────────────────────────────
    _hdr("RECOMMENDATIONS")
    _recommendations(all_pairs, all_latencies)
    print()


def print_token_breakdown(all_pairs: list) -> None:
    """
    Per-token slippage breakdown with Penalty-to-Drift correlation.

    Only Jupiter-quoted closed trades are included — DexScreener
    trades are excluded because their 0% drift would skew averages.
    """
    groups = build_token_groups(all_pairs)
    if not groups:
        print("  No token groups with ≥2 Jupiter-quoted closed trades yet.")
        return

    jup_total = sum(1 for p in all_pairs if p.get("has_quote") and p.get("won") is not None)
    _hdr(f"TOKEN SLIPPAGE BREAKDOWN  ({len(groups)} symbols  |  {jup_total} Jupiter-quoted closed trades)")

    print(
        f"\n  {'SYM':<10}  {'N':>4}  {'DRIFT avg':>9}  {'DRIFT p90':>9}"
        f"  {'>0.5% adv':>9}  {'EX_PEN':>6}  {'P/D':>5}  FLAGS"
    )
    print(f"  {'─'*10}  {'─'*4}  {'─'*9}  {'─'*9}  {'─'*9}  {'─'*6}  {'─'*5}  {'─'*24}")
    for g in groups:
        pen_s   = f"{g['mean_pen']:.3f}" if g["mean_pen"] is not None else "   n/a"
        ratio_s = f"{g['pd_ratio']:.2f}" if g["pd_ratio"] is not None else "  n/a"
        flag_s  = "  ".join(g["flags"]) if g["flags"] else "—"
        pen_n_s = f"({g['pen_n']})" if g["pen_n"] < g["trades"] else ""
        print(
            f"  {g['symbol']:<10}  {g['trades']:>4}  {g['mean_drift']:>+8.3f}%"
            f"  {g['p90_drift']:>+8.3f}%  {g['pct_adv']:>8.0f}%"
            f"  {pen_s:>6}{pen_n_s:<4}  {ratio_s:>5}  {flag_s}"
        )

    print("""
  P/D ratio  =  (exec_penalty × 5%) / drift_pct
    Compares sell-side execution quality (exec_penalty) to buy-side (drift_pct).
    > 2.0  SELL_WORSE   — exit fills are proportionally worse than entries
    ~1.0   calibrated   — symmetric slippage on both legs
    < 0.5  ENTRY_WORSE  — buy-side slippage dominates; penalty under-estimates entry cost
    n/a                 — exec_penalty not yet in DB (pre-session-53) or no adverse drift
  Note: (N) next to EX_PEN means only N of the trades have session-53 penalty data.""")


def print_full_csv(results: list) -> None:
    """
    CSV trade log sorted by drift_pct descending.

    Columns:
      timestamp      — UTC open time of the trade
      bot            — Bot1 / Bot2 / Bot3
      mint           — full on-chain mint address
      symbol         — token symbol (from open/signal event)
      tier           — execution tier (quick/gem/highconv/?)
      drift_pct      — buy-side slippage % (+ = adverse; Jupiter data only is meaningful)
      drift_source   — Jupiter (accurate) or DexScreener (always 0% pre-session-43)
      exec_penalty   — sell-side execution penalty [0–1] from session-53 close events
      pen_pct_equiv  — exec_penalty × 5%  (sell slippage as a comparable %)
      pnl_sol        — trade PnL in SOL
      lag_ms         — signal-fire to open-logged latency in ms
      outcome        — WIN / LOSS / (blank = no matching close yet)
    """
    tagged = []
    for r in results:
        for p in r.get("pairs", []):
            tagged.append({**p, "bot": r["name"]})

    if not tagged:
        print("# no paired trades found")
        return

    sorted_pairs = sorted(tagged, key=lambda p: p["drift_pct"], reverse=True)

    _hdr(f"FULL TRADE LOG  (CSV)  —  {len(sorted_pairs)} trades, sorted by drift_pct descending")
    print("# Tip: grep from the line starting with 'timestamp' to get clean CSV")
    print()
    print("timestamp,bot,mint,symbol,tier,drift_pct,drift_source,"
          "exec_penalty,pen_pct_equiv,pnl_sol,lag_ms,outcome")
    for p in sorted_pairs:
        ts       = p["open_ts"].isoformat() if hasattr(p["open_ts"], "isoformat") else str(p["open_ts"])
        pen      = p.get("exec_penalty")
        pen_s    = f"{pen:.6f}"       if pen  is not None else ""
        ppeq_s   = f"{pen * 5.0:.6f}" if pen is not None else ""
        pnl_s    = f"{p['pnl_sol']:.6f}" if p.get("pnl_sol") is not None else ""
        outcome  = ("WIN" if p["won"] else "LOSS") if p.get("won") is not None else ""
        print(
            f"{ts},{p['bot']},{p['mint']},{p.get('symbol','?')},{p.get('tier','?')},"
            f"{p['drift_pct']:.4f},{p.get('drift_source','?')},"
            f"{pen_s},{ppeq_s},{pnl_s},{p['exec_lag_ms']:.0f},{outcome}"
        )


# ── Per-bot data collection ───────────────────────────────────────────────────

def analyze_bot(name: str, db_path: str) -> dict:
    """Load events and pair them.  Returns a structured result dict (no printing)."""
    if not Path(db_path).exists():
        print(f"  {name}: DB not found — skipping", file=sys.stderr)
        return {}

    evts       = load_events(db_path)
    signals    = evts.get("signal", [])
    opens      = evts.get("open", [])
    closes     = evts.get("close", [])
    tx_results = evts.get("tx_result", [])

    confirmed = [r for r in tx_results if r.get("confirmed") and r.get("latency_ms")]
    failed    = [r for r in tx_results if not r.get("confirmed") and r.get("latency_ms")]
    latencies = [r["latency_ms"] for r in confirmed]

    pairs = pair_signal_to_open(signals, opens)
    pairs = attach_close_data(pairs, closes)

    return {
        "name":        name,
        "pairs":       pairs,
        "latencies":   latencies,
        "n_confirmed": len(confirmed),
        "n_failed":    len(failed),
        "n_signals":   len(signals),
        "n_opens":     len(opens),
        "n_closes":    len(closes),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DATBOI execution audit: latency, drift, and penalty correlation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python3 slippage_audit.py                  # quick Fleet Health Score
  python3 slippage_audit.py --report-full    # full breakdown + token table + CSV
  python3 slippage_audit.py --report-full > audit.txt   # save to file
""",
    )
    parser.add_argument(
        "--report-full",
        action="store_true",
        help="Full output: per-token slippage table with penalty correlation + CSV trade log",
    )
    args = parser.parse_args()

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode   = "FULL" if args.report_full else "SUMMARY  (--report-full for breakdown + CSV)"
    print(f"DATBOI EXECUTION AUDIT  {ts_str}  |  {mode}")
    print(f"Signal match window: {SIGNAL_MATCH_WINDOW_S}s  |  Min token trades: {_MIN_TOKEN_TRADES}")

    results = []
    for name, path in BOT_DBS.items():
        result = analyze_bot(name, path)
        if result:
            results.append(result)

    print_default_report(results)

    if args.report_full:
        all_pairs = [p for r in results for p in r.get("pairs", [])]
        print_token_breakdown(all_pairs)
        print_full_csv(results)
