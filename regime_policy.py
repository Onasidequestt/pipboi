#!/usr/bin/env python3
"""
regime_policy.py — TAXONOMY v2: the ONE play×regime policy surface (default-OFF, fail-open).

THE REWORK (operator /goal, supersedes the S127 depth-table v1 — backup *.bak.taxonomy.*):
  · REGIMES 5 → 3: RISK_ON / NEUTRAL / RISK_OFF on agg_vol_5m, cutoffs c1=$221,873 /
    c2=$281,604 derived from honest-EV separation (research/taxonomy/bands.json — honest EV
    falls MONOTONICALLY with volume, so RISK_ON = the quiet tape; the old euphoria/aggressive
    up-weighting was keyed on the inverted reading). ±10% hysteresis. The euphoria/aggressive/
    normal/sniper/dead names survive only in the legacy shim for historical reads.
  · PLAYS → 4 real cohorts: RUNNER (momentum_override lane, first-class) · SCALP (gem/
    momentum/relay/quick/bank/highconv/mean_reversion merged — fold evidence in
    research/taxonomy/cells.json) · DEEP (deep_pool/brain_rule/normal_slice — bookkeeping
    row, FROZEN) · DUST (shadow probes, never gated here).
  · ONE table (`regime_policy_table.json`): each 4×3 cell = {admit, size_mult, exit_family}.
    It absorbs _DEEP_POOL_SKIP_REGIMES, admit_guard force_skip, _REGIME_SIZE_MULT,
    _BLEED_TRIM_*, _NORMAL_DP_SIZE_MULT, _MOMENTUM_OVERRIDE_SIZE_MULT (see table _meta.absorbs).
  · EXITS key on PLAY + POOL DEPTH (EVOLVE-12), never tape regime: RIDE_TIGHT / RIDE_WIDE /
    BANK / SCALP_OUT. house_money + catastrophic_drain stay global overlays.

SAFETY MODEL (all by construction, tested in test_taxonomy.py):
  · DEFAULT-OFF — no `bots/botN/regime_policy.json` → every call no-ops → the legacy
    constants run byte-identical (bot2/3 = the control arm).
  · FAIL-OPEN — any exception/missing data → no-op (the S87/89/91 lockout class impossible).
  · DEEP IS FROZEN IN CODE — deep_pool/brain_rule/normal_slice are hardcoded: admission
    always blocked when ON (composing with the S121 allowlist + the dpgap observer gate),
    exits always RIDE_TIGHT (= today's behavior, no S110 exit change), and a tampered table
    cannot re-enable the row.
  · DUST is never blocked and never counted here (its own canary governs).
  · TRIM-ONLY sizing, clamped ≤1.0 (C²-mult only — THE ONE OPERATOR RULE: this module never
    reads or writes ev_sizing.json).
  · Lane flag resolves BEFORE play label (the S118 SPECIAL_LANES lesson).

Canary: bots/botN/regime_policy.json {"enabled": true}  (optional "layers": {"admission":
true, "sizing": true, "exits": true} for staged ops; missing layers = all on). 30s
hot-reload; `rm` = hot-disable.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
TABLE_PATH = BASE / "regime_policy_table.json"
BOTS_DIR = BASE / "bots"
SNAP_PATH = BASE / "shared_memory" / "discovery_snapshot.json"

_CFG_TTL = 30.0
_TABLE_TTL = 30.0
_SNAP_TTL = 30.0
_SNAP_STALE_S = 600.0

REGIMES = ("RISK_ON", "NEUTRAL", "RISK_OFF")
PLAYS = ("RUNNER", "SCALP", "DEEP", "DUST")

# Hardcoded — S110 + kill_criterion FAIL. A tampered table cannot reach these.
_DEEP_PLAYS = frozenset({"deep_pool", "brain_rule"})
_DEEP_FLAGS = ("normal_slice",)

# ⚠ NEVER seed a monotonic-keyed cache with 0.0 — time.monotonic() starts ~0 on this Mac
# (measured 0.007; bit dex_discovery S121 AND regime_policy S127). Always a −1e9 sentinel.
_cfg_cache: dict = {}
_table_cache = {"ts": -1e9, "table": None, "sha": None}
_snap_cache = {"ts": -1e9, "agg": None}
_band_state = {"band": None}   # per-process hysteresis memory


# ── loaders (fail-open) ─────────────────────────────────────────────────────────────────
def _table():
    now = time.monotonic()
    if _table_cache["table"] is not None and (now - _table_cache["ts"]) < _TABLE_TTL:
        return _table_cache["table"]
    try:
        raw = TABLE_PATH.read_bytes()
        t = json.loads(raw)
        sha = hashlib.sha256(raw).hexdigest()[:12]
        if not isinstance(t.get("grid"), dict) or not isinstance(t.get("regimes_v2"), dict):
            t = None
    except Exception:
        t, sha = None, None
    _table_cache.update(ts=now, table=t, sha=sha)
    return t


def table_hash():
    _table_cache["ts"] = -1e9
    _table()
    return _table_cache["sha"]


def _cfg(bot: int):
    now = time.monotonic()
    hit = _cfg_cache.get(bot)
    if hit and (now - hit[0]) < _CFG_TTL:
        return hit[1]
    cfg = None
    try:
        p = BOTS_DIR / f"bot{bot}" / "regime_policy.json"
        if p.exists():
            c = json.loads(p.read_text())
            if isinstance(c, dict) and c.get("enabled") is True:
                cfg = c
    except Exception:
        cfg = None
    _cfg_cache[bot] = (now, cfg)
    return cfg


def _layer_on(bot: int, layer: str) -> bool:
    try:
        cfg = _cfg(bot)
        if not cfg or _table() is None:
            return False
        layers = cfg.get("layers")
        if layers is None:
            return True
        return bool(layers.get(layer, False))
    except Exception:
        return False


def admission_on(bot: int) -> bool:
    return _layer_on(bot, "admission")


def sizing_on(bot: int) -> bool:
    return _layer_on(bot, "sizing")


def exits_on(bot: int) -> bool:
    return _layer_on(bot, "exits")


# ── regime v2 ───────────────────────────────────────────────────────────────────────────
def current_agg():
    """Global agg_vol_5m from the live sidecar snapshot (30s cache; None when stale)."""
    now = time.monotonic()
    if (now - _snap_cache["ts"]) < _SNAP_TTL:
        return _snap_cache["agg"]
    agg = None
    try:
        snap = json.loads(SNAP_PATH.read_text())
        ts = snap.get("ts")
        if isinstance(ts, str):
            age = time.time() - datetime.fromisoformat(ts).timestamp()
        elif ts:
            age = time.time() - float(ts)
        else:
            age = time.time() - SNAP_PATH.stat().st_mtime
        if age <= _SNAP_STALE_S:
            a = snap.get("agg_vol_5m")
            if a is not None:
                agg = float(a)
    except Exception:
        agg = None
    _snap_cache.update(ts=now, agg=agg)
    return agg


def _raw_band(agg, c1, c2):
    return "RISK_ON" if agg < c1 else ("NEUTRAL" if agg < c2 else "RISK_OFF")


def regime_v2(agg=None):
    """The 3-band regime with ±hysteresis (per-process memory). None when agg unknowable."""
    try:
        t = _table()
        if t is None:
            return None
        if agg is None:
            agg = current_agg()
        if agg is None:
            return _band_state["band"]            # hold last band through a snapshot gap
        r = t["regimes_v2"]
        c1, c2 = float(r["cuts"]["c1"]), float(r["cuts"]["c2"])
        h = float(r.get("hysteresis_frac", 0.10))
        prev = _band_state["band"]
        if prev is None:
            band = _raw_band(agg, c1, c2)
        else:
            # widen each boundary AWAY from the current band so small wobbles don't flip it
            idx = {"RISK_ON": 0, "NEUTRAL": 1, "RISK_OFF": 2}[prev]
            lo = c1 * (1 - h) if idx >= 1 else c1 * (1 + h)
            hi = c2 * (1 - h) if idx >= 2 else c2 * (1 + h)
            band = _raw_band(agg, lo, hi)
        _band_state["band"] = band
        return band
    except Exception:
        return None


def regime_v2_from_legacy(name):
    """Back-compat shim for historical reads (lossy on `normal` — see table shim_note)."""
    try:
        t = _table()
        if t is None or not name:
            return None
        return t["regimes_v2"].get("legacy_shim", {}).get(str(name))
    except Exception:
        return None


# ── play v2 ─────────────────────────────────────────────────────────────────────────────
def play_v2(play, momentum_override=False, normal_slice=False, dust_shadow=False):
    """The 4-cohort truth table. Flags resolve before play labels (lane-first, S118);
    DUST wins over everything (a dust probe is never a live cohort row)."""
    if dust_shadow:
        return "DUST"
    if play in _DEEP_PLAYS or normal_slice:
        return "DEEP"
    if momentum_override:
        return "RUNNER"
    return "SCALP"                                # gem/momentum/relay/quick/bank/highconv/
                                                  # mean_reversion/probe/force_fire + catch-all


def _cell(p2, r2):
    t = _table()
    if t is None:
        return None
    cell = (t.get("grid", {}).get(p2) or {}).get(r2 or "RISK_ON")
    return cell if isinstance(cell, dict) else None


# ── admission ───────────────────────────────────────────────────────────────────────────
def admission_skip(bot: int, play, lane, liq, dust=False):
    """(skip, why). Composes AFTER admit_guard/the S121 allowlist — can only ADD a skip.
    DEEP is frozen in code; DUST is never blocked (pass dust=True for a dust-tagged signal);
    missing data fails open."""
    try:
        if not admission_on(bot):
            return False, "off"
        p2 = play_v2(play,
                     momentum_override=(lane == "momentum_override"),
                     normal_slice=(lane == "normal_slice"),
                     dust_shadow=bool(dust))
        if p2 == "DUST":
            return False, "shadow"
        if p2 == "DEEP":
            return True, "DEEP frozen (S110 + kill_criterion FAIL — table cannot re-enable)"
        r2 = regime_v2() or "RISK_ON"
        cell = _cell(p2, r2)
        if cell is None:
            return False, "no-cell"
        # depth belt (S127 absorption): no-trade above the per-play max pool depth
        try:
            belt = (_table().get("depth_belts", {}).get(p2) or {}).get("max_liq")
            if belt is not None and liq is not None and float(liq) >= float(belt):
                return True, f"{p2} depth belt — liq ${float(liq):,.0f} ≥ ${float(belt):,.0f}"
        except Exception:
            pass
        if cell.get("admit") in ("skip", "frozen"):
            return True, f"{p2}×{r2} admit={cell.get('admit')} (table v{_table().get('_meta', {}).get('version')})"
        return False, f"{p2}×{r2} admit={cell.get('admit')}"
    except Exception:
        return False, "error-fail-open"


def observer_blocks(bot: int, play) -> bool:
    """The dpgap gate for the SEPARATE observer admission loops (deep_pool/brain_rule are
    admitted outside main.py's hook). True only when the canary is ON and the play is the
    frozen DEEP row. Cheap, no I/O beyond the cached canary/table reads, fail-open False."""
    try:
        return admission_on(bot) and (play in _DEEP_PLAYS)
    except Exception:
        return False


# ── sizing ──────────────────────────────────────────────────────────────────────────────
def size_mult(bot: int, play, lane, liq):
    """(mult ≤1.0, why). C²-trim only. Skip/frozen cells return ×1.0 (the admission layer
    owns blocking — sizing alone can never silently zero an entry). RUNNER's cell 0.5
    ABSORBS the legacy _MOMENTUM_OVERRIDE_SIZE_MULT (main.py skips the lane mult when ON)."""
    try:
        if not sizing_on(bot):
            return 1.0, "off"
        p2 = play_v2(play,
                     momentum_override=(lane == "momentum_override"),
                     normal_slice=(lane == "normal_slice"))
        if p2 in ("DUST", "DEEP"):
            return 1.0, p2.lower()
        r2 = regime_v2() or "RISK_ON"
        cell = _cell(p2, r2)
        if cell is None:
            return 1.0, "no-cell"
        if cell.get("admit") in ("skip", "frozen"):
            return 1.0, "admission-layer-owns"
        mult = max(0.05, min(1.0, float(cell.get("size_mult", 1.0))))
        return mult, f"{p2}×{r2}"
    except Exception:
        return 1.0, "error-fail-open"


def tape_mult(bot: int):
    """Replaces _REGIME_SIZE_MULT when sizing is ON — retired to 1.0 (the 5-band up/down
    weighting was keyed on the inverted axis). Kept as a hook for a future evidenced value."""
    try:
        if not sizing_on(bot):
            return 1.0
        t = _table()
        return max(0.05, min(1.0, float(t.get("tape_mult", 1.0)))) if t else 1.0
    except Exception:
        return 1.0


# ── exits (play + pool depth — EVOLVE-12; NEVER tape regime) ────────────────────────────
def exit_family(pos: dict, bot: int):
    """Resolve the named exit family for a position, or None (= legacy flag path, fail-open).
    Byte-equivalent to the legacy canary-flag outcomes on a canary-ON bot (tested):
      DEEP → RIDE_TIGHT · RUNNER → RIDE_WIDE · SCALP micro/<50k or the momentum-gem
      signature → RIDE_WIDE · bank-param SCALP → BANK · else SCALP_OUT."""
    try:
        if not exits_on(bot):
            return None
        t = _table()
        if t is None:
            return None
        q = t.get("exit_qualifiers", {})
        p2 = play_v2(pos.get("insane_tier") or pos.get("play"),
                     momentum_override=bool(pos.get("momentum_override")),
                     normal_slice=bool(pos.get("normal_slice")),
                     dust_shadow=bool(pos.get("dust_shadow")))
        if p2 == "DEEP":
            return "RIDE_TIGHT"
        if p2 == "RUNNER":
            return "RIDE_WIDE"
        liq = pos.get("entry_liq", 0.0) or 0.0
        if 0.0 < liq < float(q.get("vae_max_liq", 50_000)):
            return "RIDE_WIDE"
        if (float(q.get("mg_liq_lo", 50_000)) <= liq < float(q.get("mg_liq_hi", 250_000))
                and (pos.get("insane_tier") in tuple(q.get("mg_tiers", ("gem", "momentum"))))
                and (pos.get("momentum_at_entry", 0) or 0) >= float(q.get("mg_mom_min", 3.0))):
            return "RIDE_WIDE"
        if (pos.get("insane_tier") or pos.get("play")) in tuple(q.get("bank_plays", ("bank",))):
            return "BANK"
        return "SCALP_OUT"
    except Exception:
        return None


# ── dual-tag migration helpers ──────────────────────────────────────────────────────────
def tags(play, momentum_override=False, normal_slice=False, dust_shadow=False,
         legacy_regime=None, agg_at_open=None):
    """{play_v2, regime_v2} for a close row. regime from the exact agg-at-open when stamped,
    else the lossy name shim. Pure bookkeeping — additive fields, no behavior."""
    try:
        t = _table()
        r2 = None
        if t is not None:
            if agg_at_open is not None:
                c = t["regimes_v2"]["cuts"]
                r2 = _raw_band(float(agg_at_open), float(c["c1"]), float(c["c2"]))
            elif legacy_regime:
                r2 = regime_v2_from_legacy(legacy_regime)
        return {"play_v2": play_v2(play, momentum_override, normal_slice, dust_shadow),
                "regime_v2": r2}
    except Exception:
        return {"play_v2": None, "regime_v2": None}


if __name__ == "__main__":
    print(f"table v2 sha={table_hash()}")
    print(f"agg now: {current_agg()} → regime_v2 {regime_v2()}")
    for b in (1, 2, 3):
        print(f"bot{b}: admission={admission_on(b)} sizing={sizing_on(b)} exits={exits_on(b)}")
    for play, lane, liq in (("gem", None, 50_000), ("gem", "momentum_override", 8_000),
                            ("gem", "momentum_override", 500_000), ("deep_pool", None, 100_000),
                            ("bank", None, 60_000)):
        print(f"  {play}/{lane}/{liq}: skip={admission_skip(1, play, lane, liq)} "
              f"mult={size_mult(1, play, lane, liq)}")
    print(f"  shim: {[ (n, regime_v2_from_legacy(n)) for n in ('euphoria','aggressive','normal','sniper','dead') ]}")
