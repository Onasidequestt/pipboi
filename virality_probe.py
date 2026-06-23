#!/usr/bin/env python3
"""
virality_probe.py — read-only "Jotchua-shape" detector for meme-viral tokens. (S90)

The free, human-in-the-loop virality lens. It does NOT scrape X (X is fully paywalled —
HTTP 402/login wall) and it touches NOTHING the fleet owns: no bots/, no trades.db, no
genes, no keys. It only READS the live sidecar feed + the public DexScreener API and
prints a readout you can act on.

What it measures = the on-chain signature of a Jotchua-shaped move (the off-chain social
half you confirm yourself via the research links it prints, or later via a revived
LunarCrush — see lunarcrush.py):

  • vol-ACCELERATION   5m volume vs the hourly run-rate (the bot's LEAD edge, S87)
  • buy-pressure       sustained >55% buys across 5m / 1h / 24h (the hold-incentive tell)
  • live multi-leg     still CLIMBING (1h & 6h both green) vs pumped-and-COOLING (late)
  • sellability        liquidity depth + filling/draining (liq_velocity) — the ghost gate
  • freshness          young pair (<72h) = fresh momentum, not an aged blue-chip
  • social presence    has an X/website link (presence only — magnitude needs a social API)

USAGE
  python3 virality_probe.py <MINT>        # deep probe one token (any mint, via DexScreener)
  python3 virality_probe.py --scan        # rank the LIVE discovery feed for Jotchua-shapes
  python3 virality_probe.py --scan --top 15
  python3 virality_probe.py --watchlist   # probe the fleet's currently-open positions

Pure stdlib (urllib) — zero deps, cannot interfere with the running bot.
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "shared_memory" / "discovery_snapshot.json"
DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"

# ── scoring weights (tuned to surface live, accelerating, sellable runners) ──
_W = dict(vaccel=30, buy=20, leg=25, liq=15, fresh=10, social=5)
_LIQ_FLOOR = 30_000.0      # below this = not credibly sellable (the ghost zone)


def _get_json(url: str, timeout: float = 15.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vault-virality-probe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"  ! fetch failed: {e}", file=sys.stderr)
        return None


def _bp(buys, sells) -> float:
    t = (buys or 0) + (sells or 0)
    return (buys or 0) / t if t else 0.5


def _vol_accel(vol_5m, vol_1h) -> float:
    """5m volume vs the per-5min hourly run-rate. >1 = accelerating, <1 = fading."""
    run_rate = (vol_1h or 0) / 12.0
    if run_rate < 1.0:
        return 3.0 if (vol_5m or 0) > 0 else 0.0   # genesis burst
    return min((vol_5m or 0) / run_rate, 5.0)


def _norm_from_dexscreener(mint: str):
    """Pull a token from DexScreener and map it into the common metric dict."""
    d = _get_json(DEX_TOKENS + mint)
    pairs = (d or {}).get("pairs") or []
    if not pairs:
        return None, None
    # pick the deepest-liquidity pair
    p = max(pairs, key=lambda x: (x.get("liquidity", {}) or {}).get("usd", 0) or 0)
    pc, vol, tx = p.get("priceChange", {}), p.get("volume", {}), p.get("txns", {})
    info = p.get("info", {}) or {}
    created = p.get("pairCreatedAt")
    import time as _t
    age_h = ((_t.time() - created / 1000) / 3600.0) if created else None
    m = {
        "symbol": (p.get("baseToken", {}) or {}).get("symbol", "?"),
        "mint": mint,
        "price_usd": float(p.get("priceUsd") or 0),
        "fdv": p.get("fdv"),
        "liquidity_usd": (p.get("liquidity", {}) or {}).get("usd", 0) or 0,
        "pair_age_hours": age_h,
        "pair_address": p.get("pairAddress"),
        "dex": p.get("dexId"),
        "price_change_5m": pc.get("m5", 0), "price_change_1h": pc.get("h1", 0),
        "price_change_6h": pc.get("h6", 0), "price_change_24h": pc.get("h24", 0),
        "volume_5m": vol.get("m5", 0), "volume_1h": vol.get("h1", 0),
        "volume_6h": vol.get("h6", 0), "volume_24h": vol.get("h24", 0),
        "txns_5m_buys": tx.get("m5", {}).get("buys", 0), "txns_5m_sells": tx.get("m5", {}).get("sells", 0),
        "txns_1h_buys": tx.get("h1", {}).get("buys", 0), "txns_1h_sells": tx.get("h1", {}).get("sells", 0),
        "txns_24h_buys": tx.get("h24", {}).get("buys", 0), "txns_24h_sells": tx.get("h24", {}).get("sells", 0),
        "has_socials": bool(info.get("socials")),
    }
    return m, info


def score(m: dict, liq_vel: float = None) -> dict:
    """Return {score 0-100, parts, verdict, flags}. Higher = more Jotchua-shaped.

    Credibility gates mirror the bot's S87 scorer so micro-pool NOISE can't rank:
      • a ratio on $300 of volume is meaningless → vol-accel is scaled by a volume-
        credibility factor (full only above ~$10k/h),
      • buy-pressure needs a real sample (≥20 txns/h) or it's treated as neutral,
      • liquidity < $30k is the GHOST ZONE (unsellable) → the whole score is hard-capped.
    """
    s, parts, flags = 0.0, {}, []
    liq = m.get("liquidity_usd", 0) or 0
    vol_1h = m.get("volume_1h", 0) or 0
    cred = min(vol_1h / 10_000.0, 1.0)               # volume credibility 0–1

    # 1. vol-acceleration (LEAD) — only meaningful on credible volume
    vacc = _vol_accel(m.get("volume_5m"), vol_1h)
    p = _W["vaccel"] * min(vacc / 2.0, 1.0) * cred    # vacc 2.0+ = full, scaled by credibility
    parts["vaccel"] = p; s += p

    # 2. sustained buy pressure (needs a real sample)
    tx1 = (m.get("txns_1h_buys", 0) or 0) + (m.get("txns_1h_sells", 0) or 0)
    bp5 = _bp(m.get("txns_5m_buys"), m.get("txns_5m_sells"))
    bp1 = _bp(m.get("txns_1h_buys"), m.get("txns_1h_sells")) if tx1 >= 20 else 0.5
    bp24 = _bp(m.get("txns_24h_buys"), m.get("txns_24h_sells"))
    bp = 0.5 * bp1 + 0.3 * bp5 + 0.2 * bp24          # weight the 1h tape most
    p = _W["buy"] * max(0.0, (bp - 0.5) / 0.3)        # 0.50→0, 0.80→full
    parts["buy"] = min(p, _W["buy"]); s += parts["buy"]
    if tx1 >= 20 and bp1 < 0.45:
        flags.append("net-SELLING (1h)")

    # 3. live multi-leg vs cooling
    c1, c6 = m.get("price_change_1h", 0) or 0, m.get("price_change_6h", 0) or 0
    c5 = m.get("price_change_5m", 0) or 0
    leg = 0.0
    if c6 > 20: leg += 0.45                            # a real run is underway
    if c1 > 5:  leg += 0.40                            # still climbing this hour
    elif c1 < -3: leg -= 0.30; flags.append("COOLING (1h red)")
    if c5 > -2: leg += 0.15                            # not actively dumping right now
    p = _W["leg"] * max(0.0, min(leg, 1.0))
    parts["leg"] = p; s += p

    # 4. sellability (saturating) + drain gate
    if liq < _LIQ_FLOOR:
        parts["liq"] = 0.0; flags.append(f"thin liq ${liq:,.0f} (<${_LIQ_FLOOR:,.0f} = ghost zone)")
    else:
        parts["liq"] = _W["liq"] * min((liq - _LIQ_FLOOR) / (120_000 - _LIQ_FLOOR), 1.0)
    s += parts["liq"]
    if liq_vel is not None and liq_vel < -0.02:
        s -= 12; flags.append(f"DRAINING liq_vel {liq_vel:+.3f} (rug risk)")

    # 5. freshness
    age = m.get("pair_age_hours")
    if age is None:
        parts["fresh"] = _W["fresh"] * 0.5
    elif age <= 72:
        parts["fresh"] = _W["fresh"] * (1.0 - age / 144.0)   # <72h decays toward 0.5
    else:
        parts["fresh"] = _W["fresh"] * 0.15                  # aged blue-chip
    s += parts["fresh"]

    # 6. social presence (presence only — magnitude needs LunarCrush / X API)
    parts["social"] = _W["social"] if m.get("has_socials") else 0.0
    s += parts["social"]

    # HARD GATE: an unsellable pool is not a capturable runner, however it's moving —
    # cap it hard so the micro-pool ghost flood can never outrank a real, sellable move.
    if liq < _LIQ_FLOOR:
        s = min(s, 22.0)

    s = max(0.0, min(100.0, s))
    if   s >= 68: verdict = "🔥 LIVE RUNNER (Jotchua-shape)"
    elif s >= 50: verdict = "⚡ BUILDING — watch"
    elif s >= 32: verdict = "〰  mid / cooling"
    else:         verdict = "💤 quiet / weak"
    if liq < _LIQ_FLOOR:                     verdict = "☠ thin / ghost zone (unsellable)"
    if any("DRAINING" in f for f in flags):  verdict = "☠ DRAINING — avoid"
    return {"score": round(s, 1), "parts": {k: round(v, 1) for k, v in parts.items()},
            "verdict": verdict, "flags": flags, "vacc": round(vacc, 2), "bp1h": round(bp1, 2)}


def _research_links(m: dict, info: dict) -> list:
    sym = m.get("symbol", "?")
    mint = m["mint"]
    links = [("DexScreener", f"https://dexscreener.com/solana/{m.get('pair_address') or mint}")]
    for soc in (info or {}).get("socials", []) or []:
        links.append((soc.get("type", "social").title(), soc.get("url")))
    links.append(("X cashtag (live)", f"https://x.com/search?q=%24{sym}&f=live"))
    if mint.endswith("pump"):
        links.append(("pump.fun", f"https://pump.fun/{mint}"))
    links.append(("Solscan", f"https://solscan.io/token/{mint}"))
    return links


def probe_one(mint: str):
    print(f"\n  Fetching {mint[:10]}… from DexScreener …")
    m, info = _norm_from_dexscreener(mint)
    if not m:
        print("  ✗ no DexScreener pairs — token not indexed / no liquidity.\n"); return
    r = score(m)
    print("\n" + "═" * 66)
    print(f"  ${m['symbol']}   {r['verdict']}   ·   VIRALITY {r['score']}/100")
    print("═" * 66)
    fdv = f"${m['fdv']:,.0f}" if m.get("fdv") else "—"
    age = f"{m['pair_age_hours']:.1f}h" if m.get("pair_age_hours") is not None else "—"
    print(f"  price ${m['price_usd']:.6g}   liq ${m['liquidity_usd']:,.0f}   fdv {fdv}   age {age}   dex {m['dex']}")
    print(f"  Δ 5m {m['price_change_5m']:+.1f}%  1h {m['price_change_1h']:+.1f}%  "
          f"6h {m['price_change_6h']:+.1f}%  24h {m['price_change_24h']:+.1f}%")
    print(f"  vol 5m ${m['volume_5m']:,.0f}  1h ${m['volume_1h']:,.0f}  24h ${m['volume_24h']:,.0f}")
    print(f"  vol-accel {r['vacc']}×   buy-pressure 1h {r['bp1h']}   "
          f"buys/sells 1h {m['txns_1h_buys']}/{m['txns_1h_sells']}")
    print(f"  score breakdown: " + "  ".join(f"{k}={v}" for k, v in r["parts"].items()))
    if r["flags"]:
        print("  ⚠ " + "  ·  ".join(r["flags"]))
    print("\n  🔎 RESEARCH (confirm the social half yourself — X is paywalled to bots):")
    for label, url in _research_links(m, info):
        if url: print(f"     {label:<18} {url}")
    print()


def scan(top: int):
    if not SNAP.exists():
        print("  ✗ no discovery_snapshot.json — is the sidecar running?"); return
    snap = json.loads(SNAP.read_text())
    md = snap.get("market_data", {}) or {}
    lv = snap.get("liq_velocity", {}) or {}
    if not md:
        print("  ✗ discovery feed empty this cycle (Birdeye/gecko fetch failed) — retry shortly."); return
    rows = []
    for mint, m in md.items():
        r = score(m, liq_vel=lv.get(mint))
        rows.append((r["score"], m, r))
    rows.sort(key=lambda x: -x[0])
    print(f"\n  LIVE VIRALITY SCAN — {len(md)} tokens in feed, top {min(top, len(rows))} by Jotchua-shape")
    print(f"  feed ts {snap.get('ts','?')}   ·   sol ${snap.get('sol_price','?')}")
    print("  " + "─" * 78)
    print(f"  {'#':>2} {'SYM':<10} {'SCORE':>6} {'vacc':>5} {'bp1h':>5} {'liq$':>9} "
          f"{'1h%':>7} {'6h%':>8}  VERDICT")
    print("  " + "─" * 78)
    for i, (sc, m, r) in enumerate(rows[:top], 1):
        print(f"  {i:>2} {m.get('symbol','?')[:10]:<10} {sc:>6} {r['vacc']:>5} {r['bp1h']:>5} "
              f"${m.get('liquidity_usd',0):>8,.0f} {m.get('price_change_1h',0):>+6.1f} "
              f"{m.get('price_change_6h',0):>+7.1f}  {r['verdict']}")
    print("  " + "─" * 78)
    print("  → deep-probe any of these:  python3 virality_probe.py <MINT>\n")


def trending(top: int):
    """Pull DexScreener BOOSTED Solana tokens (paid promotion = a virality proxy),
    score each, and surface the Jotchua-shapes. This is the DISCOVERY mechanism —
    it finds candidates the fleet's narrow discovery window hasn't reached yet."""
    boosts = _get_json("https://api.dexscreener.com/token-boosts/latest/v1") or []
    if isinstance(boosts, dict):
        boosts = boosts.get("data", []) or []
    mints = []
    for b in boosts:
        if b.get("chainId") == "solana" and b.get("tokenAddress"):
            if b["tokenAddress"] not in mints:
                mints.append(b["tokenAddress"])
    if not mints:
        print("  ✗ no boosted Solana tokens returned (API empty/rate-limited)."); return
    print(f"\n  TRENDING (boosted) SCAN — scoring {min(len(mints), 30)} boosted Solana tokens …")
    rows = []
    for mint in mints[:30]:
        m, _ = _norm_from_dexscreener(mint)
        if m:
            r = score(m)
            rows.append((r["score"], m, r))
    rows.sort(key=lambda x: -x[0])
    print("  " + "─" * 78)
    print(f"  {'#':>2} {'SYM':<10} {'SCORE':>6} {'vacc':>5} {'bp1h':>5} {'liq$':>9} "
          f"{'1h%':>7} {'6h%':>8}  VERDICT")
    print("  " + "─" * 78)
    for i, (sc, m, r) in enumerate(rows[:top], 1):
        print(f"  {i:>2} {m.get('symbol','?')[:10]:<10} {sc:>6} {r['vacc']:>5} {r['bp1h']:>5} "
              f"${m.get('liquidity_usd',0):>8,.0f} {m.get('price_change_1h',0):>+6.1f} "
              f"{m.get('price_change_6h',0):>+7.1f}  {r['verdict']}")
    print("  " + "─" * 78)
    print("  → deep-probe any:  python3 virality_probe.py <MINT>\n")


def watchlist():
    bots = sorted((ROOT / "bots").glob("bot*/positions.json"))
    seen = set()
    for pf in bots:
        try:
            pos = json.loads(pf.read_text())
        except Exception:
            continue
        for mint in pos:
            if mint not in seen:
                seen.add(mint)
                print(f"\n  [{pf.parent.name}] holds {mint[:10]}…")
                probe_one(mint)
    if not seen:
        print("  (no open positions across the fleet)")


def main():
    ap = argparse.ArgumentParser(description="Jotchua-shape virality probe (read-only)")
    ap.add_argument("mint", nargs="?", help="token mint to deep-probe")
    ap.add_argument("--scan", action="store_true", help="rank the live discovery feed")
    ap.add_argument("--trending", action="store_true", help="find NEW candidates: score DexScreener-boosted Solana tokens")
    ap.add_argument("--top", type=int, default=12, help="rows to show in --scan/--trending")
    ap.add_argument("--watchlist", action="store_true", help="probe the fleet's open positions")
    a = ap.parse_args()
    if a.scan:
        scan(a.top)
    elif a.trending:
        trending(a.top)
    elif a.watchlist:
        watchlist()
    elif a.mint:
        probe_one(a.mint)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
