"""wash_veto.py — admission-side FAKE-BUY veto (READ-ONLY on-chain orderflow).

THE FINDING (cure-hunt, 2026-06-11): the live sidecar's `market_data` is COUNT-based —
it sees txn buy/sell COUNTS but never the real $ behind them, so it can be told "strong buy"
(bs >= 1.5) while real on-chain money is leaving. On 2,481 harvested forward-tracked obs
(paper_lab/sidecar_sources), the 485 such "fake buys" (count buy >=1.5 but on-chain net
flow <= 0) drew down deeper than real buys: mean fwd_min -6.7% vs -4.7%, and 11% vs 7% hit
<= -15% (the stop/rug zone). On the live ledger ~9 of 22 in-window big LOSER mints carried
clearly-negative flow (J4x1 -0.23, Cm6fNn -0.18, hf34 -0.18, JUPyiwrY -0.13, B6f27 -0.11).
So this VETO avoids ~40% of the loser tail the count-based sidecar is blind to. (The
buyer-gini "wash" thesis was TESTED and REJECTED — high gini → SHALLOWER drawdown — so this
veto is FAKE-BUY ONLY, not the gini composite.)

DISCIPLINE:
  • READ-ONLY — one GeckoTerminal REST GET of the pool's trade tape. Signs nothing, sends
    nothing, touches NO bot/gene/trades.db. Pure admission SKIP → cannot size or send.
  • FAIL-OPEN — any error / non-200 / timeout / thin tape ⇒ ALLOW. The veto can only ever
    REMOVE a candidate it is confident is a fake buy; it can NEVER block the pipeline (so it
    cannot cause an S91/S107-class lockout — that is the structural safety).
  • THROTTLED — a small semaphore + per-pair TTL cache so the per-candidate tape fetch does
    not re-starve the free GeckoTerminal tier (the S91 lesson). Runs CONCURRENTLY inside the
    existing buy-path asyncio.gather → adds zero serial latency.
  • SCOPE (enforced by the caller): gem / price-action only, NEVER the deep_pool/brain_rule
    gate cohort (frozen by S110). Bot1-only canary, default OFF.

Revert: rm bots/bot*/wash_veto.json (hot ≤30s, code goes inert) + rm wash_veto.py.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

_GECKO_TRADES = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/trades"
_HDRS = {"accept": "application/json"}

_CACHE_TTL = 45.0                       # re-eval the same pair within 45s ⇒ 0 extra calls
_cache: dict[str, tuple[float, dict]] = {}
_SEM = asyncio.Semaphore(2)             # meter the burst — never more than 2 concurrent gecko GETs


@dataclass
class WashResult:
    ok: bool                            # True = ALLOW (pass, or could-not-screen → fail-open)
    reason: str
    detail: dict = field(default_factory=dict)


def _net_frac(rows: list) -> tuple[float, int, float]:
    """(buy_usd - sell_usd)/total over the tape; returns (net_frac in [-1,1], n_trades, total_usd)."""
    buy = sell = 0.0
    n = 0
    for kind, usd in rows:
        if kind == "buy":
            buy += usd; n += 1
        elif kind == "sell":
            sell += usd; n += 1
    tot = buy + sell
    return ((buy - sell) / tot if tot > 0 else 0.0), n, tot


async def _fetch_flow(client: httpx.AsyncClient, pair_address: str, timeout: float) -> dict | None:
    """One GeckoTerminal tape GET → {net_frac, of_trades, of_usd}. None on any failure (→ fail-open)."""
    now = time.time()
    c = _cache.get(pair_address)
    if c and now - c[0] < _CACHE_TTL:
        return c[1]
    try:
        async with _SEM:
            r = await asyncio.wait_for(
                client.get(_GECKO_TRADES.format(pair=pair_address), headers=_HDRS),
                timeout=timeout,
            )
        if r.status_code != 200:
            return None
        rows = []
        for row in (r.json() or {}).get("data", []) or []:
            a = row.get("attributes", {}) or {}
            kind = a.get("kind")
            if kind not in ("buy", "sell"):
                continue
            try:
                usd = float(a.get("volume_in_usd") or 0.0)
            except (TypeError, ValueError):
                usd = 0.0
            rows.append((kind, usd))
        net, n, tot = _net_frac(rows)
        d = {"net_frac": round(net, 4), "of_trades": n, "of_usd": round(tot, 1)}
        _cache[pair_address] = (now, d)
        return d
    except Exception:
        return None


async def check(
    client: httpx.AsyncClient,
    pair_address: str | None,
    count_bs: float,
    *,
    min_count_bs: float = 1.5,
    min_trades: int = 12,
    timeout: float = 2.5,
) -> WashResult:
    """FAKE-BUY veto. Vetoes (ok=False) ONLY when the count says buy (count_bs >= min_count_bs)
    BUT the real on-chain tape shows net flow <= 0, on a tape thick enough to trust (>= min_trades).
    Everything else ALLOWS (fail-open)."""
    if not pair_address or (count_bs or 0) < min_count_bs:
        return WashResult(True, "n/a (count not a buy)")
    d = await _fetch_flow(client, pair_address, timeout)
    if d is None:
        return WashResult(True, "tape unavailable — allow (fail-open)")
    if d["of_trades"] < min_trades:
        return WashResult(True, f"thin tape n={d['of_trades']} — allow", d)
    if d["net_frac"] <= 0:
        return WashResult(
            False,
            f"FAKE BUY — count bs {count_bs:.1f} but on-chain net flow {d['net_frac']:+.2f} (n={d['of_trades']})",
            d,
        )
    return WashResult(True, f"real buy (net {d['net_frac']:+.2f}, n={d['of_trades']})", d)
