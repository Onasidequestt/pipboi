"""
rug_screen.py — S98 PRE-ENTRY LEFT-TAIL RUG SCREEN (gem / price-action path).

THESIS (operator): a break-even distribution becomes +EV the moment you cut the fat
negative tail, and the tail is everything — ~95% of the fleet drawdown (S94b) is 8
catastrophic rugs/ghosts, e.g. 8AB1Jsbd (unsellable, ≈−0.22◎ across both bots), the
$6.75M→rug (LP pulled, −0.085◎), BCdwQBAn (honeypot, −0.11◎). S87's catastrophic-drain
exit and S92's price-glitch guard are REACTIVE — they shrink a −100% to a partial loss
AFTER entry. The bigger, reliable lever is to never enter the rug: screen the gem/INSANE
plays (the ones that BYPASS the liquidity floor, observer.py:~807) BEFORE capital commits.

WHAT IT CHECKS (in reliability order):
  • freeze authority NOT revoked  → dev can freeze your tokens = guaranteed −100% (on-chain)
  • mint   authority NOT revoked  → infinite-dilution rug                        (on-chain)
  • LP not locked / burned        → dev can pull liquidity (the $6.75M→rug)       (RugCheck)
  • top-holder concentration      → one wallet can dump on you                    (RugCheck)
  • pool age below a floor        → optional gem-path freshness gate              (signal)

DESIGN (operator-chosen, this session):
  • SCOPE = gem / price-action only. The caller passes already-filtered mints; deep_pool /
    brain_rule (the gate cohort — already ≥$50k & sellable, and throughput-starved) are NOT
    screened here, so this can't re-starve the deploy gate (the S87/S89/S91 lockout mode).
  • FAIL-CLOSED on gems: on-chain mint/freeze authority is a single deterministic RPC read; if
    it can't be confirmed REVOKED within the latency budget, REJECT. The unscreenable fresh gem
    is the highest-risk case, and "cutting one −100% is worth dozens of small wins."
  • LP / holder checks HARD-BLOCK only when RugCheck RETURNS the data (fresh gems 404 → no data
    → fall back to authority + the caller's honeypot round-trip). Best-effort, never fail-closed
    on a missing third-party.

This module is READ-ONLY on chain — it signs/sends nothing. Per-bot canary
bots/botN/rug_screen.json (loaded by main.py, 30s hot-reload). Revert: rm the canary
(hot) or remove the main.py wiring + this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

import helius
from safety import TRUSTED_MINTS

# RugCheck full report (richer than safety.py's /report/summary — carries the structured
# mintAuthority / freezeAuthority / topHolders / markets[].lp fields, not just risks[]).
_RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_RUGCHECK_TIMEOUT_S = 2.0

# The standard SPL token programs. A mint owned by neither is not a normal fungible token
# we can reason about → treated as unresolved (fail-closed on the gem path).
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Default thresholds (overridable per-bot via the canary). Conservative — the goal is to cut
# the unambiguous rug signatures, not to second-guess the scorer on borderline tokens.
_DEFAULT_TOP_HOLDER_MAX_PCT = 25.0   # single largest non-LP holder above this = dump risk
_DEFAULT_LP_LOCKED_MIN_PCT = 50.0    # LP locked/burned below this = pull risk
_DEFAULT_MIN_POOL_AGE_S = 0.0        # 0 = pool-age gate OFF (cold-start in main.py already
                                     # blocks <120s + weak buys); operator can raise it.

# RugCheck risk NAMES that, when present at danger level, independently confirm a hard signature
# even if the structured field is missing. Matched case-insensitively as substrings.
_RISK_FREEZE = ("freeze authority",)
_RISK_MINT = ("mint authority",)
_RISK_LP = ("lp", "liquidity unlocked", "liquidity not locked")


@dataclass
class ScreenResult:
    ok: bool                              # True = safe to enter
    reason: str                           # human-readable verdict
    hard: bool = False                    # True = non-healing rug signature → session-ban worthy
    detail: dict = field(default_factory=dict)   # raw facts, for audit + later threshold tuning


async def _onchain_authority(client: httpx.AsyncClient, mint: str) -> dict:
    """Read the SPL mint account on-chain → {mint_authority, freeze_authority, resolved, owner}.

    `*_authority` is None when REVOKED (the safe state). `resolved` is False when the RPC
    failed or the account couldn't be parsed as a standard SPL mint — the caller fails closed
    on that for gems. Never raises."""
    out = {"mint_authority": None, "freeze_authority": None, "resolved": False, "owner": None}
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        }
        data = await helius.rpc_post(client, payload, timeout=8)
        value = (data or {}).get("result", {}).get("value") or {}
        owner = value.get("owner")
        out["owner"] = owner
        if owner not in (_TOKEN_PROGRAM, _TOKEN_2022_PROGRAM):
            return out  # not a standard SPL mint → unresolved
        info = value.get("data", {}).get("parsed", {}).get("info")
        if not isinstance(info, dict):
            return out  # base64 / unparsed → unresolved
        # SPL exposes these as the authority pubkey string, or null/absent when revoked.
        out["mint_authority"] = info.get("mintAuthority")
        out["freeze_authority"] = info.get("freezeAuthority")
        out["resolved"] = True
    except Exception:
        pass
    return out


async def _rugcheck_report(client: httpx.AsyncClient, mint: str) -> dict:
    """Fetch the RugCheck full report → structured LP-lock %, top-holder %, risk names, rugged.

    `resolved` is False on 404 (token not in RugCheck — common for fresh gems) or any error,
    in which case the caller does NOT hard-block on LP/holders (best-effort). Never raises."""
    out = {"resolved": False, "rugged": False, "lp_locked_pct": None,
           "top_holder_pct": None, "risks": []}
    try:
        r = await client.get(_RUGCHECK_REPORT_URL.format(mint=mint), timeout=_RUGCHECK_TIMEOUT_S)
        if r.status_code == 404:
            return out
        r.raise_for_status()
        data = r.json() or {}
        out["resolved"] = True
        out["rugged"] = bool(data.get("rugged"))

        # risk names (for the substring fallback)
        risks = data.get("risks") or []
        out["risks"] = [str(x.get("name", "")) for x in risks if isinstance(x, dict)]

        # LP locked %: prefer the explicit markets[].lp.lpLockedPct (max across pools — the
        # token is sellable through the deepest locked route). Some payloads put it at top level.
        lp_pcts = []
        for mkt in (data.get("markets") or []):
            lp = (mkt.get("lp") or {}) if isinstance(mkt, dict) else {}
            v = lp.get("lpLockedPct")
            if isinstance(v, (int, float)):
                lp_pcts.append(float(v))
        if lp_pcts:
            out["lp_locked_pct"] = max(lp_pcts)
        elif isinstance(data.get("lpLockedPct"), (int, float)):
            out["lp_locked_pct"] = float(data["lpLockedPct"])

        # top-holder %: the largest holder that is NOT a known LP/locker account (insider == False
        # in RugCheck's topHolders, when flagged). Fall back to the largest holder overall.
        top = data.get("topHolders") or []
        best = None
        for h in top:
            if not isinstance(h, dict):
                continue
            pct = h.get("pct")
            if not isinstance(pct, (int, float)):
                continue
            # skip accounts RugCheck marks as the LP/AMM itself (not a dumpable holder)
            if h.get("insider") is False and (h.get("isLP") or h.get("lp")):
                continue
            best = float(pct) if best is None else max(best, float(pct))
        if best is not None:
            out["top_holder_pct"] = best
    except Exception:
        pass
    return out


def _risk_hit(risk_names: list[str], needles: tuple) -> bool:
    low = [n.lower() for n in risk_names]
    return any(any(nd in n for n in low) for nd in needles)


async def screen_token(
    client: httpx.AsyncClient,
    mint: str,
    *,
    fail_closed: bool = True,
    top_holder_max_pct: float = _DEFAULT_TOP_HOLDER_MAX_PCT,
    lp_locked_min_pct: float = _DEFAULT_LP_LOCKED_MIN_PCT,
    min_pool_age_s: float = _DEFAULT_MIN_POOL_AGE_S,
    pair_age_hours: float = 999.0,
) -> ScreenResult:
    """Pre-entry rug screen for a gem / price-action mint. Returns ScreenResult.

    The caller (main.py) only invokes this for non-static, non-deep_pool/brain_rule entries
    and runs it concurrently with the existing RugCheck + Jupiter quote gather."""
    import asyncio

    if mint in TRUSTED_MINTS:
        return ScreenResult(True, "trusted watchlist token")

    # pool-age gate (cheap, no I/O) — runs first so a brand-new pool is cut before any network.
    if min_pool_age_s and min_pool_age_s > 0:
        age_s = float(pair_age_hours) * 3600.0
        if age_s < min_pool_age_s:
            return ScreenResult(False, f"pool age {age_s:.0f}s < {min_pool_age_s:.0f}s floor",
                                hard=False, detail={"pool_age_s": age_s})

    auth, rc = await asyncio.gather(
        _onchain_authority(client, mint),
        _rugcheck_report(client, mint),
    )
    detail = {
        "mint_authority": auth["mint_authority"], "freeze_authority": auth["freeze_authority"],
        "authority_resolved": auth["resolved"], "owner": auth["owner"],
        "rc_resolved": rc["resolved"], "rugged": rc["rugged"],
        "lp_locked_pct": rc["lp_locked_pct"], "top_holder_pct": rc["top_holder_pct"],
        "risks": rc["risks"],
    }

    # ── 1. On-chain authority — the reliable core, hard-blocks (non-healing) ──────────
    if auth["resolved"]:
        if auth["freeze_authority"] is not None:
            return ScreenResult(False, "freeze authority enabled (can freeze your tokens)",
                                hard=True, detail=detail)
        if auth["mint_authority"] is not None:
            return ScreenResult(False, "mint authority enabled (infinite-dilution rug)",
                                hard=True, detail=detail)
    else:
        # authority unknown. RugCheck risk-name fallback before deciding to fail closed.
        if rc["resolved"] and _risk_hit(rc["risks"], _RISK_FREEZE):
            return ScreenResult(False, "freeze authority flagged (rugcheck)", hard=True, detail=detail)
        if rc["resolved"] and _risk_hit(rc["risks"], _RISK_MINT):
            return ScreenResult(False, "mint authority flagged (rugcheck)", hard=True, detail=detail)
        if fail_closed:
            # The unscreenable fresh gem — operator-chosen to skip rather than gamble. NOT a
            # hard signature (it may simply be too new) → no session-ban, re-eval next cycle.
            return ScreenResult(False, "authority unresolved (fail-closed gem)", hard=False, detail=detail)

    # ── 2. RugCheck enrichment — hard-block only when the data is PRESENT ─────────────
    if rc["resolved"]:
        if rc["rugged"]:
            return ScreenResult(False, "rugcheck: confirmed rugged", hard=True, detail=detail)
        if rc["lp_locked_pct"] is not None and rc["lp_locked_pct"] < lp_locked_min_pct:
            return ScreenResult(False,
                                f"LP locked {rc['lp_locked_pct']:.0f}% < {lp_locked_min_pct:.0f}% (pull risk)",
                                hard=True, detail=detail)
        if rc["lp_locked_pct"] is None and _risk_hit(rc["risks"], _RISK_LP):
            return ScreenResult(False, "LP unlocked flagged (rugcheck)", hard=True, detail=detail)
        if rc["top_holder_pct"] is not None and rc["top_holder_pct"] > top_holder_max_pct:
            return ScreenResult(False,
                                f"top holder {rc['top_holder_pct']:.0f}% > {top_holder_max_pct:.0f}% (dump risk)",
                                hard=True, detail=detail)

    return ScreenResult(True, "clean", detail=detail)
