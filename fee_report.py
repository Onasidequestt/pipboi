#!/usr/bin/env python3
"""fee_report.py — S105: make the fleet's TRANSACTION-COST drag visible (READ-ONLY).

The S105 diagnosis (3h dig, 2026-06-09): the fleet's real bleed was fees that `pnl_sol`
never recorded. Two fixes followed, both now live:
  • proceeds-pnl (parallel S105): pnl_sol is booked from the REAL on-chain wallet delta
    → it now captures base fee + priority fee + slippage by construction.
  • tip accounting (this S105): the Jito TIP is paid in a separate bundle tx that the
    swap-delta misses, so it's logged as a standalone `fee_sol` on each close row.

This tool aggregates both so the cost is finally legible per bot / play / fleet. It
NEVER writes bots/genes/trades.db — pure read. Run: `python3 fee_report.py [--all]`.

Columns:
  closes          full closes in scope
  volume◎         Σ size_sol (capital deployed = swap notional, one side)
  pnl◎            Σ pnl_sol (proceeds-based once the canary is on — fee+slippage incl.)
  tips◎           Σ fee_sol (round-trip Jito tip — the residual unbooked fee)
  TRUE net◎       pnl◎ − tips◎  (what the wallet actually keeps, all-in)
  tip%vol         tips◎ / volume◎  (the tip drag as % of notional)
  px→proc◎        Σ(pnl_sol_proceeds − pnl_sol_price): how much fee+slippage proceeds-pnl
                  now captures that the OLD price-pnl was blind to (negative = was hidden)
"""
import json, sqlite3, sys, glob, os

BOTS = [1, 2, 3]
ALL = "--all" in sys.argv  # default: only rows carrying fee_sol (post-tip-restart); --all = whole ledger


def _closes(b):
    con = sqlite3.connect(f"file:bots/bot{b}/trades.db?mode=ro", uri=True)
    rows = [json.loads(r[0]) for r in con.execute(
        "SELECT data FROM trades WHERE event='close'").fetchall()]
    con.close()
    return rows


def _equity_now(b):
    try:
        d = json.load(open(f"bots/bot{b}/status.json"))
        return (d.get("sol_balance") or 0.0) + (d.get("sol_in_trades") or 0.0)
    except Exception:
        return None


def _fmt(b, rows):
    n = len(rows)
    vol = sum(r.get("size_sol") or 0 for r in rows)
    pnl = sum(r.get("pnl_sol") or 0 for r in rows)
    tips = sum(r.get("fee_sol") or 0 for r in rows)
    # how much proceeds-pnl captured vs the old price-pnl (only rows that carry both)
    both = [r for r in rows if r.get("pnl_sol_price") is not None and r.get("pnl_sol_proceeds") is not None]
    captured = sum((r["pnl_sol_proceeds"] - r["pnl_sol_price"]) for r in both)
    true_net = pnl - tips
    tip_pct = (tips / vol * 100) if vol else 0.0
    return (f"  bot{b}: closes {n:4d} | vol ◎{vol:7.3f} | pnl ◎{pnl:+8.4f} | tips ◎{tips:+8.4f} "
            f"| TRUE net ◎{true_net:+8.4f} | tip%vol {tip_pct:5.2f}% | px→proc ◎{captured:+.4f} (n={len(both)})")


def main():
    scope = "WHOLE LEDGER" if ALL else "rows carrying fee_sol (post-S105 tip-restart)"
    print(f"\n=== FEE REPORT — scope: {scope} ===")
    print("  (proceeds-pnl folds fee+slippage INTO pnl; tips◎ is the residual Jito tip; TRUE net = pnl − tips)\n")
    tot_vol = tot_pnl = tot_tips = tot_cap = 0.0
    tot_n = 0
    have_fee_total = 0
    for b in BOTS:
        rows = _closes(b)
        if not ALL:
            rows = [r for r in rows if r.get("fee_sol") is not None]
        have_fee = [r for r in rows if r.get("fee_sol") is not None]
        have_fee_total += len(have_fee)
        print(_fmt(b, rows))
        tot_n += len(rows)
        tot_vol += sum(r.get("size_sol") or 0 for r in rows)
        tot_pnl += sum(r.get("pnl_sol") or 0 for r in rows)
        tot_tips += sum(r.get("fee_sol") or 0 for r in rows)
        both = [r for r in rows if r.get("pnl_sol_price") is not None and r.get("pnl_sol_proceeds") is not None]
        tot_cap += sum((r["pnl_sol_proceeds"] - r["pnl_sol_price"]) for r in both)

    print("  " + "-" * 100)
    true_net = tot_pnl - tot_tips
    tip_pct = (tot_tips / tot_vol * 100) if tot_vol else 0.0
    print(f"  FLEET: closes {tot_n:4d} | vol ◎{tot_vol:7.3f} | pnl ◎{tot_pnl:+8.4f} | tips ◎{tot_tips:+8.4f} "
          f"| TRUE net ◎{true_net:+8.4f} | tip%vol {tip_pct:5.2f}% | px→proc ◎{tot_cap:+.4f}")

    # ── interpretation ───────────────────────────────────────────────────────
    print()
    if have_fee_total == 0:
        print("  ⚠ No close rows carry fee_sol yet — the tip-accounting restart hasn't booked a close.")
        print("    Run `--all` for the legacy view; re-run once the fleet has closed trades post-restart.")
    else:
        print(f"  • {have_fee_total} closes carry the new fee_sol (tip) field.")
        if tot_cap < -1e-6:
            print(f"  • proceeds-pnl is now booking ◎{-tot_cap:.4f} of fee+slippage that the OLD price-pnl HID "
                  f"(it read those as ~flat).")
        if tot_tips > 1e-6:
            print(f"  • Jito tips cost ◎{tot_tips:.4f} on ◎{tot_vol:.2f} of volume ({tip_pct:.2f}%) — the residual "
                  f"unbooked fee, now visible.")
        print(f"  • TRUE all-in net = ◎{true_net:+.4f}. If this is < pnl, the gap is tips the gate/edge_report "
              f"still don't subtract.")

    # ── per-play tip drag (fleet, scoped rows) ───────────────────────────────
    play = {}
    for b in BOTS:
        rows = _closes(b)
        if not ALL:
            rows = [r for r in rows if r.get("fee_sol") is not None]
        for r in rows:
            p = r.get("play") or "—"
            d = play.setdefault(p, [0, 0.0, 0.0, 0.0])
            d[0] += 1
            d[1] += r.get("size_sol") or 0
            d[2] += r.get("pnl_sol") or 0
            d[3] += r.get("fee_sol") or 0
    if play:
        print("\n  per-play (scoped):  play          n   vol◎     pnl◎      tips◎    TRUE net◎")
        for p, (n, v, pn, t) in sorted(play.items(), key=lambda kv: kv[1][2] - kv[1][3]):
            print(f"    {p:14s} {n:4d}  {v:6.3f}  {pn:+7.4f}  {t:+7.4f}   {pn - t:+7.4f}")

    # ── wallet reconcile (sanity) ────────────────────────────────────────────
    print("\n  wallet reconcile (equity now vs ◎1.0 start — ground truth incl. ALL fees):")
    for b in BOTS:
        eq = _equity_now(b)
        if eq is not None:
            print(f"    bot{b}: equity ◎{eq:.4f}  (lifetime {eq - 1.0:+.4f} vs ◎1.0)")
    print()


if __name__ == "__main__":
    if not os.path.isdir("bots"):
        print("run from ~/solana-trader (no bots/ dir here)"); sys.exit(1)
    main()
