"""
Discovery Sidecar — single consolidated discovery process for the entire fleet.

Eliminates the "redundancy tax" of three bots independently polling the same APIs:
  Before:  3 bots × (GeckoTerminal + Birdeye + DexScreener + BondingCurve WS)
  After:   1 sidecar × all sources → 3 bots read a shared snapshot

Each bot's observer.py still runs its own score_tokens() pass with its own
ValidationProfile, balance-adjusted threshold, and WR safety gate — bot-specific
execution logic is untouched.

Transport: Unix Domain Socket (ipc_layer.SOCKET_PATH) via DiscoveryServer.
Fallback:  shared_memory/discovery_snapshot.json (atomic write, readable by bots
           when the UDS server is momentarily unavailable).

Graceful degradation: if this process is not running, bots fall back to standalone
polling exactly as before — there is no hard dependency.

Usage:
    python3 ./pipboi discovery_service

run.sh starts this before the dashboard so bots receive a populated snapshot
on their first cycle.  The process runs until the terminal/fleet shuts down.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import httpx

import bonding_curve
import discovery as _disc_mod
import dexscreener
import dex_discovery   # S121 V2: $0 DexScreener candidate feed (canary-gated, fail-soft) — un-starves the stream
import rugcheck_score  # S121 V2: free RugCheck risk score as a candidate FEATURE (canary-gated, no admission change)
import bitquery_service # SOL-STRATEGY-2026: $0 Bitquery v2 GraphQL real-time trade-flow feed (canary-gated, fail-soft)
from config import (
    BASE_MINT,
    WATCHLIST_HIGH, WATCHLIST_MID, WATCHLIST_LOW,
    DISCOVERY_PAGES_BY_MODE, DISCOVERY_MAX_BY_MODE,
)
from ipc_layer import DiscoveryServer, SOCKET_PATH as _SOCKET_PATH

# ── Timing ────────────────────────────────────────────────────────────────────
_POLL_INTERVAL_S = 10.0   # 10s — 3× faster than individual bot ~30s cycles
_DISCOVERY_EVERY = 3      # re-run full discovery every N polls (~every 30s, matches bot cadence)

# Full static universe: superset of all cap tiers.  Each bot filters to its own tier.
_STATIC_ALL: list[str] = list(dict.fromkeys(WATCHLIST_LOW + WATCHLIST_MID + WATCHLIST_HIGH))

# INSANE-mode discovery parameters (widest net; per-bot observers narrow by cap)
_INSANE_CFG = DISCOVERY_PAGES_BY_MODE.get(
    "insane", {"trending": 8, "new_pools": 2, "deep": True}
)
_INSANE_DMAX = DISCOVERY_MAX_BY_MODE.get("insane", 120)
_DEX_SLOT_FRACTION = 0.40   # S121 V2: guaranteed share of the candidate cap reserved for DexScreener
                            # sellable pools (mirrors _BC_SLOT_FRACTION) — un-starves without flooding.
_BQ_SLOT_FRACTION  = 0.30   # SOL-STRATEGY-2026: bounded share reserved for Bitquery real-time buy-pressure
                            # runners (mirrors the dex reserve) — un-starves the runner lane without flooding.
_RC_ENRICH_PER_CYCLE = 8    # S121 V2: bounded NEW RugCheck fetches/cycle (cached ones re-stamp free) → stays free.

_SNAPSHOT_PATH        = Path("shared_memory/discovery_snapshot.json")
_HEARTBEAT_PATH       = Path("shared_memory/sidecar_heartbeat.json")
_LOG_PATH             = Path("logs/sidecar.log")
_HEARTBEAT_INTERVAL_S = 5.0
_MAX_BACKOFF_S        = 120.0


# ── State container ───────────────────────────────────────────────────────────

class _State:
    """Mutable, single-writer container.  Updated by the sidecar loop, read by the server."""

    def __init__(self) -> None:
        self.market_data:     dict  = {}
        self.sol_price:       float = 150.0
        self.agg_vol_5m:      float = 0.0
        self.bc_hot:          list  = []
        self.discovery_pool:  list  = []
        self.gecko_pool_data: dict  = {}
        self.liq_velocity:    dict  = {}
        self._liq_hist:       dict  = {}  # mint → deque(maxlen=5)

    # S86: liq-velocity sanitization. The fractional change (h[-1]-h[0])/h[0] explodes when
    # h[0] (the baseline depth ~5 cycles ago) is near zero — a GENESIS/fresh pool (~$50 → deep
    # in minutes). That produced absurd values (observed: +564,335 = +56M%) which the observer's
    # `_filling = lvel>0.01` test (no upper bound) falsely read as "accumulation," admitting the
    # −EV fresh cohort as a deep_pool filling edge → ghost risk. Two guards: (1) require a real
    # baseline (≥$2k — a genuine deep pool is far above this; only genesis pools are below, so
    # the true deep_pool_filling edge is untouched); (2) winsorize to ±100%/window (a deep pool
    # doesn't 100× in ~50s — bigger is corruption). Pure data-correctness; revert: git checkout.
    _LIQ_VEL_MIN_BASE = 2_000.0
    _LIQ_VEL_CLAMP    = 1.0

    def refresh_liq_velocity(self) -> None:
        """Recompute fractional liq velocity for every mint present in market_data."""
        for mint, mdata in self.market_data.items():
            liq = mdata.get("liquidity_usd", 0.0)
            if mint not in self._liq_hist:
                self._liq_hist[mint] = deque(maxlen=5)
            h = self._liq_hist[mint]
            h.append(liq)
            if len(h) >= 2 and h[0] >= self._LIQ_VEL_MIN_BASE:
                v = (h[-1] - h[0]) / h[0]
                self.liq_velocity[mint] = round(max(-1.0, min(self._LIQ_VEL_CLAMP, v)), 4)
            else:
                self.liq_velocity[mint] = 0.0

    def to_snapshot(self) -> dict:
        return {
            "ts":             datetime.now(timezone.utc).isoformat(),
            "sol_price":      self.sol_price,
            "agg_vol_5m":     round(self.agg_vol_5m),
            "market_data":    self.market_data,
            "bc_hot":         self.bc_hot,
            "discovery_pool": self.discovery_pool,
            "liq_velocity":   self.liq_velocity,
        }


# ── Reliability helpers ───────────────────────────────────────────────────────

def _write_heartbeat(version: int) -> None:
    """Atomically write a heartbeat file so bots can detect a stalled sidecar."""
    tmp = str(_HEARTBEAT_PATH) + ".tmp"
    try:
        _HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump({
                "ts":      datetime.now(timezone.utc).isoformat(),
                "unix_ts": time.time(),
                "pid":     os.getpid(),
                "version": version,
            }, fh, separators=(",", ":"))
        os.replace(tmp, str(_HEARTBEAT_PATH))
    except Exception as e:
        print(f"[Sidecar] heartbeat write error: {e}", flush=True)


def _log_crash(exc: BaseException) -> None:
    """Append a crash report with full traceback to logs/sidecar.log."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(_LOG_PATH, "a") as fh:
            fh.write(f"\n[{ts}] CRASH: {exc}\n")
            fh.write(traceback.format_exc())
            fh.write("\n")
    except Exception:
        pass


async def _heartbeat_loop(server: DiscoveryServer) -> None:
    """Write a heartbeat every 5 seconds, independent of the 10s poll cycle."""
    while True:
        _write_heartbeat(server.version)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)


# ── Poll helpers ──────────────────────────────────────────────────────────────

async def _poll_discovery(client: httpx.AsyncClient, state: _State) -> None:
    discovered, gecko_data = await _disc_mod.discover_trending_tokens(
        client,
        exclude=[BASE_MINT],
        max_tokens=_INSANE_DMAX,
        pages_trending=_INSANE_CFG["trending"],
        pages_new_pools=_INSANE_CFG["new_pools"],
        include_deep=_INSANE_CFG["deep"],
        bonding_curve_mints=state.bc_hot,
    )
    # S89: the deep/trending+gecko fetch FAILS ~⅔ of the time it runs (Birdeye HTTP 400 /
    # 429 rate-limit) and returns EMPTY gecko pricing + only the bonding-curve mints. The old
    # unconditional assignment then CLOBBERED the last-good gecko_pool_data with {} and dropped
    # the deep/trending mints → those tokens went unscoreable on the failed cycles ("0 with
    # gecko pricing", market_data collapsing to ~10 mints), starving admission. Guard it: a
    # fetch that returns no gecko data is treated as a transient failure — keep the last-good
    # pricing AND its mints so deep/trending tokens stay scoreable until the next good fetch,
    # while still folding in the fresh bonding-curve mints from `discovered`.
    if gecko_data:
        state.discovery_pool  = discovered
        state.gecko_pool_data = gecko_data
    else:
        # S91: reserve room for the preserved sellable gecko mints. `discovered` on a
        # failed cycle is BC-only and now routinely fills the whole cap — appending the
        # last-good gecko keys AFTER it (then truncating to _INSANE_DMAX) sliced them
        # right back out, re-starving admission on every empty-fetch cycle. Cap BC's
        # share (mirrors discover_trending_tokens) so the preserved sellable pools
        # survive, while fresh bonding-curve whale signals keep a bounded share.
        gecko_keys = list(state.gecko_pool_data.keys())
        bc_only    = [m for m in discovered if m not in state.gecko_pool_data]
        bc_slots   = int(_INSANE_DMAX * _disc_mod._BC_SLOT_FRACTION)
        bc_take    = bc_only[:bc_slots]
        non_bc     = _INSANE_DMAX - len(bc_take)
        pool       = list(dict.fromkeys(bc_take + gecko_keys[:non_bc]))
        if len(pool) < _INSANE_DMAX:                       # back-fill unused capacity
            leftover = bc_only[bc_slots:] + gecko_keys[non_bc:]
            pool += [m for m in leftover if m not in pool][:_INSANE_DMAX - len(pool)]
        state.discovery_pool = pool[:_INSANE_DMAX]
        # state.gecko_pool_data left intact (preserved across the failure)

    # ── S121 V2: DexScreener reserve-slotted merge (the $0 un-starve; mirrors the S91 BC reserve). ──
    # DexScreener is ~10× gecko's free budget and carries the deep sellable pools gecko's 429-throttle
    # slices off (S91/S119 A/B: +42 NEW sellable). Wired ADDITIVELY behind the per-bot dex_discovery
    # canary: dex records fold into gecko_pool_data (gap-filled into market_data downstream) and claim a
    # BOUNDED reserved slot share at the head of discovery_pool, so neither the BC flood nor truncation
    # can crowd them out. Bounded-reserve, NOT unbounded-prepend (the S91 starvation bug). Fail-soft:
    # any error → no change, discovery never stalls. No-op without bots/botN/dex_discovery.json.
    if dex_discovery.dex_discovery_on():
        try:
            dx_mints, dx_pool = await dex_discovery.fetch_candidates(client)
        except Exception:
            dx_mints, dx_pool = [], {}
        if dx_pool:
            for m, rec in dx_pool.items():
                state.gecko_pool_data.setdefault(m, rec)   # additive — existing gecko/trending wins ties
            dex_slots = int(_INSANE_DMAX * _DEX_SLOT_FRACTION)
            dex_take  = [m for m in dx_mints if m in state.gecko_pool_data][:dex_slots]
            keep      = max(0, _INSANE_DMAX - len(dex_take))
            state.discovery_pool = list(
                dict.fromkeys(dex_take + state.discovery_pool[:keep])
            )[:_INSANE_DMAX]

    # ── SOL-STRATEGY-2026: Bitquery v2 GraphQL reserve-slotted merge (the $0 real-time un-starve). ──
    # Bitquery surfaces the freshest actively-BOUGHT mints (ranked by real-time buy-pressure) the moment
    # they start trading — ~2× GeckoTerminal's coverage in a ~2s window at ~0s lag — directly feeding the
    # runner lane the candidates it was starved of (the "whale traded 1-of-259" gap). EXACT mirror of the
    # DexScreener reserve above: Bitquery records fold into gecko_pool_data (DexScreener enriches liquidity/
    # momentum downstream — a mint it can't price stays score-0, safe) + claim a BOUNDED reserved head-share
    # of discovery_pool. Fail-soft; no-op without bots/botN/bitquery.json. THE ONE OPERATOR RULE + S110 +
    # the S121 allowlist all stay in force — this is a candidate FEED, not a policy/admission/sizing change.
    if bitquery_service.bitquery_on():
        try:
            bq_mints, bq_pool = await bitquery_service.fetch_candidates(client)
        except Exception:
            bq_mints, bq_pool = [], {}
        if bq_pool:
            for m, rec in bq_pool.items():
                state.gecko_pool_data.setdefault(m, rec)   # additive — existing gecko/dex/trending wins ties
            bq_slots = int(_INSANE_DMAX * _BQ_SLOT_FRACTION)
            bq_take  = [m for m in bq_mints if m in state.gecko_pool_data][:bq_slots]
            keep     = max(0, _INSANE_DMAX - len(bq_take))
            state.discovery_pool = list(
                dict.fromkeys(bq_take + state.discovery_pool[:keep])
            )[:_INSANE_DMAX]


async def _poll_market_data(client: httpx.AsyncClient, state: _State) -> None:
    all_mints = list(dict.fromkeys(_STATIC_ALL + state.discovery_pool))
    market_data, sol_price = await asyncio.gather(
        dexscreener.get_watchlist_data(client, all_mints),
        dexscreener.get_sol_price(client),
    )
    # Gap-fill: tokens too new for DexScreener still get GeckoTerminal pricing
    for mint, gdata in state.gecko_pool_data.items():
        if mint in all_mints and mint not in market_data:
            market_data[mint] = gdata
    # ── S121 V2: RugCheck risk score as a candidate FEATURE (no admission change). Bounded so it stays
    # free: re-stamp every candidate that is already cached (cheap, no HTTP) + fetch a small NEW sample
    # per cycle. The rc_* fields ride the market_data record into discovery_snapshot.json → signal_lab
    # logs them into forward_obs for the GA (Phase 7). Fail-soft; no-op without bots/botN/rugcheck_score.json.
    if rugcheck_score.rugcheck_score_on():
        _fresh = 0
        for _m, _rec in market_data.items():
            if not isinstance(_rec, dict) or (_rec.get("price_usd") or 0) <= 0:
                continue
            _rc = rugcheck_score.cached_score(_m)
            if _rc is None and _fresh < _RC_ENRICH_PER_CYCLE:
                try:
                    _rc = await rugcheck_score.risk_score(client, _m)
                except Exception:
                    _rc = None
                _fresh += 1
            if _rc:
                _rec["rc_score_norm"]    = _rc["score_normalised"]
                _rec["rc_lp_locked_pct"] = _rc["lp_locked_pct"]
                _rec["rc_n_risks"]       = _rc["n_risks"]

    state.market_data = market_data
    state.sol_price   = sol_price
    # S84 Fix 3 — ROBUST regime aggregate (was: raw sum, dominated ~80% by one mega-cap like
    # RAY, so the regime thrashed dead↔aggressive on a single token's 5m volume blip). Two
    # de-noisers: (1) winsorize each token's contribution at a ceiling so the metric reflects
    # market BREADTH, not one whale; (2) EMA-smooth across polls so a single-poll spike/crater
    # can't flip the regime. Bands (110k/280k/600k) unchanged → now require sustained breadth.
    _PER_TOKEN_VOL_CAP = 40_000.0   # max any single token adds to the regime aggregate
    _AGG_EMA_ALPHA     = 0.3        # weight on the newest poll (≈3-poll time constant)
    _raw_agg = sum(min(d.get("volume_5m", 0.0) or 0.0, _PER_TOKEN_VOL_CAP)
                   for d in market_data.values())
    state.agg_vol_5m  = (
        _AGG_EMA_ALPHA * _raw_agg + (1 - _AGG_EMA_ALPHA) * state.agg_vol_5m
        if state.agg_vol_5m > 0 else _raw_agg
    )


def _write_fallback_file(snap: dict) -> None:
    tmp = str(_SNAPSHOT_PATH) + ".tmp"
    try:
        _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(snap, fh, separators=(",", ":"))
        os.replace(tmp, str(_SNAPSHOT_PATH))
    except Exception as e:
        print(f"[Sidecar] snapshot file error: {e}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_sidecar() -> None:
    # Explicit socket unlink at startup — prevents "Address already in use" when
    # the process restarts and the old socket file was not cleaned up on crash.
    if os.path.exists(_SOCKET_PATH):
        try:
            os.remove(_SOCKET_PATH)
        except OSError:
            pass

    state  = _State()
    server = DiscoveryServer()

    # BondingCurve: one WebSocket for the entire fleet
    bonding_curve.start()
    print("[Sidecar] BondingCurve WebSocket started", flush=True)

    # IPC server + heartbeat: run forever in the background
    asyncio.create_task(server.serve())
    asyncio.create_task(_heartbeat_loop(server))

    async with httpx.AsyncClient(timeout=10.0) as client:
        iteration = 0
        while True:
            t0 = time.monotonic()
            try:
                # Always refresh BC hot list (in-memory, near-zero cost)
                state.bc_hot = bonding_curve.get_hot_mints()

                # Discovery re-runs every _DISCOVERY_EVERY polls
                if iteration % _DISCOVERY_EVERY == 0:
                    await _poll_discovery(client, state)

                await _poll_market_data(client, state)
                state.refresh_liq_velocity()

                snap = state.to_snapshot()
                server.update(snap)
                _write_fallback_file(snap)

                elapsed = time.monotonic() - t0
                print(
                    f"[Sidecar] v{server.version:>4}  "
                    f"disc={len(state.discovery_pool):>3}  "
                    f"mints={len(state.market_data):>3}  "
                    f"bc={len(state.bc_hot):>3}  "
                    f"agg=${state.agg_vol_5m/1000:.0f}k  "
                    f"sol=${state.sol_price:.0f}  "
                    f"{elapsed:.1f}s",
                    flush=True,
                )
            except Exception as exc:
                print(f"[Sidecar] ⚠ poll error: {exc}", flush=True)

            await asyncio.sleep(max(0.0, _POLL_INTERVAL_S - (time.monotonic() - t0)))
            iteration += 1


if __name__ == "__main__":
    # Supervised restart loop — catches any uncaught exception from run_sidecar()
    # and relaunches with exponential backoff instead of exiting the process.
    # Backoff resets to 5s when the process ran cleanly for > 60s (transient failure).
    # KeyboardInterrupt (Ctrl+C / SIGINT) exits cleanly without restarting.
    _backoff = 5.0
    while True:
        _t_start = time.monotonic()
        try:
            asyncio.run(run_sidecar())
        except KeyboardInterrupt:
            print("[Sidecar] interrupted, exiting", flush=True)
            break
        except Exception as _exc:
            _ran_for = time.monotonic() - _t_start
            print(
                f"[Sidecar] ⚠ CRASH after {_ran_for:.0f}s: {_exc}"
                f" — restarting in {_backoff:.0f}s",
                flush=True,
            )
            _log_crash(_exc)
            time.sleep(_backoff)
            _backoff = 5.0 if _ran_for > 60.0 else min(_backoff * 2, _MAX_BACKOFF_S)
