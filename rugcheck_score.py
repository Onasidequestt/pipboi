#!/usr/bin/env python3
"""RugCheck risk score as a candidate FEATURE — the free substitute for SolanaTracker's
pre-entry rug/sniper score (the other half of what the €50/mo would buy).

WHY: RugCheck is FREE, no key, and ALREADY integrated (`safety.py` RUGCHECK_URL, `rug_screen.py`)
— but only as a pass/FAIL gate. The /report/summary endpoint returns a numeric `score` +
`score_normalised` (0-100, higher = riskier) + `lpLockedPct` + `risks[]`. Surfacing that NUMBER
on each candidate lets the scorer/admission USE pre-entry risk as a continuous signal (the thing
the count-based sidecar is blind to) instead of only a binary block.

DISCIPLINE: read-only on chain (signs nothing); fail-soft (any error → None, never blocks an
entry); cached (10-min TTL per mint) so it adds ~0 load; canary-gated so it's inert until wired.
THE ONE OPERATOR RULE intact — this is a FEATURE, not sizing. No ev_sizing touched.

USAGE (after wiring): attach `risk = await risk_score(client, mint)` to the candidate dict; a
high `score_normalised` (e.g. >60) is a pre-entry de-prioritise / skip signal for the scorer.
Default-OFF via bots/botN/rugcheck_score.json {"enabled":true}; rm to revert.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_ROOT = Path(__file__).resolve().parent
URL = "https://api.rugcheck.xyz/v1/tokens"   # same host safety.py uses

_SEM = asyncio.Semaphore(3)
_CACHE: dict = {}          # mint -> (ts, result)
_CACHE_TTL = 600.0          # 10 min — risk profile changes slowly
_CANARY_CACHE = {"ts": -1e9, "val": {}}   # S121: −sentinel so the FIRST call reads the canary
                                          # immediately (monotonic() starts ~0 on macOS → was "off"
                                          # for a process's first 30s after a restart).


def _bot_id() -> int:
    import os
    try:
        return int(os.getenv("BOT_ID", "1"))
    except Exception:
        return 1


def rugcheck_score_on(bot_id: int | None = None) -> bool:
    """Gate: bots/botN/rugcheck_score.json {"enabled":true}. 30s cache."""
    if httpx is None:
        return False
    now = time.monotonic()
    if now - _CANARY_CACHE["ts"] >= 30.0:
        val = {}
        try:
            fp = _ROOT / f"bots/bot{bot_id if bot_id is not None else _bot_id()}/rugcheck_score.json"
            if fp.exists():
                val = json.loads(fp.read_text()) or {}
        except Exception:
            val = {}
        _CANARY_CACHE.update(ts=now, val=val)
    return bool(_CANARY_CACHE["val"].get("enabled"))


async def risk_score(client, mint: str, force: bool = False):
    """Return {score, score_normalised(0-100), lp_locked_pct, n_risks} or None. Fail-soft.
    `force=True` bypasses the canary gate (for the read-only validator)."""
    if httpx is None or not mint:
        return None
    if not force and not rugcheck_score_on():
        return None
    now = time.monotonic()
    hit = _CACHE.get(mint)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    out = None
    try:
        async with _SEM:
            r = await client.get(f"{URL}/{mint}/report/summary",
                                 headers={"accept": "application/json"}, timeout=8)
        if r.status_code == 200:
            j = r.json() or {}
            risks = j.get("risks") if isinstance(j.get("risks"), list) else []
            out = {
                "score":            float(j.get("score") or 0),
                "score_normalised": float(j.get("score_normalised") or 0),
                "lp_locked_pct":    float(j.get("lpLockedPct") or 0),
                "n_risks":          len(risks),
            }
        # 404 = not in RugCheck DB yet (fresh) → None = "unknown", never a block
    except Exception:
        out = None
    if out is not None:
        _CACHE[mint] = (now, out)
    return out


def cached_score(mint: str):
    """Cache-only read (no HTTP, no canary, no await) — for cheap per-cycle re-stamping of the
    candidate feature. Returns the cached dict or None if absent/expired. S121 V2."""
    hit = _CACHE.get(mint)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


if __name__ == "__main__":
    print("httpx:", httpx is not None, "| enabled (no canary):", rugcheck_score_on())
    if httpx:
        async def _demo():
            tokens = {
                "SOL":  "So11111111111111111111111111111111111111112",
                "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            }
            async with httpx.AsyncClient() as c:
                for sym, m in tokens.items():
                    r = await risk_score(c, m, force=True)
                    print(f"  {sym:5} → {r}")
        asyncio.run(_demo())
