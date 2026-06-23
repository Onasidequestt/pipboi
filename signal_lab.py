#!/usr/bin/env python3
"""
signal_lab.py — Offline signal-validation harness (P1).

The fleet's real blocker is edge, not tuning. You can only learn whether a
signal predicts profit by spending SOL on it — 4 clean closes at a time. This
removes that constraint: it rides the discovery sidecar's existing snapshot
(zero extra API load, zero trading risk), records every scored token's features
+ price each cycle, then measures the *forward price move* that followed. That
tells you which data points actually predict a profitable trade — before you
ever risk capital on them.

Usage:
    python3 signal_lab.py --log              # run the logger daemon (alongside the fleet)
    python3 signal_lab.py                     # analyze: which signals predict forward returns
    python3 signal_lab.py --horizon 60        # forward-return horizon in minutes (default 30)
    python3 signal_lab.py --target 5.0        # "win" = forward return >= this % (default 3.0)

The logger reads shared_memory/discovery_snapshot.json (written by the sidecar
every ~10s) and appends compact rows to shared_memory/signal_log.jsonl. It dedups
by snapshot version, so each cycle is logged once. Storage is ~25 MB/day; prune
the jsonl when you've extracted what you need.
"""
import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path

_SNAP_PATH = Path(__file__).parent / "shared_memory" / "discovery_snapshot.json"
_LOG_PATH = Path(__file__).parent / "shared_memory" / "signal_log.jsonl"
_BOT1_STATUS = Path(__file__).parent / "bots" / "bot1" / "status.json"
_POLL_SECS = 20.0


# ─────────────────────────────────────────────────────────── logger ──

def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _scores_by_mint():
    """Best-effort: pull the composite ValidationProfile score + conf per mint
    from bot1's live evals (INSANE = widest universe)."""
    s = _read_json(_BOT1_STATUS)
    out = {}
    if not s:
        return out
    evals = (s.get("thinking", {}) or {}).get("tier1_observer", {}).get("token_evals", []) or []
    for e in evals:
        m = e.get("mint")
        if m:
            out[m] = {"score": e.get("score"), "conf": e.get("conf")}
    return out


def _debater_by_mint():
    """Best-effort: the LIVE debater verdict per mint from bot1's status (INSANE = the
    only mode that runs the debater). Records the REAL gate decision so the brain can
    forward-validate it — no offline reconstruction error. Only standard-path signals
    that reached the debater appear here; everything else is left unmarked."""
    s = _read_json(_BOT1_STATUS)
    out = {}
    if not s:
        return out
    for e in (s.get("thinking", {}) or {}).get("debater", []) or []:
        m = e.get("mint")
        if m and "passed" in e:
            out[m] = bool(e["passed"])
    return out


def _capture_row(snap, scores, bc_set, dbt_map=None):
    """Yield one compact record per token that has price data this cycle."""
    ts = time.time()
    md = snap.get("market_data", {}) or {}
    liqv = snap.get("liq_velocity", {}) or {}
    agg = snap.get("agg_vol_5m", 0.0)
    for mint, d in md.items():
        price = d.get("price_usd") or 0.0
        if price <= 0:
            continue
        b = d.get("txns_5m_buys", 0) or 0
        sells = d.get("txns_5m_sells", 0) or 0
        sc = scores.get(mint, {})
        liq = d.get("liquidity_usd", 0.0) or 0.0
        mc = d.get("market_cap", 0.0) or 0.0
        fdv = d.get("fdv", 0.0) or 0.0
        # Live debater verdict (B1): 1=passed, -1=vetoed, key absent=not evaluated.
        # Separable under _num default 0 → debater_pass / debater_veto candidates.
        _row = {
            "ts": round(ts, 1),
            "mint": mint,
            "sym": d.get("symbol", ""),
            "px": price,
            "liq": liq,
            "mc": mc,
            "fdv": fdv,
            # Quality ratios (P4.3, zero API cost): a deep pool relative to market cap
            # is harder to rug and easier to exit. A thin pool vs a huge FDV is a ghost/rug
            # signature (the WEN/SV15 ghost-close pattern).
            "liq_mc": (liq / mc) if mc > 0 else 0.0,
            "liq_fdv": (liq / fdv) if fdv > 0 else 0.0,
            "v5": d.get("volume_5m", 0.0) or 0.0,
            "v1h": d.get("volume_1h", 0.0) or 0.0,
            "m5": d.get("price_change_5m", 0.0) or 0.0,
            "m1h": d.get("price_change_1h", 0.0) or 0.0,
            "b5": b,
            "s5": sells,
            "bs": (b / sells) if sells else (float(b) if b else 0.0),
            "lqv": liqv.get(mint, 0.0) or 0.0,
            "whale": 1 if mint in bc_set else 0,
            "score": sc.get("score"),
            "conf": sc.get("conf"),
            "agg": agg,
            # S121 V2: free RugCheck pre-entry risk features (None unless rugcheck_score canary on +
            # this mint was enriched this/a recent cycle). Telemetry only → the GA can learn on them.
            "rc_score_norm":    d.get("rc_score_norm"),
            "rc_lp_locked_pct": d.get("rc_lp_locked_pct"),
            "rc_n_risks":       d.get("rc_n_risks"),
        }
        if dbt_map and mint in dbt_map:
            _row["dbt"] = 1 if dbt_map[mint] else -1
        yield _row


def run_logger():
    _LOG_PATH.parent.mkdir(exist_ok=True)
    last_ver = None
    print(f"[signal_lab] logging → {_LOG_PATH}  (poll {_POLL_SECS:.0f}s, dedup by version)", flush=True)
    written = 0
    while True:
        try:
            snap = _read_json(_SNAP_PATH)
            if snap and snap.get("version") != last_ver:
                last_ver = snap.get("version")
                bc_set = set(snap.get("bc_hot", []) or [])
                scores = _scores_by_mint()
                dbt_map = _debater_by_mint()
                rows = list(_capture_row(snap, scores, bc_set, dbt_map))
                if rows:
                    with open(_LOG_PATH, "a") as fh:
                        for r in rows:
                            fh.write(json.dumps(r) + "\n")
                    written += len(rows)
                    if written % 500 < len(rows):
                        print(f"[signal_lab] v{last_ver}  +{len(rows)} rows  ({written} total this run)", flush=True)
        except KeyboardInterrupt:
            print("\n[signal_lab] stopped", flush=True)
            break
        except Exception as exc:
            print(f"[signal_lab] warn: {exc}", flush=True)
        time.sleep(_POLL_SECS)


# ───────────────────────────────────────────────────────── analyzer ──

def _regime_of(row) -> str:
    """Market condition of a logged row, from the fleet aggregate 5m volume it
    captured (agg). S85: upgraded 3-band → 5-band to MATCH the live S79 regime ladder.
    The old hot/normal/dead lumped the +EV `sniper` band ($48-110k, shadow ev_lo +5.4)
    into dead — the brain literally could not see its best regime. Diagnostic only
    (by_regime is context for promotion, not a gate), so this changes NO trading behavior."""
    agg = row.get("agg", 0.0) or 0.0
    if agg >= 600_000:  return "euphoria"
    if agg >= 280_000:  return "aggressive"
    if agg >= 110_000:  return "normal"
    if agg >= 48_000:   return "sniper"
    return "dead"


def _load_rows():
    if not _LOG_PATH.exists():
        return []
    rows = []
    with open(_LOG_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


# Forward returns are winsorized to this band before any averaging. A handful of
# glitched micro-cap prices (one was +524,370%) otherwise destroy every mean. The
# bot can't realistically capture >+100% in a 30m hold anyway (TP/trail cap it), and
# a rug is bounded near -100%, so clipping here removes garbage without losing signal.
_FWD_CLIP = (-90.0, 100.0)


def _attach_forward_returns(rows, horizon_min):
    """For each row, find the same mint's price closest to ts + horizon
    (within [0.6H, 2.0H]) and attach fwd return %. Also rolling vol_accel.
    Forward returns are winsorized to _FWD_CLIP to neutralize price glitches."""
    by_mint = defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    h = horizon_min * 60.0
    lo, hi = h * 0.6, h * 2.0
    out = []
    for mint, series in by_mint.items():
        series.sort(key=lambda r: r["ts"])
        # rolling mean volume_5m for vol-accel (uses prior rows only)
        vols = []
        for i, r in enumerate(series):
            prior = vols[-8:]
            base = (sum(prior) / len(prior)) if prior else 0.0
            r["vacc"] = (r["v5"] / base) if base > 0 else 1.0
            vols.append(r["v5"])
            # forward price
            tgt = r["ts"] + h
            best = None
            for f in series[i + 1:]:
                dt = f["ts"] - r["ts"]
                if dt < lo:
                    continue
                if dt > hi:
                    break
                if best is None or abs(f["ts"] - tgt) < abs(best["ts"] - tgt):
                    best = f
            if best and r["px"] > 0:
                _raw = (best["px"] / r["px"] - 1.0) * 100.0
                r["fwd"] = max(_FWD_CLIP[0], min(_FWD_CLIP[1], _raw))
                # fwd_min: worst price hit ANYWHERE from entry up to the matched exit point.
                # A token can log fwd +5% while having dipped −70% mid-hold — untradeable with
                # any stop. This is the exitability/survivorship check (the S61 illusion). It is
                # ≤ fwd by construction (the min includes the endpoint).
                _dt_best = best["ts"] - r["ts"]
                _min_px = best["px"]
                # fwd_max: best price hit anywhere from entry up to the matched exit (≥ fwd by
                # construction). With fwd_min (trough) it gives the full intra-hold envelope, so
                # exit rails (TP / ride-vs-bank) can be optimized on what the price ACTUALLY did
                # mid-hold — not just the endpoint. S70 tuned tp1 from the endpoint distribution
                # alone (no peak); once fwd_max matures the brain can derive the optimal TP directly.
                _max_px = best["px"]
                for f in series[i + 1:]:
                    _dt = f["ts"] - r["ts"]
                    if _dt <= 0:
                        continue
                    if _dt > _dt_best:
                        break
                    if 0 < f["px"] < _min_px:
                        _min_px = f["px"]
                    if f["px"] > _max_px:
                        _max_px = f["px"]
                _raw_min = (_min_px / r["px"] - 1.0) * 100.0
                r["fwd_min"] = max(_FWD_CLIP[0], min(_FWD_CLIP[1], _raw_min))
                _raw_max = (_max_px / r["px"] - 1.0) * 100.0
                r["fwd_max"] = max(_FWD_CLIP[0], min(_FWD_CLIP[1], _raw_max))
                out.append(r)
    return out


# ── Durable matured-observation store (24h-span persistence) ──────────────────
# signal_log.jsonl is prunable for size; pruning it resets the readiness-bar span
# clock, making the 24h live-gate structurally unreachable. Fix: once a row's
# forward window has fully elapsed (now > ts + 2.0·horizon) its fwd return is
# FINAL — copy {features + fwd} into an append-only archive that is NEVER pruned.
# The brain reads archive ∪ fresh-raw, so the span grows monotonically forever.
# Rows are downsampled to one per (mint, 5-min bucket): cuts volume ~15× AND makes
# observations closer to independent, so n / EV / std are statistically honest.
_DURABLE_PATH = Path(__file__).parent / "shared_memory" / "forward_obs.jsonl"
_BUCKET_SECS = 300.0
# Realizable EV: a forward return on a token you can't exit isn't profit. $50k
# mirrors observer._MIN_SELL_LIQ_USD — the bot won't enter below it post-S57, so
# scoring EV only on sellable tokens matches what it can actually trade.
SELL_FLOOR = 50_000.0
_DURABLE_FIELDS = ("ts", "mint", "sym", "px", "liq", "mc", "fdv", "liq_mc",
                   "liq_fdv", "v5", "v1h", "m5", "m1h", "b5", "s5", "bs", "lqv",
                   "whale", "score", "conf", "agg", "vacc", "dbt", "fwd", "fwd_min", "fwd_max")


def _bucket_key(r, horizon_min):
    return f"{r['mint']}:{int(r['ts'] // _BUCKET_SECS)}:{horizon_min}"


def _durable_keys():
    keys = set()
    if _DURABLE_PATH.exists():
        with open(_DURABLE_PATH) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    keys.add(_bucket_key(d, d.get("h")))
                except Exception:
                    pass
    return keys


def harvest_durable(horizon_min):
    """Append newly-matured (final-fwd) observations to the durable archive.
    Idempotent — deduped by (mint, 5-min bucket, horizon). Returns rows added."""
    rows = _load_rows()
    if not rows:
        return 0
    fwd = _attach_forward_returns(rows, horizon_min)
    now = time.time()
    cutoff = horizon_min * 60.0 * 2.0   # past this, no closer forward price can arrive
    seen = _durable_keys()
    added = 0
    _DURABLE_PATH.parent.mkdir(exist_ok=True)
    with open(_DURABLE_PATH, "a") as fh:
        for r in fwd:
            if now - r["ts"] <= cutoff:
                continue
            key = _bucket_key(r, horizon_min)
            if key in seen:
                continue
            rec = {k: r.get(k) for k in _DURABLE_FIELDS}
            rec["h"] = horizon_min
            fh.write(json.dumps(rec) + "\n")
            seen.add(key)
            added += 1
    return added


def _load_durable(horizon_min):
    out = []
    if not _DURABLE_PATH.exists():
        return out
    with open(_DURABLE_PATH) as fh:
        for line in fh:
            try:
                d = json.loads(line)
                if d.get("h") == horizon_min and "fwd" in d:
                    out.append(d)
            except Exception:
                pass
    return out


def load_matured(horizon_min):
    """Brain evidence base: durable archive ∪ fresh raw, bucket-deduped (durable
    wins — its fwd is final). Span over this set is monotonic across raw pruning."""
    fresh = _attach_forward_returns(_load_rows(), horizon_min)
    durable = _load_durable(horizon_min)
    best = {}
    for r in fresh:                      # fresh first …
        best[_bucket_key(r, horizon_min)] = r
    for r in durable:                    # … durable overwrites (final value)
        best[_bucket_key(r, horizon_min)] = r
    return list(best.values())


def _stats(rows, target):
    if not rows:
        return (0, 0.0, 0.0, 0.0)
    n = len(rows)
    mean = sum(r["fwd"] for r in rows) / n
    wins = sum(1 for r in rows if r["fwd"] >= target)
    median = sorted(r["fwd"] for r in rows)[n // 2]
    return (n, mean, 100.0 * wins / n, median)


def _bucket_report(title, rows, keyfn, buckets, target):
    print(f"\n  {title}")
    print(f"    {'bucket':<16}{'n':>6}{'mean fwd%':>11}{'win%':>8}{'median%':>9}")
    print("    " + "-" * 50)
    for label, pred in buckets:
        sub = [r for r in rows if pred(keyfn(r))]
        n, mean, wr, med = _stats(sub, target)
        if n == 0:
            print(f"    {label:<16}{0:>6}{'  —':>11}{'  —':>8}{'  —':>9}")
        else:
            print(f"    {label:<16}{n:>6}{mean:>+11.2f}{wr:>8.1f}{med:>+9.2f}")


def run_report(horizon_min, target):
    rows = _load_rows()
    print("=" * 64)
    print(f"  SIGNAL LAB — forward-return edge  (horizon {horizon_min}m, win ≥ +{target}%)")
    print("=" * 64)
    if not rows:
        print("\n  No data yet. Start the logger:  python3 signal_lab.py --log")
        print("  Let it run a few hours, then re-run this report.")
        return
    span_h = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 3600.0
    mints = len({r["mint"] for r in rows})
    print(f"\n  {len(rows):,} observations · {mints} unique tokens · {span_h:.1f}h coverage")

    fwd = _attach_forward_returns(rows, horizon_min)
    if not fwd:
        print(f"\n  Not enough paired observations at {horizon_min}m horizon yet.")
        print("  Need the logger to run for at least ~2× the horizon. Keep collecting.")
        return

    n, mean, wr, med = _stats(fwd, target)
    print(f"\n  BASELINE (every observation held {horizon_min}m):")
    print(f"    n={n:,}  mean fwd={mean:+.2f}%  win%={wr:.1f}  median={med:+.2f}%")
    print(f"    ↑ This is the bar. A signal only has edge if it beats this baseline.")

    # Exitability / survivorship (B2): how deep did the 'winners' dip mid-hold? A fwd≥target
    # win that drank a −STOP% drawdown first was NOT actually capturable with a stop in place.
    _dd = [r for r in fwd if "fwd_min" in r]
    if _dd:
        _winners = [r for r in _dd if r["fwd"] >= target]
        med_min = sorted(r["fwd_min"] for r in _dd)[len(_dd) // 2]
        _STOP = -20.0
        trapped = sum(1 for r in _winners if r["fwd_min"] <= _STOP)
        wpct = (100.0 * trapped / len(_winners)) if _winners else 0.0
        print(f"\n  EXITABILITY (fwd_min = worst price mid-hold):")
        print(f"    median fwd_min={med_min:+.2f}%  ·  {wpct:.0f}% of fwd≥+{target}% 'wins' "
              f"dipped ≤{_STOP:.0f}% first (untradeable w/ a {_STOP:.0f}% stop)")

    _bucket_report("MOMENTUM  (price_change_5m at signal time)", fwd, lambda r: r["m5"], [
        ("< 0%", lambda v: v < 0),
        ("0–2%", lambda v: 0 <= v < 2),
        ("2–5%", lambda v: 2 <= v < 5),
        ("5–10%", lambda v: 5 <= v < 10),
        (">= 10%", lambda v: v >= 10),
    ], target)

    _bucket_report("BUY/SELL RATIO (5m txns)", fwd, lambda r: r["bs"], [
        ("< 0.8", lambda v: v < 0.8),
        ("0.8–1.2", lambda v: 0.8 <= v < 1.2),
        ("1.2–2.0", lambda v: 1.2 <= v < 2.0),
        (">= 2.0", lambda v: v >= 2.0),
    ], target)

    _bucket_report("VOLUME ACCEL (v5m vs rolling mean)", fwd, lambda r: r["vacc"], [
        ("< 0.7 (fading)", lambda v: v < 0.7),
        ("0.7–1.5", lambda v: 0.7 <= v < 1.5),
        ("1.5–2.5", lambda v: 1.5 <= v < 2.5),
        (">= 2.5 (surge)", lambda v: v >= 2.5),
    ], target)

    _bucket_report("LIQUIDITY VELOCITY", fwd, lambda r: r["lqv"], [
        ("draining < 0", lambda v: v < 0),
        ("flat ≈ 0", lambda v: v == 0),
        ("filling > 0", lambda v: v > 0),
    ], target)

    _bucket_report("WHALE FLOW (bonding-curve hot)", fwd, lambda r: r["whale"], [
        ("not whale-hot", lambda v: v == 0),
        ("whale-hot", lambda v: v == 1),
    ], target)

    qrows = [r for r in fwd if r.get("liq_mc", 0) > 0]
    if qrows:
        _bucket_report("POOL QUALITY (liquidity / market cap)", qrows, lambda r: r["liq_mc"], [
            ("< 2% (thin/rug)", lambda v: v < 0.02),
            ("2–5%", lambda v: 0.02 <= v < 0.05),
            ("5–15%", lambda v: 0.05 <= v < 0.15),
            (">= 15% (deep)", lambda v: v >= 0.15),
        ], target)

    scored = [r for r in fwd if isinstance(r.get("score"), (int, float))]
    if scored:
        _bucket_report("COMPOSITE SCORE (current entry signal)", scored, lambda r: r["score"], [
            ("< 50", lambda v: v < 50),
            ("50–70", lambda v: 50 <= v < 70),
            ("70–80", lambda v: 70 <= v < 80),
            (">= 80 (would trade)", lambda v: v >= 80),
        ], target)

    # Correlations of each feature with forward return
    def corr(xs, ys):
        n = len(xs)
        if n < 5:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0 or syy <= 0:
            return None
        return sxy / math.sqrt(sxx * syy)

    print("\n  PEARSON CORRELATION with forward return  (|r|>0.10 = worth exploring)")
    print("    " + "-" * 50)
    feats = [("momentum_5m", "m5"), ("buy_sell", "bs"), ("vol_accel", "vacc"),
             ("liq_velocity", "lqv"), ("liquidity", "liq"), ("volume_5m", "v5"),
             ("liq/mcap", "liq_mc"), ("liq/fdv", "liq_fdv")]
    ys = [r["fwd"] for r in fwd]
    for name, k in feats:
        c = corr([r.get(k, 0.0) for r in fwd], ys)
        flag = "  ←" if (c is not None and abs(c) > 0.10) else ""
        print(f"    {name:<16}{('  n/a' if c is None else f'{c:+.3f}'):>10}{flag}")

    print("\n" + "=" * 64)
    print("  Read: any bucket whose mean fwd% clearly beats BASELINE is a real")
    print("  edge worth trading. If the COMPOSITE SCORE >=80 bucket does NOT beat")
    print("  baseline, the current entry signal has no edge — that's the headline.")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(description="Offline signal-validation harness")
    ap.add_argument("--log", action="store_true", help="run the logger daemon")
    ap.add_argument("--horizon", type=int, default=30, help="forward-return horizon (minutes)")
    ap.add_argument("--target", type=float, default=3.0, help="win threshold (%% forward return)")
    args = ap.parse_args()
    if args.log:
        run_logger()
    else:
        run_report(args.horizon, args.target)


if __name__ == "__main__":
    main()
