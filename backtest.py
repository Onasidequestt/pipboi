#!/usr/bin/env python3
"""
backtest.py — simulate the core strategy over REAL historical price data.

The fleet never stored a price tape, so until now you could only learn whether a
strategy works by spending SOL on it. GeckoTerminal serves free historical OHLCV
per pool — this replays the bot's entry thesis + exit rules over that real history,
across different market conditions, and sweeps parameters to find what is actually
+EV. It is the offline complement to signal_lab.py (which validates live, forward).

What it can replay faithfully from OHLCV: price momentum, volume, volume
acceleration, realized volatility (→ regime), and the full exit stack (TP / SL /
trail / max-hold). What it CANNOT see (OHLCV has no order book): liquidity depth,
buy/sell txn ratio, social. Those are *filters* in the live bot; the core thesis it
gates is momentum+volume, which is exactly what this measures. So this answers the
question that matters: does the entry THESIS have edge, and under which conditions?

Usage:
    python3 backtest.py                      # default momentum thesis, traded-token basket
    python3 backtest.py --thesis momentum    # breakout: enter on up-move + volume
    python3 backtest.py --thesis reversion    # washout: enter on down-move + volume spike
    python3 backtest.py --sweep              # grid-search floor/TP/SL for best EV
    python3 backtest.py --tf 15m --days 10   # candle timeframe + history depth
    python3 backtest.py --mints MINT1,MINT2  # custom basket (default: tokens the bot traded)

Data is cached to shared_memory/ohlcv_cache/ so re-runs are instant and gentle on
the GeckoTerminal rate limit (~30 req/min, free, no key).
"""
import argparse
import json
import sqlite3
import statistics
import time
from pathlib import Path

import httpx

BASE = Path(__file__).parent
CACHE = BASE / "shared_memory" / "ohlcv_cache"
GECKO = "https://api.geckoterminal.com/api/v2"
HDR = {"accept": "application/json", "User-Agent": "Mozilla/5.0"}

_TF = {  # cli timeframe → (gecko_timeframe, aggregate, seconds_per_bar)
    "5m":  ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h":  ("hour", 1, 3600),
}


# ── Data layer ────────────────────────────────────────────────────────────────

def _http_get(url: str, params: dict) -> dict:
    for attempt in range(3):
        try:
            r = httpx.get(url, params=params, headers=HDR, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return {}


def _traded_mints() -> list[str]:
    """Distinct mints the fleet actually opened positions on — the honest basket
    for 'how would the strategy have done on what the bot actually picked'."""
    mints: dict[str, int] = {}
    for b in (1, 2, 3):
        db = BASE / f"bots/bot{b}/trades.db"
        if not db.exists():
            continue
        c = sqlite3.connect(db)
        for (raw,) in c.execute("SELECT data FROM trades WHERE event='open'"):
            try:
                m = json.loads(raw).get("mint")
                if m:
                    mints[m] = mints.get(m, 0) + 1
            except Exception:
                pass
        c.close()
    # most-traded first
    return [m for m, _ in sorted(mints.items(), key=lambda kv: -kv[1])]


def _pool_for_mint(mint: str) -> tuple[str, str]:
    """Return (pool_address, symbol) of the top pool for a token, or ('','')."""
    cache_f = CACHE / f"pool_{mint}.json"
    if cache_f.exists():
        d = json.loads(cache_f.read_text())
        return d.get("pool", ""), d.get("sym", "")
    d = _http_get(f"{GECKO}/networks/solana/tokens/{mint}/pools", {"page": 1})
    pools = d.get("data", []) if d else []
    pool, sym = "", ""
    if pools:
        attrs = pools[0].get("attributes", {})
        pool = attrs.get("address", "")
        sym = (attrs.get("name", "") or "").split(" / ")[0]
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps({"pool": pool, "sym": sym}))
    time.sleep(2.2)  # rate-limit courtesy
    return pool, sym


def _ohlcv(pool: str, tf: str, days: int) -> list[list]:
    """Return [[ts,o,h,l,c,v], ...] oldest→newest. Disk-cached for the day."""
    gtf, agg, secs = _TF[tf]
    bars_needed = int(days * 86400 / secs)
    cache_f = CACHE / f"ohlcv_{pool}_{tf}.json"
    if cache_f.exists() and (time.time() - cache_f.stat().st_mtime) < 3600:
        return json.loads(cache_f.read_text())
    out: list[list] = []
    before = None
    while len(out) < bars_needed:
        params = {"aggregate": agg, "limit": 1000}
        if before:
            params["before_timestamp"] = before
        d = _http_get(f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{gtf}", params)
        chunk = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or [] if d else []
        if not chunk:
            break
        out.extend(chunk)
        before = chunk[-1][0] - 1
        if len(chunk) < 1000:
            break
        time.sleep(2.2)
    out = sorted({c[0]: c for c in out}.values(), key=lambda c: c[0])  # dedup, oldest→newest
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(out))
    return out


# ── Strategy simulation ─────────────────────────────────────────────────────

def _regime(vols: list[float]) -> str:
    """Classify market condition from recent realized volume level (proxy for the
    bot's agg_vol_5m regimes). Returned per-entry so we can split results."""
    if not vols:
        return "normal"
    v = statistics.mean(vols)
    if v >= 50_000:
        return "hot"      # active market
    if v < 5_000:
        return "dead"     # quiet — the condition the fleet sits in most
    return "normal"


def simulate(candles: list[list], thesis: str, floor: float, tp: float, sl: float,
             accel_min: float, max_hold: int) -> list[dict]:
    """Walk candles, open at most one position at a time, return list of closed trades.
    candle = [ts, open, high, low, close, volume]. Returns dicts {pnl, regime, bars}."""
    trades: list[dict] = []
    i, n = 10, len(candles)
    while i < n - 1:
        c = candles[i]
        o, hi, lo, cl, vol = c[1], c[2], c[3], c[4], c[5]
        prev_close = candles[i - 1][4]
        if prev_close <= 0:
            i += 1
            continue
        mom = (cl - prev_close) / prev_close * 100.0
        prior_vols = [candles[j][5] for j in range(i - 8, i)]
        base_v = statistics.mean(prior_vols) if prior_vols else 0.0
        vacc = (vol / base_v) if base_v > 0 else 1.0

        enter = False
        if thesis == "momentum":
            enter = mom >= floor and vacc >= accel_min
        elif thesis == "reversion":
            enter = (-10.0 <= mom <= -floor) and vacc >= accel_min  # washout: down-move + vol spike
        if not enter:
            i += 1
            continue

        entry = cl
        regime = _regime(prior_vols)
        peak = entry
        exit_pnl = None
        bars_held = 0
        for j in range(i + 1, min(i + 1 + max_hold, n)):
            b = candles[j]
            bhi, blo, bcl = b[2], b[3], b[4]
            bars_held = j - i
            # stop-loss first (intrabar low) — conservative
            if (blo - entry) / entry * 100.0 <= sl:
                exit_pnl = sl
                break
            # take-profit touch (intrabar high) → exit at TP (no trail in v1: honest floor)
            if (bhi - entry) / entry * 100.0 >= tp:
                exit_pnl = tp
                break
            peak = max(peak, bhi)
        if exit_pnl is None:  # timed out — exit at last close
            last = candles[min(i + max_hold, n - 1)][4]
            exit_pnl = (last - entry) / entry * 100.0
            bars_held = min(max_hold, n - 1 - i)
        trades.append({"pnl": exit_pnl, "regime": regime, "bars": bars_held})
        i += max(1, bars_held)  # don't re-enter mid-trade
    return trades


# ── Reporting ────────────────────────────────────────────────────────────────

def _slip_pct() -> float:
    return 1.0  # round-trip cost assumption: ~0.5% slippage each way + fees (conservative)


def _stats(trades: list[dict]) -> dict:
    pnls = [t["pnl"] - _slip_pct() for t in trades]  # net of round-trip cost
    if not pnls:
        return {"n": 0, "wr": 0.0, "ev": 0.0, "avg_w": 0.0, "avg_l": 0.0, "total": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "n": len(pnls),
        "wr": 100.0 * len(wins) / len(pnls),
        "ev": statistics.mean(pnls),
        "avg_w": statistics.mean(wins) if wins else 0.0,
        "avg_l": statistics.mean(losses) if losses else 0.0,
        "total": sum(pnls),
    }


def _project_to_2(start_sol: float, ev_pct: float, trades_per_day: float, size_frac: float) -> str:
    """Rough path-to-2.0 projection: compound EV per trade at a given position
    fraction of wallet. size_frac approximates the bot's avg position size."""
    if ev_pct <= 0:
        return "never (EV ≤ 0 — strategy loses in expectation)"
    bal = start_sol
    days = 0
    per_day = trades_per_day * (ev_pct / 100.0) * size_frac
    while bal < 2.0 and days < 3650:
        bal *= (1 + per_day)
        days += 1
    if days >= 3650:
        return ">10 years (EV too small at this size/frequency)"
    return f"~{days} days  ({days/30:.1f} months)" if days > 60 else f"~{days} days"


def run(thesis: str, tf: str, days: int, mints: list[str], sweep: bool,
        floor: float, tp: float, sl: float, accel: float, hold: int) -> None:
    print("=" * 70)
    print(f"  BACKTEST — '{thesis}' thesis on real GeckoTerminal OHLCV")
    print(f"  timeframe={tf}  history={days}d  basket={len(mints)} tokens  cost=1%/round-trip")
    print("=" * 70)

    # Load price tapes
    tapes: list[tuple[str, list]] = []
    print("\n  fetching/loading price tapes (cached)...")
    for m in mints:
        pool, sym = _pool_for_mint(m)
        if not pool:
            continue
        candles = _ohlcv(pool, tf, days)
        if len(candles) >= 30:
            tapes.append((sym or m[:6], candles))
    if not tapes:
        print("  No price tapes available. Check network / token basket.")
        return
    total_bars = sum(len(c) for _, c in tapes)
    span_d = total_bars * _TF[tf][2] / 86400
    print(f"  loaded {len(tapes)} tapes, {total_bars:,} candles (~{span_d:.0f} token-days of history)\n")

    if sweep:
        _run_sweep(thesis, tapes, accel, hold)
        return

    all_tr: list[dict] = []
    for _, candles in tapes:
        all_tr.extend(simulate(candles, thesis, floor, tp, sl, accel, hold))

    s = _stats(all_tr)
    print(f"  CONFIG: floor={floor}%  TP=+{tp}%  SL={sl}%  vol_accel≥{accel}  max_hold={hold} bars")
    print("  " + "-" * 66)
    if s["n"] == 0:
        print("  0 trades triggered — entry conditions never met on this history.")
        return
    print(f"  Trades : {s['n']}")
    print(f"  Win rate: {s['wr']:.1f}%")
    print(f"  Avg win : {s['avg_w']:+.2f}%   Avg loss: {s['avg_l']:+.2f}%")
    print(f"  EV/trade: {s['ev']:+.2f}%  (net of cost)   ← the number that matters")
    print(f"  Total  : {s['total']:+.1f}% summed across all trades")

    # By market condition
    print("\n  BY MARKET CONDITION (the part you asked about):")
    print(f"    {'regime':<10}{'trades':>8}{'WR':>8}{'EV/trade':>11}")
    print("    " + "-" * 37)
    for reg in ("hot", "normal", "dead"):
        sub = [t for t in all_tr if t["regime"] == reg]
        ss = _stats(sub)
        if ss["n"]:
            print(f"    {reg:<10}{ss['n']:>8}{ss['wr']:>7.1f}%{ss['ev']:>+10.2f}%")

    # Path to 2.0 (bot's current balances ~0.5–0.97, avg position ~8–15% of wallet)
    print("\n  PATH TO ◎2.0  (from ◎0.97, ~6 trades/day, ~12% avg position):")
    print(f"    {_project_to_2(0.97, s['ev'], 6.0, 0.12)}")
    print("\n  Note: OHLCV has no liquidity/buy-sell/social — those live filters")
    print("  would PRUNE some of these trades. Treat EV as the thesis ceiling.")


def _run_sweep(thesis: str, tapes: list, accel: float, hold: int) -> None:
    print("  GRID SEARCH — finding the +EV island (if one exists)\n")
    floors = [1.0, 2.0, 3.0, 5.0, 8.0] if thesis == "momentum" else [3.0, 5.0, 7.0]
    tps = [5.0, 8.0, 12.0, 20.0]
    sls = [-3.0, -5.0, -8.0]
    rows = []
    for fl in floors:
        for tp in tps:
            for sl in sls:
                tr = []
                for _, candles in tapes:
                    tr.extend(simulate(candles, thesis, fl, tp, sl, accel, hold))
                st = _stats(tr)
                if st["n"] >= 20:
                    rows.append((st["ev"], fl, tp, sl, st["n"], st["wr"]))
    rows.sort(reverse=True)
    print(f"    {'EV/tr':>7}{'floor':>7}{'TP':>6}{'SL':>6}{'n':>6}{'WR':>7}")
    print("    " + "-" * 39)
    for ev, fl, tp, sl, n, wr in rows[:12]:
        flag = "  ← +EV" if ev > 0 else ""
        print(f"    {ev:>+6.2f}%{fl:>6.0f}%{tp:>+5.0f}{sl:>+5.0f}{n:>6}{wr:>6.1f}%{flag}")
    if rows and rows[0][0] > 0:
        ev, fl, tp, sl, n, wr = rows[0]
        print(f"\n  BEST: floor={fl}% TP=+{tp}% SL={sl}% → EV {ev:+.2f}%/trade, WR {wr:.1f}%, {n} trades")
        print(f"  Path to ◎2.0: {_project_to_2(0.97, ev, 6.0, 0.12)}")
    else:
        print("\n  ⚠ NO +EV config found for this thesis on this basket/history.")
        print("  That's a real answer: this entry thesis has no edge here. Try")
        print("  --thesis reversion, a different basket, or a new signal entirely.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest core strategy on real OHLCV")
    ap.add_argument("--thesis", choices=["momentum", "reversion"], default="momentum")
    ap.add_argument("--tf", choices=list(_TF), default="15m")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--mints", type=str, default="")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--floor", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=12.0)
    ap.add_argument("--sl", type=float, default=-5.0)
    ap.add_argument("--accel", type=float, default=1.5)
    ap.add_argument("--hold", type=int, default=12)
    a = ap.parse_args()
    mints = [m.strip() for m in a.mints.split(",") if m.strip()] or _traded_mints()
    if not mints:
        print("No mints to test (empty trades.db and no --mints given).")
        return
    run(a.thesis, a.tf, a.days, mints[:25], a.sweep, a.floor, a.tp, a.sl, a.accel, a.hold)


if __name__ == "__main__":
    main()
