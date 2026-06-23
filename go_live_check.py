#!/usr/bin/env python3
"""Go-live readiness checklist (read-only) — does the evidence clear the bar to enable a
LIVE deep_pool/brain_rule filter and/or EV-weighted sizing?

Codifies the operator's most important decision into one verdict so it can't be made on a
hunch. Checks four independent gates and prints PASS/FAIL + the single recommendation. Pure
read — never writes, never trades, never flips a flag. Run it, then YOU decide.

    python3 go_live_check.py
"""
import json

# Single source of truth for the deploy gate — the SAME stats arm_genes uses to actually
# arm the SIZE genes (clean/ghost-excluded net + policy-aware skip-regime cohort). Importing
# it (instead of re-deriving net here) is what keeps go_live_check from drifting back into the
# pre-S80/pre-S86 legacy definition (all-regime net with ghosts folded in) that disagreed with
# the real arming gate. S88-debug.
from prestige_tracker import _fleet_deep_pool_stats, MIN_CLOSES, GHOST_MAX as _PT_GHOST_FRAC

# Live-readiness bar (mirrors strategy_brain's gate + the handoff's filter standard)
LIVE_MIN_N   = 100      # forward obs
LIVE_MIN_EV  = 2.0      # paper EV %, and each chronological half must clear this (stability)
LIVE_MIN_WR  = 55.0     # %
LIVE_MIN_SPAN = 24.0    # data-span hours
GHOST_MAX    = _PT_GHOST_FRAC * 100.0   # max LIVE deep_pool/brain_rule ghost-rate % (=10%, from prestige_tracker)


def _load_brain():
    try:
        j = json.load(open("shared_memory/strategy_brain.json"))
        le = j.get("last_eval", {}) or {}
        return j, le, (le.get("candidates", {}) or {})
    except Exception:
        return {}, {}, {}


def _rule_live_ready(c):
    """A candidate clears the full per-rule live bar (n / EV / WR / ev_lo>0 / both halves)."""
    try:
        return (c.get("n", 0) >= LIVE_MIN_N
                and c.get("ev", -9) >= LIVE_MIN_EV
                and c.get("wr", 0) >= LIVE_MIN_WR
                and c.get("ev_lo", -9) > 0
                and c.get("ev_h1", -9) >= LIVE_MIN_EV
                and c.get("ev_h2", -9) >= LIVE_MIN_EV)
    except Exception:
        return False


def _live_deep_pool():
    """Delegate to the gate's single source of truth (prestige_tracker).

    Returns (tot, gh, net) where `net` is the CLEAN (ghost-excluded) realized net over the
    POLICY-AWARE kept cohort — identical to what arm_genes evaluates. Previously this re-derived
    its own all-regime, ghost-folded net, which made go_live_check contradict the gate that
    actually arms the genes. S88-debug."""
    n, net, ghosts, _gr = _fleet_deep_pool_stats()
    return n, ghosts, net


def main():
    j, le, cands = _load_brain()
    span = le.get("span_hrs", 0.0)
    tot, gh, net = _live_deep_pool()
    ghost_rate = (gh / tot * 100.0) if tot else 0.0

    ready = {n: c for n, c in cands.items() if _rule_live_ready(c)}

    gates = []
    # Gate 1 — data span
    gates.append(("24h data span", span >= LIVE_MIN_SPAN, f"{span:.1f}h / {LIVE_MIN_SPAN:.0f}h"))
    # Gate 2 — a temporally-stable rule clears the bar
    if ready:
        best = max(ready.items(), key=lambda kv: kv[1].get("ev_lo", 0))
        gates.append(("a rule clears live bar", True,
                      f"{best[0]} (n={best[1].get('n')} EV={best[1].get('ev')} "
                      f"ev_lo={best[1].get('ev_lo')} WR={best[1].get('wr')})"))
    else:
        # show the closest candidate by ev_lo among those with decent n
        near = sorted(((n, c) for n, c in cands.items() if c.get("n", 0) >= 30),
                      key=lambda kv: kv[1].get("ev_lo", -9), reverse=True)
        tip = (f"closest: {near[0][0]} ev_lo={near[0][1].get('ev_lo')} n={near[0][1].get('n')}"
               if near else "no candidate ≥ n30")
        gates.append(("a rule clears live bar", False, tip))
    # Gate 3 — live ghost-rate (kept-cohort, matches arm_genes)
    gates.append((f"live ghost-rate ≤ {GHOST_MAX:.0f}%", tot > 0 and ghost_rate <= GHOST_MAX,
                  f"{ghost_rate:.0f}% ({gh}/{tot} closes)"))
    # Gate 4 — clean realized net not bleeding (ghost-excluded, policy-aware cohort)
    gates.append(("live deep_pool clean net ≥ 0", net >= 0, f"{net:+.5f}◎ over {tot} kept closes"))
    # Gate 5 — enough kept-cohort closes to trust the edge (the arm_genes n bar)
    gates.append((f"deep_pool kept closes n ≥ {MIN_CLOSES}", tot >= MIN_CLOSES, f"{tot}"))

    print("═" * 64)
    print("  GO-LIVE READINESS CHECK  (read-only — informs, does not act)")
    print("═" * 64)
    for name, ok, detail in gates:
        print(f"  [{'PASS' if ok else 'WAIT'}]  {name:26} {detail}")
    all_ok = all(ok for _, ok, _ in gates)
    print("─" * 64)
    if all_ok:
        print("  ✅ ALL GATES PASS — operator MAY consider, on the qualifying rule only:")
        print("     • main.py:_LIVE_RULE_ENABLED = True (deep_pool_quality filter) + ./run.sh, OR")
        print("     • enable EV-weighted sizing (ev_sizing.py header) behind its flag.")
        print("     Re-run rule_robustness.py on fresh data first; canary one bot.")
    else:
        blockers = [name for name, ok, _ in gates if not ok]
        print(f"  ⛔ HOLD — {len(blockers)} gate(s) not met: {', '.join(blockers)}.")
        print("     Keep EV-weighted sizing + live_rule filter GATED. Let the loop run.")
    print("═" * 64)


if __name__ == "__main__":
    main()
