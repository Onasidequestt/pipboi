#!/usr/bin/env python3
"""p1_watch.py — is the S80 exit-leak fix actually maturing deep_pool winners? (read-only)

S78/S79 diagnosed the paper(+14%)→live(−EV) leak to the EXIT side: the dead-pool volume
guard was clipping quiet-but-HEALTHY deep pools at ~3–5min holds, before the edge's 30-min
horizon. S80 fixed it (the guard now needs vol-dead AND a FROZEN price feed). This tool
answers the only question that matters for the gate: did the fix work — are winners now
allowed to mature instead of being amputated near-flat?

It splits deep_pool closes at the S80 RESTART (auto-detected from the newest logs/run_*.log
epoch — the guard change is inert until then) and compares the two eras. NEVER writes.

    python3 p1_watch.py            # the verdict
    python3 p1_watch.py --since 1780882753   # override the S80 cutoff epoch
"""
import json, sqlite3, sys, time, datetime
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
BOTS  = (1, 2, 3)
MATURE_MIN = 20.0          # a hold ≥ this is "given its window" (edge horizon is 30min)


def _s80_cutoff() -> float:
    """Epoch of the S80 restart = newest logs/run_<epoch>.log (the guard is inert before it)."""
    best = 0
    for p in (ROOT / "logs").glob("run_*.log"):
        try:
            best = max(best, int(p.stem.split("_")[1]))
        except Exception:
            pass
    return float(best) if best else time.time() - 3600


def _ts_epoch(ts: str):
    # trades.db ts is UTC but stored tz-NAIVE (no Z/offset). fromisoformat() then yields a
    # naive datetime that .timestamp() reads as LOCAL time — the exact "do NOT filter by local
    # wall-clock" trap the handoff warns about. Force UTC on naive datetimes.
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _deep_closes():
    """Every deep_pool/brain_rule close across the fleet, as dicts (+derived fields)."""
    out = []
    for b in BOTS:
        try:
            c = sqlite3.connect(f"{ROOT}/bots/bot{b}/trades.db")
            for (d,) in c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid"):
                try:
                    r = json.loads(d)
                except Exception:
                    continue
                if (r.get("play") or r.get("tier") or r.get("insane_tier")) in ("deep_pool", "brain_rule"):
                    r["_bot"] = b
                    r["_ep"]  = _ts_epoch(r.get("ts"))
                    out.append(r)
            c.close()
        except Exception:
            pass
    return out


def _is_ghost(r):
    if r.get("ghost") or str(r.get("exit_reason", "")).startswith("ghost"):
        return True
    return abs(r.get("pnl", 0.0)) < 1e-9 and (r.get("pnl_sol", 0.0) or 0.0) < -0.02


def _category(reason: str) -> str:
    """Bucket an exit_reason into a coarse class for the breakdown."""
    s = (reason or "").lower()
    if not s:                       return "other/legacy"
    if s.startswith("ghost"):       return "ghost"
    if "dead pool" in s:            return "dead-pool guard"
    if "stop loss" in s:            return "stop loss"
    if "lp drain" in s:             return "lp drain"
    if "trail" in s or "tp" in s or "take" in s or "smart" in s: return "trail/TP (winner)"
    if "max hold" in s or "flat" in s or "stall" in s:           return "maturity/stall"
    return "other"


def _era_stats(rows):
    clean = [r for r in rows if not _is_ghost(r)]
    holds = [r.get("hold_min") for r in clean if isinstance(r.get("hold_min"), (int, float))]
    deadn = sum(1 for r in clean if "dead pool" in str(r.get("exit_reason", "")).lower())
    matur = sum(1 for r in clean if isinstance(r.get("hold_min"), (int, float)) and r["hold_min"] >= MATURE_MIN)
    net   = sum((r.get("pnl_sol", 0.0) or 0.0) for r in clean)
    med   = (sorted(holds)[len(holds) // 2] if holds else None)
    return dict(n=len(clean), med=med, deadn=deadn, matur=matur, net=net,
                deadpct=(deadn / len(clean) * 100 if clean else 0.0))


def main(cutoff):
    rows = _deep_closes()
    pre  = [r for r in rows if r["_ep"] and r["_ep"] <  cutoff]
    post = [r for r in rows if r["_ep"] and r["_ep"] >= cutoff]
    a, b = _era_stats(pre), _era_stats(post)

    when = datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    print("=" * 66)
    print("  P1 EXIT-FIX WATCH — are deep_pool winners maturing now? (read-only)")
    print("=" * 66)
    print(f"  S80 restart cutoff: {when}   (closes at/after = NEW frozen-feed guard)")
    print("  " + "─" * 62)
    print(f"  {'metric':<22}{'pre-S80':>13}{'post-S80':>13}")
    def fmt(x, s=""): return "—" if x is None else f"{x}{s}"
    print(f"  {'clean closes':<22}{a['n']:>13}{b['n']:>13}")
    print(f"  {'median hold (min)':<22}{fmt(a['med']):>13}{fmt(b['med']):>13}")
    print(f"  {'dead-pool exits':<22}{a['deadn']:>6} ({a['deadpct']:>3.0f}%){b['deadn']:>6} ({b['deadpct']:>3.0f}%)")
    print(f"  {'matured ≥20min':<22}{a['matur']:>13}{b['matur']:>13}")
    print(f"  {'clean net ◎':<22}{a['net']:>+13.4f}{b['net']:>+13.4f}")
    print("  " + "─" * 62)

    # Per-exit breakdown of the NEW era (where the winners should be appearing)
    if post:
        cats = {}
        for r in post:
            if _is_ghost(r):
                continue
            c = _category(r.get("exit_reason"))
            d = cats.setdefault(c, [0, 0.0, []])
            d[0] += 1
            d[1] += (r.get("pnl_sol", 0.0) or 0.0)
            if isinstance(r.get("hold_min"), (int, float)):
                d[2].append(r["hold_min"])
        print("  POST-S80 exits by kind (n · net◎ · avg hold):")
        for c in sorted(cats, key=lambda k: -cats[k][0]):
            n, net, hs = cats[c]
            ah = f"{sum(hs)/len(hs):.0f}min" if hs else "—"
            print(f"    {c:<20}{n:>3} · {net:>+8.4f} · {ah:>6}")
        print("  " + "─" * 62)

    # Verdict
    print("  VERDICT:")
    if b["n"] == 0:
        print("    ⏳ TOO EARLY — no deep_pool closes since the S80 restart yet.")
        print("       Re-run after a handful of deep_pool entries open & close.")
    else:
        signals = []
        if a["med"] is not None and b["med"] is not None:
            signals.append(("holds rising", b["med"] > a["med"]))
        signals.append(("dead-pool% falling", b["deadpct"] < a["deadpct"]))
        signals.append(("winners maturing (≥20min)", b["matur"] > 0))
        signals.append(("clean net improving/trade", (b["net"]/max(b["n"],1)) > (a["net"]/max(a["n"],1))))
        good = sum(1 for _, ok in signals if ok)
        for name, ok in signals:
            print(f"    [{'✓' if ok else '·'}] {name}")
        if good >= 3:
            print("    ✅ P1 WORKING — the fix is letting winners develop. Watch the gate flip.")
        elif good >= 1 and b["n"] < 5:
            print(f"    ⏳ EARLY-POSITIVE — {good}/4 signals, only {b['n']} post-S80 closes. Need more.")
        else:
            print("    ⚠ NOT CONFIRMED — exits still near-flat/short. If it holds with more n,")
            print("       the leak is ENTRY/timing, not the exit — revisit observer admission.")
    # Live gate echo for context
    try:
        from prestige_tracker import _fleet_deep_pool_stats, MIN_CLOSES
        n, net, gh, gr = _fleet_deep_pool_stats()
        print("  " + "─" * 62)
        print(f"  GATE NOW: n={n}/{MIN_CLOSES} · clean net ◎{net:+.4f} · ghost {gr:.0%} "
              f"→ {'ARMS' if (n>=MIN_CLOSES and net>=0 and gr<=0.10) else 'DORMANT'}")
    except Exception:
        pass
    print("=" * 66)


if __name__ == "__main__":
    cut = None
    if "--since" in sys.argv:
        try: cut = float(sys.argv[sys.argv.index("--since") + 1])
        except Exception: cut = None
    main(cut if cut else _s80_cutoff())
