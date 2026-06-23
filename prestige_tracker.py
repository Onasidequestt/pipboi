#!/usr/bin/env python3
"""PRESTIGE TRACKER — read-only projection + the EV-sizing DEPLOY gate (S70).

Answers the only question that matters for the PATH TO PRESTIGE: are the bots on track
to ◎2.0, and is the deep_pool edge PROVEN +EV live yet (the trigger to deploy EV-weighted
sizing — the months→weeks lever)?

  python3 prestige_tracker.py               # report only (never writes)
  python3 prestige_tracker.py --deploy-if-ready   # if the edge is proven live, enable
                                                  # EV-sizing on the CANARY bot (one file),
                                                  # else report "waiting" and touch nothing

DEPLOY GATE (for EV-sizing specifically — NOT the 24h FILTER clock): the lever sizes UP the
deep_pool edge, so it deploys only once that edge is proven +EV on real SOL:
    live deep_pool net ≥ 0   AND   ghost-rate ≤ 10%   AND   n ≥ MIN_CLOSES
Canary = enable on ONE bot first (bots/botN/ev_sizing.json, hot-reload, reversible), expand
only after the canary itself shows +EV live. Sizing up an UNPROVEN edge just loses faster —
that gate is the whole point.
"""
import json, sqlite3, time, sys, math
from pathlib import Path

ROOT = Path(__file__).parent
GOAL_SOL = 2.0
BOTS = (1, 2, 3)
CANARY_BOT = 1            # INSANE — trades the most deep_pool volume → fastest canary feedback
MIN_CLOSES = 15          # deep_pool closes needed before trusting the live edge
GHOST_MAX  = 0.10        # ≤10% ghost-rate
_DP_PLAYS  = {"deep_pool", "brain_rule"}

# ── S86: POLICY-AWARE gate (operator decision S86). The gate validates "is the edge we DEPLOY
# +EV?" — so it must measure the cohort the CURRENT admission actually trades, not closes from a
# discontinued policy. S85 made observer skip normal/dead; those closes describe an edge we no
# longer trade, yet they dragged the legacy cumulative net (the −0.0257 was ENTIRELY stale normal
# drag — the kept euph/aggr/sniper cohort was already +EV). So a close whose regime is in the
# current skip-set is EXCLUDED from n / net / ghost-rate. Same n≥15, net≥0, ghost≤10% bars — this
# is NOT a force (it still needs 15 GOOD closes; today kept n=4 → gate stays shut), it's a standing
# policy-conditional rule: re-add normal to observer's skip-set → those closes count again, auto-
# synced via the import below. Closes with no regime tag are INCLUDED (can't prove skipped).
try:
    from observer import _DEEP_POOL_SKIP_REGIMES as _GATE_SKIP_REGIMES   # auto-sync to live policy
except Exception:
    _GATE_SKIP_REGIMES = {"dead", "normal"}   # fallback mirror (keep in sync w/ observer S85)


def _bot_balance(b):
    try:
        s = json.loads((ROOT / f"bots/bot{b}/status.json").read_text())
        v = s.get("sol_balance")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    except Exception:
        pass
    # fallback: last non-zero balance_history row
    try:
        last = 0.0
        for line in (ROOT / f"bots/bot{b}/balance_history.jsonl").read_text().splitlines():
            try:
                v = json.loads(line).get("sol_balance", 0.0) or 0.0
                if v > 0:
                    last = v
            except Exception:
                pass
        return last
    except Exception:
        return 0.0


def _closes(b):
    """All close events for bot b as list of dicts."""
    out = []
    try:
        c = sqlite3.connect(f"{ROOT}/bots/bot{b}/trades.db")
        for (d,) in c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid"):
            try:
                out.append(json.loads(d))
            except Exception:
                pass
        c.close()
    except Exception:
        pass
    return out


def _is_ghost(r):
    if r.get("ghost"):
        return True
    # backstop: pnl≈0 fraction but big SOL loss = unsellable (the S57 signature)
    return abs(r.get("pnl", 0.0)) < 1e-9 and r.get("pnl_sol", 0.0) < -0.02


def _fleet_deep_pool_stats():
    """Live realized deep_pool/brain_rule stats across the fleet.

    S80: `net` is the CLEAN (ghost-excluded) realized net — the "is the edge +EV?" question.
    Ghost losses are bounded SEPARATELY by the ghost-RATE gate (gr ≤ GHOST_MAX); folding them
    into net too let a single in-tolerance legacy ghost (the −0.0285 S74 artifact, dated inside
    the race) BOTH pass the rate test AND veto the gate via net — double-counting one failure
    from a mode later fixes addressed. n still counts ALL closes (volume threshold) and gr is
    still ghosts/all, so the gate is no easier to pass on volume or ghost tolerance — only the
    +EV test is now measured on the clean edge, matching edge_report.py's definition."""
    n = ghosts = 0
    net = 0.0
    for b in BOTS:
        for r in _closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play in _DP_PLAYS:
                # S99: dust-shadow closes are an EVIDENCE probe at ~◎0.01 — they feed the SEPARATE
                # size-normalized shadow gate (dust_gate.py), NEVER the live arming gate. Their
                # absolute pnl_sol (±0.0005) would corrupt this gate's absolute-SOL net/ghost math
                # (a dust ghost is −0.01, ABOVE the −0.02 _is_ghost floor → it would evade the
                # ghost-rate gate AND its near-zero net would let the cohort pass net≥0 on noise).
                # Excluding them keeps EV-sizing arming on real-capital evidence only → THE ONE
                # OPERATOR RULE intact. (Dust regimes are dead/normal, so they're already dropped
                # below — this is belt-and-suspenders against any skip-set change.)
                if r.get("dust_shadow"):
                    continue
                # S86: policy-aware — drop closes from a regime the current admission no longer
                # trades (stale cohort). A missing/empty regime tag is kept (can't prove skipped).
                _reg = r.get("regime")
                # S98-reconcile: the tight `normal` deep_pool slice (normal_slice=True) is a
                # DELIBERATELY-traded cohort — it MUST count toward the gate it was built to feed.
                # Only the STALE skip-regime bulk (no slice flag) is dropped. Keeps S85's exclusion
                # of the discontinued normal/dead bulk while letting the new slice advance n/net/ghost.
                if _reg in _GATE_SKIP_REGIMES and not r.get("normal_slice"):
                    continue
                n += 1
                if _is_ghost(r):
                    ghosts += 1
                else:
                    net += r.get("pnl_sol", 0.0) or 0.0   # clean net only
    gr = (ghosts / n) if n else 0.0
    return n, net, ghosts, gr


def _fleet_deep_pool_concentration():
    """S105-audit: how single-token is the gate cohort? Returns (distinct, top_mint, top_n, n,
    top_net◎). A deploy gate that reads +EV off ONE lucky token is not a proven edge — this
    surfaces it as a READ-ONLY diagnostic (it does NOT gate arming here; ride_ab's concentration
    check (d) is what holds the automatic arm). Mirrors _fleet_deep_pool_stats' cohort filter."""
    from collections import Counter, defaultdict
    cnt = Counter()
    per_mint_net = defaultdict(float)
    for b in BOTS:
        for r in _closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play not in _DP_PLAYS:
                continue
            if r.get("dust_shadow"):
                continue
            if r.get("regime") in _GATE_SKIP_REGIMES and not r.get("normal_slice"):
                continue
            mint = r.get("mint") or "(unknown)"
            cnt[mint] += 1
            if not _is_ghost(r):                       # clean net, same as the gate
                per_mint_net[mint] += r.get("pnl_sol", 0.0) or 0.0
    n = sum(cnt.values())
    distinct = len(cnt)
    top_mint, top_n = cnt.most_common(1)[0] if cnt else (None, 0)
    top_net = per_mint_net.get(top_mint, 0.0)
    return distinct, top_mint, top_n, n, top_net


def _bot_clean_perf(b):
    """Clean-era (ghost-excluded) realized EV-per-trade and trades/day for bot b."""
    rows = [r for r in _closes(b) if not _is_ghost(r)]
    if not rows:
        return 0, 0.0, 0.0
    # realized return per trade = pnl_sol / size_sol (fall back to pnl% if size missing)
    rets, ts = [], []
    for r in rows:
        sz = r.get("size_sol") or 0.0   # S80: recorded on closes going forward; pre-S80 rows
        if sz > 0:                       # lack it (return-% needs the deployed-capital denom).
            rets.append((r.get("pnl_sol", 0.0) or 0.0) / sz)
        t = r.get("ts", "")
        try:
            ts.append(time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:
            pass
    ev = (sum(rets) / len(rets)) if rets else 0.0
    span_d = ((max(ts) - min(ts)) / 86400) if len(ts) > 1 else 1.0
    tpd = len(rows) / max(span_d, 0.5)
    return len(rows), ev, tpd


def _days_to_goal(bal, ev_per_trade, size_frac, tpd):
    """Compounding days from bal→GOAL at (ev × size) per trade, tpd trades/day."""
    if bal <= 0 or bal >= GOAL_SOL or tpd <= 0:
        return None
    g = math.log(1 + ev_per_trade * size_frac)
    if g <= 0:
        return None                       # −EV → never (by compounding)
    return (math.log(GOAL_SOL / bal) / g) / tpd


def _ev_filling_frac():
    """Effective wallet fraction an EV-sized deep_pool_filling entry targets (normal mkt)."""
    try:
        import ev_sizing
        fr = ev_sizing.ev_size_fraction_of_cap("deep_pool_filling")
        if fr is None:
            fr = ev_sizing.ev_size_fraction_of_cap("deep_pool") or 0.2
        return fr * 0.15 * 0.55           # × cap (15%) × normal-market vol_scale
    except Exception:
        return 0.08


def report(deploy_if_ready=False):
    print("=" * 66)
    print("  PRESTIGE TRACKER  ·  goal ◎%.1f" % GOAL_SOL)
    print("=" * 66)
    total = 0.0
    bot_rows = []
    for b in BOTS:
        bal = _bot_balance(b)
        total += bal
        n, ev, tpd = _bot_clean_perf(b)
        bot_rows.append((b, bal, n, ev, tpd))
        pct = 100 * bal / GOAL_SOL
        ev_s = f"{ev:+.3%}" if abs(ev) > 1e-9 else "n/a (need size_sol on closes)"
        print(f"  Bot{b}: ◎{bal:.4f}  ({pct:4.0f}% of goal)  | clean: n={n} EV/trade={ev_s} ~{tpd:.0f} trades/day")
    print(f"  FLEET TOTAL: ◎{total:.4f}   (nearest bot to ◎2.0 leads the race)")

    # ── deep_pool live readiness (the deploy gate) ──
    n, net, ghosts, gr = _fleet_deep_pool_stats()
    print("\n  ── DEPLOY GATE — is the deep_pool edge PROVEN +EV live? ──")
    g_net   = net >= 0
    g_ghost = gr <= GHOST_MAX
    g_n     = n >= MIN_CLOSES
    print(f"   [{'PASS' if g_n else 'WAIT'}]  closes n≥{MIN_CLOSES}        {n}")
    print(f"   [{'PASS' if g_net else 'WAIT'}]  live net ≥ 0          ◎{net:+.4f}")
    print(f"   [{'PASS' if g_ghost else 'WAIT'}]  ghost-rate ≤ {GHOST_MAX:.0%}      {gr:.0%} ({ghosts}/{n})")
    ready = g_net and g_ghost and g_n

    # ── concentration diagnostic (S105-audit): is a "green" gate actually one lucky token? ──
    dist, tmint, tn, cn, tnet = _fleet_deep_pool_concentration()
    if cn:
        share = tn / cn
        tm = (tmint or "—")[:10]
        conc_flag = "⚠ single-token" if (share > 0.40 or dist < 5) else "ok"
        print(f"   [diag]  concentration         {dist} distinct mints · top {tm} {share:.0%} "
              f"(net ◎{tnet:+.4f})  → {conc_flag}")
        if ready and (share > 0.40 or dist < 5):
            print(f"           ↳ NOTE: gate net rests heavily on one token — ride_ab's concentration")
            print(f"             check (d) holds the automatic arm until the cohort diversifies.")

    # ── days-to-prestige projection: current sizing vs EV-sizing ──
    print("\n  ── DAYS TO ◎2.0 (leader = Bot with most SOL) ──")
    ev_frac = _ev_filling_frac()
    for b, bal, ncl, ev, tpd in bot_rows:
        if bal <= 0 or bal >= GOAL_SOL:
            continue
        # use the proven deep_pool paper edge as the realized-EV proxy once the gate clears;
        # below the gate, show the bot's actual clean EV (often ~0/neg → "n/a (−EV)")
        ev_use = max(ev, 0.0001) if ev > 0 else ev
        cur = _days_to_goal(bal, ev_use, 0.015, tpd or 12)            # ~1.5% C² size today
        evd = _days_to_goal(bal, 0.05, ev_frac, tpd or 12)           # +5% proven edge, EV-sized
        cur_s = f"{cur:.0f}d" if cur else "n/a (−EV)"
        evd_s = f"{evd:.0f}d" if evd else "n/a"
        print(f"   Bot{b}: current-size → {cur_s:>9}   |   EV-sized(~{ev_frac:.0%} wallet) → {evd_s:>6}")
    print("   (EV-sized column = the lever, unlocked when the DEPLOY GATE is all PASS)")

    # ── verdict / deploy ──
    print("\n  " + "─" * 62)
    if ready:
        print(f"  ✅ DEPLOY GATE ALL PASS — deep_pool is +EV live (n={n}, net ◎{net:+.4f}).")
        if deploy_if_ready:
            fp = ROOT / f"bots/bot{CANARY_BOT}/ev_sizing.json"
            fp.write_text(json.dumps({"enabled": True, "deployed_ts": time.time(),
                                      "by": "prestige_tracker auto-deploy"}))
            print(f"  🚀 DEPLOYED EV-sizing on CANARY Bot{CANARY_BOT} → {fp.name} (hot-reload ≤30s, no restart).")
            print(f"     Watch Bot{CANARY_BOT} for ~10–20 closes; if it holds +EV, enable the others.")
            print(f"     Revert: rm {fp}")
        else:
            print(f"  → run with --deploy-if-ready to enable EV-sizing on canary Bot{CANARY_BOT}.")
    else:
        waiting = [name for ok, name in ((g_n, f"n≥{MIN_CLOSES}"), (g_net, "net≥0"), (g_ghost, "ghost≤10%")) if not ok]
        print(f"  ⏳ WAITING on: {', '.join(waiting)}. EV-sizing stays OFF (sizing up an")
        print(f"     unproven edge loses faster). Re-check as new deep_pool closes land.")
    print("=" * 66)
    return ready


if __name__ == "__main__":
    deploy = "--deploy-if-ready" in sys.argv
    r = report(deploy_if_ready=deploy)
    sys.exit(0 if r else 1)
