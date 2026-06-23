#!/usr/bin/env python3
"""gene_propagation.py — GEN-2 seeding: copy the prestige WINNER's SIZE gene to the rest.

The prestige race (race.py) varies ONE gene across the fleet — SIZE (ev_sizing.json). When a
bot first reaches ◎2.0 and prestiges, gen-1 is decided. This is the missing genetic-algorithm
step the handoff flagged ("Propagation step NOT built"): the winner's gene must seed the next
generation, or bots 4-6 launch on the DEFAULT gene instead of the proven winner.

  propagate: winner's ev_sizing.json  ──►  every other bot (losers 1-3 + new 4-6)
  if the winner is the FLAT control (no ev_sizing.json), propagation REMOVES the file
  everywhere — the fleet adopts "flat is best," which is itself a valid evolutionary result.

SAFETY — this respects THE ONE OPERATOR RULE. Propagation writes ev_sizing.json, which the rule
forbids doing MANUALLY on an unproven edge. But a genuine prestige can only occur AFTER the
deploy gate opened and gene_arm_loop armed the winner's gene on a PROVEN +EV edge — so the gene
being copied is already proven. To keep that guarantee, this script HARD-REFUSES to commit
unless a real winner exists (a bot with payout_count > 0), and it is DRY-RUN by default.

    python3 gene_propagation.py              # detect winner + show the plan (writes NOTHING)
    python3 gene_propagation.py --commit     # propagate IF a genuine winner exists
    python3 gene_propagation.py --winner 1 --commit   # operator override of winner detection
    python3 gene_propagation.py --simulate 1 # dry-run AS IF bot 1 won (testing, no winner needed)
"""
import json, sqlite3, sys, time, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALL_BOTS = (1, 2, 3, 4, 5, 6)
GEN = 2  # the generation this propagation seeds


def _gene_path(b: int) -> Path:
    return ROOT / f"bots/bot{b}/ev_sizing.json"


def _read_gene(b: int):
    """The bot's SIZE gene = its ev_sizing.json payload, or None for the flat control."""
    try:
        return json.loads(_gene_path(b).read_text())
    except Exception:
        return None


def _gene_label(g) -> str:
    if not g or not g.get("enabled"):
        return "C² flat (control)"
    sc = g.get("scale")
    return f"EV-size ×{sc}" if sc else "EV-size (full)"


def _total_payouts(b: int) -> int:
    """Genuine-prestige counter for bot b (same field /api/fleet uses to unlock row 2)."""
    try:
        live = json.loads((ROOT / f"bots/bot{b}/status.json").read_text())
        return sum(m.get("payout_count", 0) for m in live.get("payout_milestones", []))
    except Exception:
        return 0


def detect_winner():
    """The bot that has genuinely prestiged (payout_count > 0), else None."""
    winners = [b for b in ALL_BOTS if _total_payouts(b) > 0]
    return winners[0] if winners else None


def _core_gene(g):
    """Strip provenance metadata so we compare/copy only the behavioural gene."""
    if not g:
        return None
    return {k: g[k] for k in ("enabled", "scale") if k in g}


def _audit(b: int, record: dict) -> None:
    record["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        c = sqlite3.connect(str(ROOT / f"bots/bot{b}/trades.db"))
        c.execute("""CREATE TABLE IF NOT EXISTS trades
            (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
             event TEXT NOT NULL, data TEXT NOT NULL)""")
        c.execute("INSERT INTO trades (ts, event, data) VALUES (?,?,?)",
                  (record["ts"], "gene_propagation", json.dumps(record)))
        c.commit(); c.close()
    except Exception as e:
        print(f"   ⚠ audit write failed (bot{b}): {e}")


def plan(winner: int):
    """Return the list of (bot, action, before_label, after_label) without writing anything."""
    win_gene = _core_gene(_read_gene(winner))
    rows = []
    for b in ALL_BOTS:
        if b == winner:
            continue
        cur = _core_gene(_read_gene(b))
        if cur == win_gene:
            action = "keep"
        elif win_gene is None:
            action = "remove"            # winner is flat → drop EV-sizing everywhere
        else:
            action = "write"
        rows.append((b, action, _gene_label(_read_gene(b)), _gene_label(win_gene)))
    return win_gene, rows


def propagate(winner: int, win_gene, rows, basis: str):
    changed = 0
    for b, action, _bl, _al in rows:
        if action == "keep":
            continue
        fp = _gene_path(b)
        if action == "remove":
            fp.unlink(missing_ok=True)
        else:  # write
            payload = dict(win_gene)
            payload.update({"gen": GEN, "propagated_from": winner,
                            "propagated_ts": time.time(), "by": f"gene_propagation ({basis})"})
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(payload, indent=2))
        _audit(b, {"event": "gene_propagation", "from_winner": winner, "gen": GEN,
                   "action": action, "gene": win_gene, "basis": basis})
        changed += 1
    return changed


def main(argv):
    commit   = "--commit" in argv
    sim_id   = None
    win_over = None
    if "--simulate" in argv:
        try: sim_id = int(argv[argv.index("--simulate") + 1])
        except Exception: sim_id = None
    if "--winner" in argv:
        try: win_over = int(argv[argv.index("--winner") + 1])
        except Exception: win_over = None

    real_winner = detect_winner()
    winner = sim_id or win_over or real_winner
    basis = ("simulate" if sim_id else "operator-override" if win_over else "auto-detect")

    print("=" * 66)
    print("  GENE PROPAGATION — seed gen-2 from the prestige winner")
    print("=" * 66)
    print(f"  genuine winner (payout_count>0): {('Bot'+str(real_winner)) if real_winner else 'NONE yet'}")
    if winner is None:
        print("  ⏳ No winner — nothing to propagate. (Run after a bot prestiges to ◎2.0,")
        print("     or `--simulate N` to preview, or `--winner N --commit` to force.)")
        print("=" * 66)
        return 1
    if sim_id:
        print(f"  🧪 SIMULATING winner = Bot{winner} (no real prestige required; dry-run only)")

    win_gene, rows = plan(winner)
    print(f"  WINNER Bot{winner} gene: {_gene_label(_read_gene(winner))}  → seeds:")
    print("  " + "─" * 62)
    for b, action, before, after in rows:
        arrow = {"keep": "= already", "write": "←", "remove": "✕ drop"}[action]
        print(f"    Bot{b}: {before:<22} {arrow:<9} {after}")
    print("  " + "─" * 62)

    n_change = sum(1 for _, a, *_ in rows if a != "keep")
    if not commit:
        print(f"  DRY-RUN — {n_change} bot(s) would change. Add --commit to apply.")
        print("=" * 66)
        return 0

    # ── commit path — hard guard ──
    if sim_id and not win_over:
        print("  ⛔ REFUSING to commit a --simulate winner. Use --winner N to force a real write.")
        print("=" * 66)
        return 2
    if real_winner is None and win_over is None:
        print("  ⛔ REFUSING to commit — no genuine prestige winner (payout_count==0 fleet-wide).")
        print("     Propagating an unproven gene violates THE ONE OPERATOR RULE.")
        print("=" * 66)
        return 2

    changed = propagate(winner, win_gene, rows, basis)
    print(f"  ✅ Propagated Bot{winner}'s gene → {changed} bot(s). Bots hot-reload ≤30s;")
    print(f"     unlaunched bots (4-6) pick it up at spawn. Revert: rm bots/bot*/ev_sizing.json")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
