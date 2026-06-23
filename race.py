#!/usr/bin/env python3
"""race.py — Vault-Bot PRESTIGE RACE leaderboard (read-only, never acts).

GENERATION 1 of the gene pool. Three bots, ONE shared deep_pool edge, and the
SIZE gene is the only varying variable:

    Bot 1  — aggressive EV-size   (gene: ev_sizing.json enabled, full)
    Bot 2  — moderate  EV-size    (gene: ev_sizing.json enabled, scale<1)
    Bot 3  — flat C² control      (gene: no ev_sizing.json)

Fitness = first to ◎2.0 (prestige). However it gets there, the winner's SIZE
gene seeds the next generation (the losers adopt it; bots 4-6 launch with it).

Ranks by TOTAL capital = liquid (sol_balance) + in-flight (sol_in_trades), so
opening a position never looks like a loss. Pure balance race — no projections
you can fool yourself with; the ETA is a naive thermometer off recent growth.

The race only MEANS something once go_live_check flips +EV; until then all three
sit at the same ~1.5% C² size (edge-proving warm-up) and the board barely moves.

    python3 race.py            # one-shot leaderboard
    python3 race.py --watch    # refresh every 30s
"""
import json, sys, time, datetime
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
GOAL  = 2.0
BOTS  = (1, 2, 3)
START = ROOT / "race_start.json"


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _ts(x):
    try:
        return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def bot_state(b):
    s = _load(ROOT / f"bots/bot{b}/status.json", {}) or {}
    liq  = float(s.get("sol_balance", 0) or 0)
    intr = float(s.get("sol_in_trades", 0) or 0)
    return {
        "bot": b,
        "liquid": liq,
        "in_trades": intr,
        "total": liq + intr,
        "open": len(s.get("open_positions", {}) or {}),
        "mode": f"{s.get('mode','?')}/{s.get('marketcap','?')}",
        "updated": str(s.get("last_update", ""))[:19],
    }


def gene(b):
    f = _load(ROOT / f"bots/bot{b}/ev_sizing.json")
    if not f or not f.get("enabled"):
        return "C² flat (ctrl)"
    sc = f.get("scale")
    return f"EV-size ×{sc}" if sc else "EV-size (full)"


def trailing_rate(b, since=None, hours=6.0):
    """◎/hour from balance_history. If `since` (epoch) is given, only counts rows at
    or after it — so the refund jump at the start line is excluded and the rate
    reflects the bot's actual in-race trading. Returns None until ≥2 in-race rows."""
    try:
        lines = (ROOT / f"bots/bot{b}/balance_history.jsonl").read_text().splitlines()[-600:]
    except Exception:
        return None
    rows = []
    for ln in lines:
        try:
            d = json.loads(ln)
            t = _ts(d.get("ts"))
            v = float(d.get("sol_balance", 0) or 0)
            if t and (since is None or t >= since):
                rows.append((t, v))
        except Exception:
            pass
    if len(rows) < 2:
        return None
    now_t = rows[-1][0]
    if since is not None:
        base = rows[0]
    else:
        cutoff = now_t - hours * 3600
        base = next(((t, v) for t, v in rows if t >= cutoff), rows[0])
    dt = (now_t - base[0]) / 3600.0
    if dt < 0.5:          # too little race-time → any rate is dt→0 noise
        return None
    return (rows[-1][1] - base[1]) / dt


def ensure_start(states):
    """Capture the equal-start line on first run; never overwrite it."""
    if START.exists():
        return _load(START, {})
    mark = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "goal": GOAL,
        "start_total": {str(s["bot"]): round(s["total"], 6) for s in states},
    }
    START.write_text(json.dumps(mark, indent=2))
    print(f"[race] start line captured → {START.name}\n")
    return mark


def bar(frac, width=22):
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def render():
    states = [bot_state(b) for b in BOTS]
    mark = ensure_start(states)
    start_tot = (mark or {}).get("start_total", {})
    started = (mark or {}).get("started_at", "")
    t0 = _ts(started)

    for s in states:
        s["gene"]  = gene(s["bot"])
        s["rate"]  = trailing_rate(s["bot"], since=t0)
        st0        = float(start_tot.get(str(s["bot"]), s["total"]))
        s["start"] = st0
        s["delta"] = s["total"] - st0
        s["pct"]   = (s["delta"] / st0 * 100.0) if st0 else 0.0
        rate = s["rate"]
        remain = GOAL - s["total"]
        s["eta_h"] = (remain / rate) if (rate and rate > 0 and remain > 0) else None

    states.sort(key=lambda x: x["total"], reverse=True)

    el = ""
    t0 = _ts(started)
    if t0:
        hrs = (time.time() - t0) / 3600.0
        el = f"  ·  elapsed {hrs:.1f}h"
    print("═" * 78)
    print(f"  ⚡ VAULT-BOT PRESTIGE RACE — GEN 1   goal ◎{GOAL:.1f}{el}")
    print(f"     fitness = first to ◎{GOAL:.1f}; winner's SIZE gene seeds the next generation")
    print("═" * 78)
    print(f"  {'#':<2}{'BOT':<7}{'GENE':<17}{'TOTAL◎':>9}{'Δstart':>9}{'%':>7}{'◎/h':>9}{'ETA':>8}  to ◎2.0")
    print("  " + "─" * 74)
    for i, s in enumerate(states, 1):
        eta = f"{s['eta_h']:.0f}h" if s["eta_h"] else "—"
        rate = f"{s['rate']:+.4f}" if s["rate"] is not None else "—"
        prog = s["total"] / GOAL
        flag = " ◀ LEAD" if i == 1 else ""
        print(f"  {i:<2}Bot{s['bot']:<4}{s['gene']:<17}{s['total']:>9.4f}"
              f"{s['delta']:>+9.4f}{s['pct']:>+6.1f}%{rate:>9}{eta:>8}  "
              f"[{bar(prog)}] {prog*100:4.1f}%{flag}")
    print("  " + "─" * 74)

    # ── S80: GENE ATTRIBUTION — is a lead the GENE, or the confound? ───────────────
    # The SIZE gene is NOT cleanly isolated: each bot runs a different MODE (insane/
    # wild/stoic) on its own wallet, discovering DIFFERENT tokens at different rates.
    # So a balance lead can be gene-driven, mode/volume-driven, or pure tail-trade luck.
    # Metric is SOL-based (net ◎, ◎/trade) — the race's actual currency, computable on all
    # history. (Return-% needs per-trade size_sol, only recorded from S80 on — see the gate.)
    # net ◎/trade × trades-day = the real per-day pull; a flat/−ve leader's lead is noise.
    try:
        from prestige_tracker import _closes, _is_ghost
        print(f"  {'ATTRIBUTION':<12}{'MODE':<13}{'cleanN':>7}{'net◎':>10}{'◎/trade':>10}{'tr/day':>8}  driver")
        for s in states:
            rows = [r for r in _closes(s["bot"]) if not _is_ghost(r)]
            n = len(rows)
            net = sum((r.get("pnl_sol", 0.0) or 0.0) for r in rows)
            per = (net / n) if n else 0.0
            ts = []
            for r in rows:
                try: ts.append(_ts(r.get("ts")))
                except Exception: pass
            ts = [t for t in ts if t]
            span_d = ((max(ts) - min(ts)) / 86400) if len(ts) > 1 else 0.5
            tpd = n / max(span_d, 0.5)
            tag = ("+edge" if per > 1e-4 else "−edge" if per < -1e-4 else "flat")
            print(f"  {'Bot'+str(s['bot']):<12}{s['mode']:<13}{n:>7}"
                  f"{net:>+10.4f}{per:>+10.5f}{tpd:>8.1f}  {tag} ({per*tpd:+.4f}◎/d)")
        print("  ⚠ gene NOT isolated: mode+token-luck confound it until all 3 trade identical")
        print("    signals — read leads against net◎/trade, not raw balance (one tail = noise).")
        print("  " + "─" * 74)
    except Exception:
        pass

    print("  note: TOTAL = liquid + in-trades. ETA is naive trailing-growth extrapolation.")
    print("  ⚠ race is REAL only once go_live_check flips +EV — until then sizes are equal (warm-up).")
    print("═" * 78)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        try:
            while True:
                print("\033[2J\033[H", end="")
                render()
                time.sleep(30)
        except KeyboardInterrupt:
            pass
    else:
        render()
