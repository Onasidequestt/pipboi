#!/usr/bin/env python3
"""S98 — pre-entry rug-screen test suite (READ-ONLY, no network).

Verifies the left-tail screen blocks the realized rug signatures and passes clean
tokens, under the operator-chosen posture: gem-scope, fail-CLOSED on unresolved gems,
hard-block LP/holders only when RugCheck data is present.

Stubs rug_screen._onchain_authority / _rugcheck_report so nothing hits the chain or
RugCheck. Run: python3 test_rug_screen_s98.py
"""
import asyncio

import rug_screen as rs

_fails = []


def _check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def _auth(mint_auth=None, freeze_auth=None, resolved=True, owner=rs._TOKEN_PROGRAM):
    return {"mint_authority": mint_auth, "freeze_authority": freeze_auth,
            "resolved": resolved, "owner": owner}


def _rc(resolved=False, rugged=False, lp=None, top=None, risks=None):
    return {"resolved": resolved, "rugged": rugged, "lp_locked_pct": lp,
            "top_holder_pct": top, "risks": risks or []}


def _run(auth, rc, **kw):
    """Patch the two async fetchers and run screen_token to a ScreenResult."""
    async def _fake_auth(client, mint):
        return auth
    async def _fake_rc(client, mint):
        return rc
    orig_a, orig_r = rs._onchain_authority, rs._rugcheck_report
    rs._onchain_authority, rs._rugcheck_report = _fake_auth, _fake_rc
    try:
        return asyncio.get_event_loop().run_until_complete(
            rs.screen_token(None, "FakeMint1111111111111111111111111111111111", **kw)
        )
    finally:
        rs._onchain_authority, rs._rugcheck_report = orig_a, orig_r


print("\n1. On-chain authority — the reliable hard core")
r = _run(_auth(freeze_auth="SomeDevPubkey"), _rc())
_check("freeze authority enabled → BLOCK + hard", (not r.ok) and r.hard, r.reason)
r = _run(_auth(mint_auth="SomeDevPubkey"), _rc())
_check("mint authority enabled → BLOCK + hard", (not r.ok) and r.hard, r.reason)
r = _run(_auth(), _rc(resolved=True))
_check("both authorities revoked + rugcheck clean → PASS", r.ok, r.reason)

print("\n2. Fail-closed on the unscreenable fresh gem")
r = _run(_auth(resolved=False), _rc(resolved=False), fail_closed=True)
_check("authority unresolved + fail_closed → BLOCK, NOT hard (no ban)",
       (not r.ok) and (not r.hard), r.reason)
r = _run(_auth(resolved=False), _rc(resolved=False), fail_closed=False)
_check("authority unresolved + fail_open → PASS", r.ok, r.reason)
# unresolved on-chain but rugcheck names the danger → still a hard block even fail_open
r = _run(_auth(resolved=False), _rc(resolved=True, risks=["Freeze Authority still enabled"]),
         fail_closed=False)
_check("authority unresolved + rugcheck freeze-risk → BLOCK + hard", (not r.ok) and r.hard, r.reason)

print("\n3. RugCheck enrichment — hard-block only when data is PRESENT")
r = _run(_auth(), _rc(resolved=True, lp=10.0))
_check("LP locked 10% < 50% floor → BLOCK + hard", (not r.ok) and r.hard, r.reason)
r = _run(_auth(), _rc(resolved=True, lp=100.0))
_check("LP locked 100% → PASS", r.ok, r.reason)
r = _run(_auth(), _rc(resolved=True, top=40.0))
_check("top holder 40% > 25% → BLOCK + hard", (not r.ok) and r.hard, r.reason)
r = _run(_auth(), _rc(resolved=True, top=12.0, lp=90.0))
_check("top holder 12% + LP 90% → PASS", r.ok, r.reason)
r = _run(_auth(), _rc(resolved=True, rugged=True))
_check("rugcheck rugged → BLOCK + hard", (not r.ok) and r.hard, r.reason)
# data ABSENT (fresh gem, rugcheck 404) → no LP/holder block, authority carries it
r = _run(_auth(), _rc(resolved=False))
_check("authority clean + rugcheck 404 → PASS (best-effort, no false block)", r.ok, r.reason)
r = _run(_auth(), _rc(resolved=True, lp=None, risks=["Liquidity unlocked"]))
_check("LP pct missing but risk-name flags unlocked → BLOCK + hard", (not r.ok) and r.hard, r.reason)

print("\n4. Pool-age gate + trusted bypass")
r = _run(_auth(), _rc(resolved=True), min_pool_age_s=300, pair_age_hours=60 / 3600)  # 60s old
_check("pool 60s < 300s floor → BLOCK, NOT hard", (not r.ok) and (not r.hard), r.reason)
r = _run(_auth(), _rc(resolved=True), min_pool_age_s=300, pair_age_hours=600 / 3600)  # 600s old
_check("pool 600s > 300s floor → PASS", r.ok, r.reason)
# trusted watchlist short-circuit (no fetch needed)
res = asyncio.get_event_loop().run_until_complete(
    rs.screen_token(None, next(iter(rs.TRUSTED_MINTS)))
)
_check("trusted watchlist mint → PASS (short-circuit)", res.ok, res.reason)

print("\n5. Threshold knobs honored")
r = _run(_auth(), _rc(resolved=True, top=20.0), top_holder_max_pct=15.0)
_check("top 20% blocked when max lowered to 15%", (not r.ok) and r.hard, r.reason)
r = _run(_auth(), _rc(resolved=True, lp=60.0), lp_locked_min_pct=80.0)
_check("LP 60% blocked when min raised to 80%", (not r.ok) and r.hard, r.reason)

print("\n" + ("❌ FAILURES: " + ", ".join(_fails) if _fails else "✅ ALL PASS"))
raise SystemExit(1 if _fails else 0)
