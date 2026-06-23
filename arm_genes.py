#!/usr/bin/env python3
"""arm_genes.py — gate-gated activator for the PRESTIGE RACE size genes (GEN 1).

The race differentiates the three bots by ONE variable — the SIZE gene — but sizing UP
the deep_pool edge is forbidden until that edge is PROVEN +EV live. This script is the
"armed but dormant" mechanism: it watches the same DEPLOY GATE prestige_tracker defines
(live deep_pool net ≥ 0  AND  ghost-rate ≤ 10%  AND  n ≥ MIN_CLOSES) and the instant it
passes, writes the three gene files at their experiment levels — no sooner.

    Bot 1  → ev_sizing.json {"enabled": true}              full EV-size (aggressive)
    Bot 2  → ev_sizing.json {"enabled": true, "scale":0.5} moderate EV-size (half)
    Bot 3  → (no file)                                     C² flat control

Until the gate passes it touches NOTHING — zero SOL sized into the unproven edge.
Reversible at any time: rm bots/bot*/ev_sizing.json.

    python3 arm_genes.py            # report gate status + what WOULD activate (no writes)
    python3 arm_genes.py --arm      # activate the genes IF (and only if) the gate passes
                                    # (the durable loop runs this; safe to run anytime)
"""
import json, sys, time
from pathlib import Path

# Reuse the project's canonical deploy gate — single source of truth, no reimplementation.
from prestige_tracker import _fleet_deep_pool_stats, MIN_CLOSES, GHOST_MAX

# S98: SEQUENCING GUARD. Sizing sizes UP the deep_pool edge under whatever EXIT policy is
# live — so it must not arm until the RIDE exit (SL/trail, no fixed-TP bank) is PROVEN the
# right exit on a real LIVE n, not just the counterfactual. ride_ab.ride_exit_proven() is
# fail-closed (any error ⇒ not proven). This can only DELAY arming, never force it — it is
# strictly conservative, in the spirit of THE ONE OPERATOR RULE. Remove this import + the
# `exit_proven` term in main() to revert to the pre-S98 net/ghost/n-only gate.
try:
    from ride_ab import ride_exit_proven
except Exception:
    def ride_exit_proven():
        return False, {"binding": "ride_ab import failed — fail-closed (sizing stays off)"}

ROOT = Path(__file__).resolve().parent

# The experiment genome: the ONLY variable that differs across the three bots.
# Bot 3 is deliberately absent → no ev_sizing.json → C² flat control.
GENES = {
    1: {"enabled": True},               # aggressive — full ev_lo-driven fraction
    2: {"enabled": True, "scale": 0.5}, # moderate   — half the EV target
}
GENE_LABEL = {1: "full EV-size (aggressive)", 2: "moderate EV-size ×0.5", 3: "C² flat control"}


def _gate():
    """(passed, n, net, ghosts, ghost_rate)."""
    n, net, ghosts, gr = _fleet_deep_pool_stats()
    passed = (net >= 0) and (gr <= GHOST_MAX) and (n >= MIN_CLOSES)
    return passed, n, net, ghosts, gr


def _already_armed():
    for b in GENES:
        try:
            if json.loads((ROOT / f"bots/bot{b}/ev_sizing.json").read_text()).get("enabled"):
                continue
        except Exception:
            return False
        return False
    # all genes' files exist & enabled
    return all((ROOT / f"bots/bot{b}/ev_sizing.json").exists() for b in GENES)


def _write_genes(stats):
    n, net, gr = stats
    for b, gene in GENES.items():
        payload = dict(gene)
        payload.update({"armed_ts": time.time(),
                        "by": "arm_genes (race gen1) — deploy gate PASS",
                        "gate": {"n": n, "net_sol": round(net, 5), "ghost_rate": round(gr, 4)}})
        fp = ROOT / f"bots/bot{b}/ev_sizing.json"
        fp.write_text(json.dumps(payload, indent=2))
        print(f"   🧬 Bot{b} ← {fp.name}  ({GENE_LABEL[b]})")
    print(f"   Bot3 untouched ({GENE_LABEL[3]}). Bots hot-reload ≤30s — no restart needed.")
    print("   Revert any: rm bots/bot*/ev_sizing.json")


def main(arm=False):
    gate_passed, n, net, ghosts, gr = _gate()
    # S98 sequencing guard — the ride exit must be PROVEN live before sizing arms.
    exit_proven, xdetail = ride_exit_proven()
    passed = gate_passed and exit_proven
    print("=" * 64)
    print("  ARM GENES — prestige-race SIZE genes, gate-gated activation")
    print("=" * 64)
    print(f"   [{'PASS' if n >= MIN_CLOSES else 'WAIT'}]  deep_pool closes n ≥ {MIN_CLOSES}      {n}")
    print(f"   [{'PASS' if net >= 0 else 'WAIT'}]  live net ≥ 0              ◎{net:+.5f}")
    print(f"   [{'PASS' if gr <= GHOST_MAX else 'WAIT'}]  ghost-rate ≤ {GHOST_MAX:.0%}          {gr:.0%} ({ghosts}/{n})")
    print(f"   [{'PASS' if exit_proven else 'WAIT'}]  ride-exit proven live    {xdetail.get('binding', '—')}")
    print("  " + "─" * 60)

    if _already_armed():
        print("  ✅ Genes already ARMED (ev_sizing files present). Nothing to do.")
        print("=" * 64)
        return True

    if not passed:
        print("  ⏳ DORMANT — gate not yet passed. No files written, no SOL sized up.")
        if gate_passed and not exit_proven:
            print("     Deploy gate is GREEN but the RIDE exit is not yet proven live —")
            print("     holding sizing until the exit A/B is conclusive (python3 ride_ab.py).")
        else:
            print("     Genes activate automatically once the edge is +EV live AND the ride")
            print("     exit is proven (deploy gate + ride_ab both PASS).")
        print("=" * 64)
        return False

    print("  🚀 DEPLOY GATE PASS — the deep_pool edge is proven +EV live.")
    if arm:
        _write_genes((n, net, gr))
        print("  ✅ Size genes ACTIVATED. The real prestige race begins now.")
    else:
        print("  → run `python3 arm_genes.py --arm` to activate the three genes.")
    print("=" * 64)
    return passed


if __name__ == "__main__":
    main(arm=("--arm" in sys.argv))
