#!/usr/bin/env python3
"""
heal_trade_memory_s100.py — one-off, STOP-WINDOW heal for the S92/S100 phantom residue
in shared_memory/trade_memory.json (the deferred cosmetic cleanup).

CONTEXT
  The S92/S100 glitched-price phantoms (a feed read of ~5000× fabricating a "+495,805%
  TP bank") were corrected IN THE LEDGER (trades.db rows zeroed, pnl_sol_orig/pnl_orig
  preserved, corrected_by S92/S100). But the fleet-shared learning store
  trade_memory.json still carries the inflated cumulative `total_pnl` for those mints
  (e.g. Cm6fNnMk $27,273, Dz9mQ9 $16,048). It is COSMETIC — confidence keys on win_rate
  post-S89, not total_pnl — but it trips reconcile_harness and is just wrong.

WHAT IT DOES
  For each mint whose |total_pnl| exceeds the outlier bound, recompute total_pnl as the
  sum of the CORRECTED per-mint `pnl` (USD) across all three bots' ledgers and write that
  back, preserving the old value in `total_pnl_orig` + tagging `corrected_by:"S100-memory"`.
  Nothing else in the entry is touched (wins/losses/streaks/momentum untouched).

SAFETY (why it is a STOP-WINDOW tool)
  trade_memory.json is rewritten LIVE by any bot that trades one of these mints, so a heal
  applied while the fleet is up would be CLOBBERED (and could race a writer). This script
  therefore REFUSES to run while any main.py is alive — run it in the restart stop-window
  (which you need anyway to load the on-disk S100 price-glitch fix). DRY-RUN by default;
  --apply writes (after a timestamped backup). Never touches bots/, genes, or trades.db.

USAGE
  python3 heal_trade_memory_s100.py                 # dry-run: show the plan
  python3 heal_trade_memory_s100.py --apply         # write (fleet MUST be stopped)
  python3 heal_trade_memory_s100.py --bound 5000    # outlier threshold (USD, default 5000)
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRADE_MEMORY = ROOT / "shared_memory" / "trade_memory.json"
BOTS = (1, 2, 3)


def _fleet_up():
    """True if any main.py process is alive (heal must not race a live writer)."""
    try:
        out = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, text=True)
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        return True   # fail-closed: if we can't tell, assume up and refuse


def _ledger_pnl_usd(mint):
    """Sum the CORRECTED `pnl` (USD) for a mint across all bots' close rows (read-only)."""
    total, n = 0.0, 0
    for b in BOTS:
        db = ROOT / f"bots/bot{b}/trades.db"
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for (d,) in c.execute("SELECT data FROM trades WHERE event='close'"):
                r = json.loads(d)
                if r.get("mint") == mint:
                    total += (r.get("pnl") or 0.0)   # corrected value; orig preserved on the row
                    n += 1
            c.close()
        except Exception:
            pass
    return total, n


def main():
    ap = argparse.ArgumentParser(description="Stop-window heal of phantom total_pnl in trade_memory.json.")
    ap.add_argument("--bound", type=float, default=5000.0, help="|total_pnl| (USD) above which a mint is a phantom outlier")
    ap.add_argument("--apply", action="store_true", help="write the heal (fleet MUST be stopped); default is dry-run")
    args = ap.parse_args()

    if not TRADE_MEMORY.exists():
        print("no trade_memory.json — nothing to heal."); return

    tm = json.loads(TRADE_MEMORY.read_text())
    plan = []
    for mint, m in tm.items():
        if not isinstance(m, dict):
            continue
        tp = m.get("total_pnl")
        if isinstance(tp, (int, float)) and abs(tp) > args.bound:
            corrected, n = _ledger_pnl_usd(mint)
            plan.append((mint, tp, round(corrected, 4), n))

    print("=" * 74)
    print("  TRADE_MEMORY PHANTOM HEAL (S100-memory) — " + ("APPLY" if args.apply else "DRY RUN"))
    print("=" * 74)
    if not plan:
        print(f"  no mints with |total_pnl| > ${args.bound:.0f} — nothing to heal.")
        print("=" * 74); return
    for mint, old, new, n in plan:
        print(f"  {mint[:16]}…  total_pnl  ${old:>12.2f}  →  ${new:>9.2f}   (from {n} corrected ledger closes)")
    print("  " + "─" * 70)

    if not args.apply:
        print("  DRY RUN — nothing written. Re-run with --apply IN THE RESTART STOP-WINDOW.")
        print("=" * 74); return

    if _fleet_up():
        print("  ✗ REFUSING TO WRITE — a main.py process is ALIVE. trade_memory.json is")
        print("    live-written and your heal would be clobbered. Stop the fleet first")
        print("    (the same restart that loads the on-disk S100 fix), then --apply.")
        print("=" * 74); sys.exit(2)

    backup = TRADE_MEMORY.with_suffix(f".json.bak.s100heal.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(TRADE_MEMORY, backup)
    for mint, old, new, _n in plan:
        m = tm[mint]
        m["total_pnl_orig"] = old
        m["total_pnl"] = new
        m["corrected_by"] = "S100-memory"
    TRADE_MEMORY.write_text(json.dumps(tm, indent=2))
    print(f"  ✅ healed {len(plan)} mint(s). Backup: {backup.name}")
    print("=" * 74)


if __name__ == "__main__":
    main()
