#!/usr/bin/env python3
"""
throughput.py — S85 one-command throughput + deploy-gate readout  (READ-ONLY).

Answers "are we trading enough +EV, and how close is the gate?" in one screen:
  1. ENTRY FUNNEL per bot (signals → rejects → executions → closes) + reject breakdown.
  2. DEPLOY GATE progress (deep_pool n / net / ghost-rate) from prestige_tracker (single source of truth)
     + the binding constraint + a naive ETA from recent deep_pool close cadence.
  3. ADMIT-GUARD verdict summary (how many cells are proven ADMIT/SKIP yet).

S85 finding baked into the framing: throughput is NOT the bottleneck (supply is ample, ~hundreds/day);
the score-0 rejects are the genuinely −EV fresh flood (correctly dropped); the gate's binding constraint
is clean net, which the S80 exit-fix maturation lifts. More *+EV* trades open the gate; −EV volume delays it.

    python3 throughput.py            # last 24h funnel + gate
    python3 throughput.py --hours 72
"""
import json, sqlite3, argparse, re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent   # S88-debug: regime_ev (R) has no ROOT attr

import regime_ev as R
from prestige_tracker import _fleet_deep_pool_stats, MIN_CLOSES, GHOST_MAX
import admit_guard as AG


def _ro(bot):
    return sqlite3.connect(f"file:bots/bot{bot}/trades.db?mode=ro", uri=True)


def funnel(hours: int):
    since = f"-{hours} hour"
    print(f"  ENTRY FUNNEL  (last {hours}h)")
    print(f"  {'bot':4} {'signals':>8} {'rejects':>8} {'score-0':>8} {'tx':>5} {'tx_ok':>6} {'closes':>7}")
    tot = Counter()
    rej_reasons = Counter()
    for b in R.BOTS:
        try:
            c = _ro(b)
        except Exception:
            continue
        q = lambda s: c.execute(s).fetchone()[0]
        w = f"ts > datetime('now','{since}')"
        sig = q(f"SELECT COUNT(*) FROM trades WHERE event='signal' AND {w}")
        rej = q(f"SELECT COUNT(*) FROM trades WHERE event='risk_reject' AND {w}")
        r0  = q(f"SELECT COUNT(*) FROM trades WHERE event='risk_reject' "
                f"AND json_extract(data,'$.reason') LIKE 'strict-gate score 0 %' AND {w}")
        tx  = q(f"SELECT COUNT(*) FROM trades WHERE event='tx_result' AND {w}")
        txok = q(f"SELECT COUNT(*) FROM trades WHERE event='tx_result' "
                 f"AND json_extract(data,'$.confirmed')=1 AND {w}")
        cl  = q(f"SELECT COUNT(*) FROM trades WHERE event='close' AND {w}")
        print(f"  bot{b:1} {sig:>8} {rej:>8} {r0:>8} {tx:>5} {txok:>6} {cl:>7}")
        tot.update(signals=sig, rejects=rej, score0=r0, tx=tx, txok=txok, closes=cl)
        for (d,) in c.execute(f"SELECT data FROM trades WHERE event='risk_reject' AND {w}"):
            try:
                reason = json.loads(d).get("reason", "")
            except Exception:
                continue
            m = re.match(r"strict-gate score (\d+)", reason)
            if m:
                s = int(m.group(1))
                rej_reasons["strict-gate score 0 (unscored fresh flood, −EV)"] += 1 if s == 0 else 0
                if s != 0:
                    rej_reasons["strict-gate near-miss (1-54)"] += 1
            else:
                rej_reasons[reason[:46]] += 1
        c.close()
    print(f"  {'ALL':4} {tot['signals']:>8} {tot['rejects']:>8} {tot['score0']:>8} "
          f"{tot['tx']:>5} {tot['txok']:>6} {tot['closes']:>7}")
    if tot['rejects']:
        print(f"\n  reject breakdown (all bots, {hours}h):")
        for k, v in rej_reasons.most_common(8):
            if v:
                print(f"    {v:5d}  {k}")
    _discovery_efficiency(hours)
    return tot


def _discovery_efficiency(hours: int):
    """S85-C: the INSANE-only bonding-curve gradient scan emits force_fire candidates as score=0.
    Since the force_fire strict-gate exemption was reverted (S84), they're 100% rejected. This quantifies
    the dead weight: distinct score-0 mints surfaced vs how many ever converted to a trade."""
    since = f"-{hours} hour"
    for b in R.BOTS:
        try:
            c = _ro(b)
        except Exception:
            continue
        s0 = c.execute(
            "SELECT COUNT(DISTINCT json_extract(data,'$.mint')) FROM trades "
            f"WHERE event='risk_reject' AND json_extract(data,'$.reason') LIKE 'strict-gate score 0%' "
            f"AND ts > datetime('now','{since}')").fetchone()[0]
        if not s0:
            c.close()
            continue
        conv = c.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT json_extract(data,'$.mint') m FROM trades "
            f"WHERE event='tx_result' AND ts > datetime('now','{since}')) t WHERE t.m IN "
            "(SELECT json_extract(data,'$.mint') FROM trades WHERE event='risk_reject' "
            "AND json_extract(data,'$.reason') LIKE 'strict-gate score 0%')").fetchone()[0]
        c.close()
        # S88-debug: is the gradient scan CURRENTLY gated off (S86)? If so, this {hours}h count is
        # trailing-window residue from before the gate flipped, NOT ongoing waste — say so instead
        # of implying RPC is still being burned.
        _gs_off = False
        try:
            _gs_off = (json.loads((_ROOT / f"bots/bot{b}/gradient_scan.json").read_text()).get("enabled") is False)
        except Exception:
            _gs_off = False
        print(f"\n  ⚠ DISCOVERY DEAD-WEIGHT (bot{b}): {s0} distinct score-0 force_fire mints surfaced/"
              f"{hours}h → {conv} converted to a trade.")
        if conv == 0 and _gs_off:
            print(f"     ✓ gradient scan is CURRENTLY DISABLED (bots/bot{b}/gradient_scan.json, S86) — the above "
                  f"is trailing-window residue, not ongoing RPC waste. Shrink the window (--hours) to confirm.")
        elif conv == 0:
            print(f"     The bonding-curve gradient scan (observer.py:1044, INSANE-only) is pure RPC waste "
                  f"while the force_fire exemption stays reverted. Disable: bots/bot{b}/gradient_scan.json "
                  f"{{\"enabled\":false}}. See S85_FINDINGS.md (C).")


def gate():
    n, net, ghosts, gr = _fleet_deep_pool_stats()
    print("\n  DEPLOY GATE  (deep_pool edge must prove +EV live → arms the SIZE genes)")
    chk = lambda ok: "PASS" if ok else "WAIT"
    print(f"   [{chk(n>=MIN_CLOSES)}]  closes  n ≥ {MIN_CLOSES:<3}        {n}")
    print(f"   [{chk(net>=0)}]  clean net ≥ 0          ◎{net:+.5f}")
    print(f"   [{chk(gr<=GHOST_MAX)}]  ghost-rate ≤ {GHOST_MAX:.0%}        {gr:.0%} ({ghosts}/{n})")
    # binding constraint
    binders = []
    if n < MIN_CLOSES:   binders.append(f"need {MIN_CLOSES-n} more close(s)")
    if net < 0:          binders.append(f"need net +{-net:.4f}◎")
    if gr > GHOST_MAX:   binders.append("ghost-rate too high")
    if binders:
        print(f"   ⏳ DORMANT — binding: {', '.join(binders)}.")
    else:
        print("   🚀 GATE PASS — gene_arm_loop will arm the SIZE genes within ~10min.")
    # naive ETA from recent clean deep_pool close cadence
    ts = []
    for b in R.BOTS:
        try:
            c = _ro(b)
        except Exception:
            continue
        for (d,) in c.execute("SELECT data FROM trades WHERE event='close'"):
            try:
                r = json.loads(d)
            except Exception:
                continue
            if (r.get("play") or r.get("tier")) == "deep_pool" and not R._is_ghost(r) \
               and str(r.get("ts", ""))[:10] >= R.CLEAN_ERA:
                ts.append(str(r.get("ts", "")))
        c.close()
    if len(ts) >= 2 and n < MIN_CLOSES:
        ts.sort()
        span_h = (datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds() / 3600
        rate = len(ts) / span_h if span_h > 0 else 0
        if rate > 0:
            eta_h = (MIN_CLOSES - n) / rate
            # S88-debug: the parenthetical now reflects the ACTUAL net state (it used to always
            # claim "net still must reach ≥0" even when net already PASSed).
            _net_note = (f"clean net already +{net:.4f}◎ ✓ — n is the only binding gate"
                         if net >= 0 else
                         f"net still must reach ≥0 (currently {net:+.4f}◎) — that's the other lever")
            print(f"   ETA to n≥{MIN_CLOSES}: ~{eta_h:.1f}h at {rate:.2f} clean deep_pool closes/h "
                  f"({_net_note}).")


def guard_summary():
    s = AG.cell_stats()
    a = sum(1 for k in s if AG.verdict_for(*k, s).action == "ADMIT")
    sk = sum(1 for k in s if AG.verdict_for(*k, s).action == "SKIP")
    print(f"\n  ADMIT-GUARD: {a} cell(s) proven ADMIT · {sk} proven SKIP · rest NEUTRAL "
          f"(gate plays never skipped). `python3 admit_guard.py --table` for detail.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()
    print("=" * 72)
    print(f"  THROUGHPUT + GATE  ·  {datetime.now(timezone.utc):%Y-%m-%d %H:%MZ}")
    print("=" * 72)
    funnel(a.hours)
    gate()
    guard_summary()
    print("=" * 72)
