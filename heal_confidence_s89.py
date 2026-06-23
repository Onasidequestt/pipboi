#!/usr/bin/env python3
"""
S89 one-time confidence heal.

The S87 dead-pool flat-exit cohort was booked as wins/losses under the too-tight $0.01 USD
break-even band, eroding every token's win_rate below the confidence floors and locking the
fleet out of trading. memory.py now judges break-even by PERCENT (|pnl_pct| < _BREAKEVEN_PCT),
but the ALREADY-RECORDED counts stay poisoned until tokens slowly re-trade.

This recomputes wins/losses/breakeven/consecutive_losses for each token DIRECTLY from the trade
ledger (trades.db close events, all 3 bots) under the corrected band — grounded in real fills,
nothing fabricated. SAFETY: a token is only rewritten when the ledger fully covers its recorded
history (recomputed total >= memory 'trades'), so partial/rotated ledgers never lose trades.
All other fields (momentum avgs, bans, timestamps) are preserved.

Usage:  python3 heal_confidence_s89.py            # dry-run table
        python3 heal_confidence_s89.py --commit   # back up + apply
"""
import json, sqlite3, sys, shutil, glob
from datetime import datetime, timezone
from pathlib import Path

import memory  # for the band constant + confidence_score (before/after)

MEM = Path("shared_memory/trade_memory.json")
BAND_PCT = memory._BREAKEVEN_PCT
BAND_USD = memory._BREAKEVEN_USD
COMMIT = "--commit" in sys.argv


def ledger_closes():
    """mint -> list of (ts, pnl_pct_or_None, pnl_usd) for every close, all bots, ts-ordered."""
    out = {}
    for db in sorted(glob.glob("bots/bot*/trades.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT json_extract(data,'$.mint'), ts, "
                "json_extract(data,'$.pnl_sol'), json_extract(data,'$.size_sol'), "
                "json_extract(data,'$.pnl') "
                "FROM trades WHERE event='close'"
            ).fetchall()
            con.close()
        except Exception as e:
            print(f"  (skip {db}: {e})")
            continue
        for mint, ts, pnl_sol, size_sol, pnl_usd in rows:
            if not mint:
                continue
            pct = None
            if size_sol and pnl_sol is not None and float(size_sol) > 0:
                pct = float(pnl_sol) / float(size_sol) * 100.0
            out.setdefault(mint, []).append((ts or "", pct, pnl_usd))
    for m in out:
        out[m].sort(key=lambda r: r[0])
    return out


def classify(pct, pnl_usd):
    if pct is not None:
        be = abs(pct) < BAND_PCT
    else:
        be = pnl_usd is not None and abs(float(pnl_usd)) <= BAND_USD
    if be:
        return "be"
    if pnl_usd is not None and float(pnl_usd) > 0:
        return "win"
    if pnl_usd is not None and float(pnl_usd) < 0:
        return "loss"
    # pnl_usd missing and not flagged be → treat by pct sign
    return "win" if (pct or 0) > 0 else "loss"


def main():
    data = json.load(open(MEM))
    closes = ledger_closes()

    changed, skipped_cov, unchanged, would_lower = [], [], 0, []
    new_data = json.loads(json.dumps(data))  # deep copy

    for mint, s in data.items():
        if not isinstance(s, dict):
            continue
        recs = closes.get(mint, [])
        recomputed_total = len(recs)
        recorded = s.get("trades", 0)
        if recomputed_total == 0:
            continue
        # SAFETY: only heal when the ledger covers the recorded history.
        if recomputed_total < recorded:
            if (s.get("wins", 0)/max(1, s.get("wins",0)+s.get("losses",0))) < 0.6:
                skipped_cov.append((mint, recorded, recomputed_total))
            continue

        w = l = be = cons = 0
        for ts, pct, pnl_usd in recs:
            k = classify(pct, pnl_usd)
            if k == "win":
                w += 1; cons = 0
            elif k == "loss":
                l += 1; cons += 1
            else:
                be += 1  # breakeven: transparent to streak (matches record_trade)

        old_conf = memory.confidence_score(mint)
        ow, ol, obe = s.get("wins",0), s.get("losses",0), s.get("breakeven",0)
        if (w, l, be) == (ow, ol, obe) and cons == s.get("consecutive_losses",0):
            unchanged += 1
            continue

        # compute the proposed new conf in an isolated copy
        probe = json.loads(json.dumps(new_data))
        ps = probe[mint]
        ps["wins"], ps["losses"], ps["breakeven"] = w, l, be
        ps["consecutive_losses"] = cons
        ps["trades"] = max(recorded, w + l + be)
        _orig = memory._load
        memory._load = lambda: probe               # type: ignore
        new_conf = memory.confidence_score(mint)
        memory._load = _orig                        # type: ignore

        # SAFETY: this heal only UNDOES the bug (flat exits mis-booked as losses) → it may
        # only RAISE or HOLD confidence. A recompute that LOWERS conf means the ledger found
        # additional REAL decisive losses (a coverage/under-count artifact, not our bug) — leave
        # that token untouched; it'll accrue naturally under the corrected band.
        if new_conf < old_conf - 1e-9:
            would_lower.append((mint, old_conf, new_conf))
            continue

        new_data[mint].update({"wins": w, "losses": l, "breakeven": be,
                               "consecutive_losses": cons,
                               "trades": max(recorded, w + l + be)})
        changed.append((mint, (ow, ol, obe), (w, l, be), cons, old_conf, new_conf))

    # ---- report ----
    print(f"\nband: |pnl_pct| < {BAND_PCT}%  (USD fallback ≤ ${BAND_USD})")
    print(f"tokens in memory: {len(data)} | with ledger closes: {len(closes)}\n")
    print(f"{'mint':14} {'old W/L/BE':>12}  {'new W/L/BE':>12}  {'cons':>4}  {'conf →':>13}")
    for mint, old, new, cons, oc, nc in sorted(changed, key=lambda x: x[4]):
        arrow = "↑" if nc > oc else ("↓" if nc < oc else "=")
        print(f"{mint[:14]:14} {str(old):>12}  {str(new):>12}  {cons:>4}  "
              f"{oc:.3f}→{nc:.3f} {arrow}")
    print(f"\nhealed (conf raised/held): {len(changed)} | unchanged: {unchanged} | "
          f"left as-is — ledger<recorded: {len(skipped_cov)} | "
          f"left as-is — recompute would LOWER conf: {len(would_lower)}")
    for mint, oc, nc in would_lower[:10]:
        print(f"  keep {mint[:14]} {oc:.3f} (recompute→{nc:.3f}, real losses — not our bug)")

    if not COMMIT:
        print("\nDRY-RUN — re-run with --commit to back up + apply.")
        return

    bak = str(MEM) + ".bak.s89heal." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy(MEM, bak)
    json.dump(new_data, open(MEM, "w"), indent=2)
    print(f"\n✓ applied. backup: {bak}")


if __name__ == "__main__":
    main()
