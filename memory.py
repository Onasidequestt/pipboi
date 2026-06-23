"""
Trade memory — persists per-token stats and computes confidence scores.
The bot learns from every trade outcome and adjusts future behavior.
Fleet-shared: all bots read/write the same trade_memory.json so a loss on Bot2
immediately lowers confidence for Bot1 and Bot3 on the same token.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Shared memory path — one file for the whole fleet ────────────────────────
_SHARED_DIR = Path(__file__).parent / "shared_memory"
_SHARED_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_PATH   = _SHARED_DIR / "trade_memory.json"
_PENALTY_PATH = _SHARED_DIR / "execution_penalty.json"
_BREAKEVEN_USD = 0.01   # S87: |pnl_usd| below this = break-even (neutral, not win/loss)
# S89: a $0.01 ABSOLUTE band was too tight for the S87 dead-pool flat-exit cohort. On ~$1
# positions those exits land at ±0.2–0.4% but only ±$0.01–0.04 in USD — OUTSIDE the band — so
# they booked as wins/losses, splitting ~37% with a fee-driven negative tilt and eroding every
# token's win_rate below the confidence floors (the recurring trade-lockout). A flat exit is
# flat by PERCENT regardless of size — judge break-even on |pnl_pct| when available, USD fallback.
_BREAKEVEN_PCT = 1.0    # |pnl_pct| below this (%) = break-even (≈ round-trip fee+slippage band)

# In-process cache — updated by every write so reads in the same process don't hit disk.
_penalty_cache: Optional[dict] = None


def _migrate_per_bot_memories() -> None:
    """One-time migration: merge bots/botN/trade_memory.json into shared_memory/.
    Runs on first startup after this change. Subsequent runs are no-ops (.migrated marker).
    Merge strategy: sum trades/wins/losses, weighted-average momentum, latest timestamps,
    most conservative ban and consecutive_losses.
    """
    marker = _SHARED_DIR / ".migrated"
    if marker.exists():
        return

    merged: dict = {}
    for bot_id in range(1, 5):
        per_bot = Path(__file__).parent / f"bots/bot{bot_id}/trade_memory.json"
        if not per_bot.exists():
            continue
        try:
            bot_data: dict = json.loads(per_bot.read_text())
        except Exception:
            continue
        for mint, s in bot_data.items():
            if mint not in merged:
                merged[mint] = {k: v for k, v in s.items()}
                continue
            m = merged[mint]
            old_wins   = m.get("wins", 0)
            old_losses = m.get("losses", 0)
            new_wins   = s.get("wins", 0)
            new_losses = s.get("losses", 0)

            m["trades"] = m.get("trades", 0) + s.get("trades", 0)
            m["wins"]   = old_wins + new_wins
            m["losses"] = old_losses + new_losses
            m["total_pnl"] = round(m.get("total_pnl", 0.0) + s.get("total_pnl", 0.0), 4)

            # Weighted average of momentum values
            for avg_key, count_key in (("avg_win_momentum", old_wins), ("avg_loss_momentum", old_losses)):
                old_n = count_key
                new_n = new_wins if avg_key == "avg_win_momentum" else new_losses
                old_v = m.get(avg_key, 0.0)
                new_v = s.get(avg_key, 0.0)
                if old_n > 0 and new_n > 0:
                    m[avg_key] = round((old_v * old_n + new_v * new_n) / (old_n + new_n), 4)
                elif new_n > 0:
                    m[avg_key] = new_v

            if s.get("wins", 0) > 0 and s.get("avg_win_volume_5m"):
                old_v5 = m.get("avg_win_volume_5m", 0.0)
                new_v5 = s.get("avg_win_volume_5m", 0.0)
                if old_wins > 0 and new_wins > 0:
                    m["avg_win_volume_5m"] = round((old_v5 * old_wins + new_v5 * new_wins) / (old_wins + new_wins), 2)
                else:
                    m["avg_win_volume_5m"] = new_v5

            # Most conservative: highest consecutive_losses
            m["consecutive_losses"] = max(
                m.get("consecutive_losses", 0), s.get("consecutive_losses", 0)
            )
            # Most recent timestamps
            for ts_key in ("last_win_at", "last_loss_at"):
                ts_new = s.get(ts_key)
                ts_old = m.get(ts_key)
                if ts_new and (not ts_old or ts_new > ts_old):
                    m[ts_key] = ts_new
            # Latest (most conservative) ban
            ban_new = s.get("banned_until")
            ban_old = m.get("banned_until")
            if ban_new and (not ban_old or ban_new > ban_old):
                m["banned_until"] = ban_new

    _tmp = MEMORY_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps(merged, indent=2))
    os.replace(_tmp, MEMORY_PATH)
    marker.touch()
    print(f"[Memory] Migrated {len(merged)} token(s) from per-bot files → shared_memory/trade_memory.json")


_migrate_per_bot_memories()

# In-memory cache — trade_memory.json is read on every confidence_score() call.
# With 96 tokens in the watchlist, that's 200+ disk reads per 30s cycle.
# Cache is invalidated immediately on every write so reads always reflect the truth.
_cache: dict = {}
_cache_loaded: bool = False


def _load() -> dict:
    global _cache, _cache_loaded
    if not _cache_loaded:
        _cache = json.loads(MEMORY_PATH.read_text()) if MEMORY_PATH.exists() else {}
        _cache_loaded = True
    return _cache


def _save(data: dict) -> None:
    global _cache, _cache_loaded
    # Atomic write so a crash mid-write never corrupts the shared fleet memory file.
    # os.replace() is POSIX-atomic as long as src and dst are on the same filesystem.
    _tmp = MEMORY_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps(data, indent=2))
    os.replace(_tmp, MEMORY_PATH)
    _cache = data          # keep cache in sync so next read is still fast
    _cache_loaded = True


def record_trade(mint: str, pnl_usd: float, momentum_at_entry: float, volume_spike: bool, volume_5m_at_entry: float = 0.0, pnl_pct: Optional[float] = None) -> None:
    """Record a completed trade outcome for a token.

    S89: pass pnl_pct (the % move, size-independent) so break-even is judged on the relative
    move. The flat dead-pool exits (±0.2–0.4%) are then NEUTRAL on any position size instead
    of slipping past the $0.01 USD band and poisoning win_rate. USD band stays as the fallback
    when pnl_pct is unavailable (e.g. legacy callers).
    """
    data = _load()
    if mint not in data:
        data[mint] = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "consecutive_losses": 0,
            "avg_win_momentum": 0.0,
            "avg_loss_momentum": 0.0,
        }

    s = data[mint]
    s["trades"] += 1
    s["total_pnl"] = round(s["total_pnl"] + pnl_usd, 4)

    # S87/S89 FIX: break-even exits (e.g. the flat dead-pool guard, pnl ≈ 0) are NEUTRAL —
    # NOT losses. Counting them as losses crashed both per-token confidence and fleet WR and
    # deadlocked the whole fleet. A break-even neither wins nor loses, so it touches neither
    # counter nor the consecutive-loss streak. S89: judge "flat" by PERCENT (|pnl_pct| <
    # _BREAKEVEN_PCT) when we know it — size-independent so dead-pool exits are neutral on any
    # position — and fall back to the absolute USD band only when pnl_pct wasn't passed.
    if pnl_pct is not None:
        _is_breakeven = abs(pnl_pct) < _BREAKEVEN_PCT
    else:
        _is_breakeven = abs(pnl_usd) <= _BREAKEVEN_USD

    if _is_breakeven:
        # Break-even: neutral. Track count for visibility; do NOT touch wins/losses/streak.
        s["breakeven"] = s.get("breakeven", 0) + 1
        s["last_breakeven_at"] = datetime.now(timezone.utc).isoformat()
    elif pnl_usd > 0:
        s["wins"] += 1
        s["consecutive_losses"] = 0
        s["last_win_at"] = datetime.now(timezone.utc).isoformat()
        n = s["wins"]
        s["avg_win_momentum"] = round(
            (s["avg_win_momentum"] * (n - 1) + momentum_at_entry) / n, 4
        )
        if volume_5m_at_entry > 0:
            s["avg_win_volume_5m"] = round(
                (s.get("avg_win_volume_5m", 0.0) * (n - 1) + volume_5m_at_entry) / n, 2
            )
    else:
        s["losses"] += 1
        s["consecutive_losses"] += 1
        s["last_loss_at"] = datetime.now(timezone.utc).isoformat()
        n = s["losses"]
        s["avg_loss_momentum"] = round(
            (s["avg_loss_momentum"] * (n - 1) + momentum_at_entry) / n, 4
        )

    _save(data)
    print(
        f"[Memory] {mint[:8]}... | trades={s['trades']} wins={s['wins']} "
        f"losses={s['losses']} pnl=${s['total_pnl']:.4f}"
    )


# Ban duration scales with consecutive losses — the more it keeps losing, the longer it sits out
# 2 consecutive → 30m, 3 → 60m, 4 → 4h, 5+ → 8h
_BAN_SCALE_MINUTES = [30, 60, 240, 480]

def ban_token(mint: str, consecutive_losses: int = 2) -> None:
    """Temporarily ban a token after consecutive losses. Duration scales with loss streak."""
    idx     = max(0, min(consecutive_losses - 2, len(_BAN_SCALE_MINUTES) - 1))
    minutes = _BAN_SCALE_MINUTES[idx]
    data    = _load()
    until   = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    if mint not in data:
        data[mint] = {"banned_until": until}
    else:
        data[mint]["banned_until"] = until
    _save(data)
    print(f"[Memory] {mint[:8]}... banned for {minutes}m ({consecutive_losses} consec losses, until {until[:19]}Z)")


# ── S105: DEAD-POOL RE-ENTRY QUARANTINE ───────────────────────────────────────
# The dead-pool flat-exit churn is the fleet's actual SOL leak: ~83–88% of all closes are
# "Dead pool — vol <$500 +frozen px — exit before it ghosts" at ~0% PRICE move, so they book
# as BREAK-EVEN → the loss-streak ban (ban_token) never fires → the same dead mint gets
# re-bought 20–45× (Cm6fNnMk 42×, HZ1JovNi 45×, …), each round-trip bleeding fees+tip+slippage
# the price-based pnl can't see. Quarantine a mint when it exits dead, escalating on REPEAT
# offences so chronically-dead pools sit out progressively longer. Skip-only (only ever WIDENS
# an existing ban, never shortens it) → strictly capital-protective.
# S116-CHURNCUT (capital-preservation): the old [60,240,720,1440] CAPPED at 24h, so a chronically-dead
# mint (HZ1Jov, JUPyiwrY, Cm6fNn, DezX, 6qdz — re-bought 12–24× in the last 100 closes/bot) just
# round-trips ONCE PER BAN-PERIOD forever, paying fees+slippage on a near-zero-edge flat exit each time.
# Steeper escalation + an effectively-permanent tail RETIRES proven-dead mints: 3rd dead exit → 7d,
# 4th+ → 30d. Pure DISCIPLINE/fee-cut — only ever blocks RE-ENTRY on a mint that already dead-pool-
# exited, only ever WIDENS a ban (the never-SHORTEN guard below is intact). Cannot admit anything,
# cannot upsize, does NOT touch deep_pool admission/exit thresholds (S110 hold respected; this retires
# specific proven-dead mints, it is not edge-tuning). Revert: restore [60,240,720,1440] + restart.
_DEADPOOL_QUARANTINE_MINUTES = [120, 1440, 10080, 43200]  # 1st 2h · 2nd 24h · 3rd 7d · 4th+ 30d

def quarantine_dead_pool(mint: str) -> int:
    """Quarantine a mint from re-entry after a dead-pool exit. Escalates by lifetime
    dead-exit count. Honoured by memory.is_banned at every admission point. Returns the
    quarantine minutes applied (0 if a longer ban already covers it)."""
    data = _load()
    s    = data.setdefault(mint, {})
    n    = int(s.get("deadpool_exits", 0)) + 1
    s["deadpool_exits"] = n
    idx     = max(0, min(n - 1, len(_DEADPOOL_QUARANTINE_MINUTES) - 1))
    minutes = _DEADPOOL_QUARANTINE_MINUTES[idx]
    until   = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    existing = s.get("banned_until")
    applied  = minutes
    if existing and existing >= until:   # never SHORTEN an existing (e.g. loss-streak) ban
        applied = 0
    else:
        s["banned_until"] = until
    s["last_deadpool_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)
    if applied:
        print(f"[Memory] {mint[:8]}... dead-pool quarantine {minutes}m (offence #{n}, until {until[:19]}Z)", flush=True)
    return applied


def confidence_score(mint: str) -> float:
    """
    Return a confidence multiplier (0.25 – 1.0) for a token based on history.
    No history = 0.6 (cautious but not excluded).
    Consecutive-loss penalty: -0.05 per active losing streak entry (floor 0.25).
    Requires 3+ trades before fully trusting the win rate.
    """
    data = _load()
    if mint not in data:
        return 0.6

    s = data[mint]
    # S87 FIX: confidence is a WIN/LOSS signal — break-evens are neutral and must NOT
    # dilute it. Base win_rate AND the sample ramp on DECISIVE (win+loss) trades, not
    # total trades (which now include break-evens). A token with only break-evens has no
    # decisive signal → treat as no history (0.6), don't crater it to 0.
    wins     = s.get("wins", 0)
    losses   = s.get("losses", 0)
    decisive = wins + losses
    if decisive == 0:
        return 0.6

    win_rate = wins / decisive

    # Scale toward win_rate as sample grows — needs 3 decisive trades before full trust.
    # Faster ramp than 5 so a ≥60% WR token earns higher confidence sooner and
    # stops being filtered by conf gates. Trade-off: thin samples shift conf faster.
    sample_weight = min(decisive / 3.0, 1.0)
    raw_score = win_rate * sample_weight + 0.6 * (1 - sample_weight)

    # Active losing streak penalty: -0.05 per consecutive loss.
    # Tokens coming off a ban re-enter at reduced confidence so they don't immediately
    # trade at full size again.
    consecutive = s.get("consecutive_losses", 0)
    if consecutive > 0:
        raw_score -= consecutive * 0.05

    # Time-decay: wins older than 7 days reduce confidence. A token that hasn't won
    # recently should re-earn trust rather than trading at full confidence forever.
    # 0.05 penalty per week stale past day 7, capped at 0.20.
    if s.get("wins", 0) > 0:
        last_win = s.get("last_win_at")
        if last_win:
            try:
                days_stale = (datetime.now(timezone.utc) - datetime.fromisoformat(last_win)).days
                if days_stale > 7:
                    decay = min(0.20, (days_stale - 7) / 7 * 0.05)
                    raw_score -= decay
            except Exception:
                pass

    return round(max(0.25, min(1.0, raw_score)), 3)


def get_momentum_floor(mint: str, mode_min: float) -> float:
    """
    Returns a per-token momentum floor based on historical win patterns.
    If this token consistently wins at higher momentum than mode_min,
    don't fire on weaker signals — the history says they lose.
    Requires ≥3 wins before trusting avg_win_momentum.
    Falls back to mode_min when history is thin.
    """
    data = _load()
    s = data.get(mint, {})
    if s.get("wins", 0) < 3:
        return mode_min
    avg_win = s.get("avg_win_momentum", 0.0)
    if avg_win <= mode_min:
        return mode_min
    # Use 80% of avg winning momentum — gives room for variation without requiring
    # an exact replay of past conditions. Never raises the floor above 3× mode_min
    # so a single anomalous high-momentum win can't shut the token out entirely.
    floor = min(avg_win * 0.80, mode_min * 3)
    return round(max(mode_min, floor), 3)


def get_volume_floor(mint: str, mode_min_vol: float = 200.0) -> float:
    """
    Returns a per-token volume floor based on historical win patterns.
    If this token consistently wins at higher volume than mode_min_vol,
    don't fire on quieter signals — the history says they lose.
    Requires ≥3 wins before trusting avg_win_volume_5m.
    Falls back to mode_min_vol when history is thin.
    """
    data = _load()
    s = data.get(mint, {})
    if s.get("wins", 0) < 3:
        return mode_min_vol
    avg_win_vol = s.get("avg_win_volume_5m", 0.0)
    if avg_win_vol <= mode_min_vol:
        return mode_min_vol
    # 70% of avg winning volume — gives room for variation.
    # Never raises the floor above 50× mode_min_vol ($10k at $200 base) so one
    # anomalous high-volume win can't shut a token out entirely.
    floor = min(avg_win_vol * 0.70, mode_min_vol * 50)
    return round(max(mode_min_vol, floor), 2)


def is_banned(mint: str, current_cycle: int = 0) -> bool:
    data = _load()
    entry = data.get(mint, {})
    # Timestamp-based ban (current system)
    banned_until = entry.get("banned_until")
    if banned_until:
        try:
            return datetime.now(timezone.utc) < datetime.fromisoformat(banned_until)
        except Exception:
            pass
    return False


def get_avg_loss_momentum(mint: str) -> float:
    """Return average momentum at which this token has historically lost.
    Returns 0.0 if fewer than 3 losses (insufficient data).
    Used in evaluate() to block re-entry on known-losing momentum patterns.
    """
    data = _load()
    s = data.get(mint, {})
    if s.get("losses", 0) < 3:
        return 0.0
    return s.get("avg_loss_momentum", 0.0)


def get_stats(mint: str) -> Optional[dict]:
    return _load().get(mint)


def summary() -> list:
    """Return all token stats sorted by total PnL."""
    data = _load()
    rows = [{"mint": k, **v} for k, v in data.items()]
    return sorted(rows, key=lambda x: x.get("total_pnl", 0), reverse=True)


# ── Cross-bot Sentiment Relay ─────────────────────────────────────────────────
# When Bot 1 (INSANE) fires a high-quality signal, it writes a short-lived prime
# to sentiment_relay.json.  Bot 2 (WILD) reads this and lowers its confidence
# threshold by 0.10 for that token, letting INSANE's fast discovery lead.
#
# Ephemeral by design: 30-minute TTL, never accumulates in trade_memory.json.
# Cross-process: Bot 2 re-reads the file every 30s so it sees Bot 1's writes
# within one cycle without hammering disk.

_RELAY_PATH         = _SHARED_DIR / "sentiment_relay.json"
_RELAY_TTL_MINUTES  = 45   # S95: 30→45 — longer pile-in window for Bot2 on Bot1's primes
_relay_cache:       dict  = {}
_relay_cache_ts:    float = 0.0
_RELAY_CACHE_TTL_S: float = 30.0   # re-read from disk at most once per 30s


def _load_relay() -> dict:
    global _relay_cache, _relay_cache_ts
    if time.time() - _relay_cache_ts < _RELAY_CACHE_TTL_S and _relay_cache is not None:
        return _relay_cache
    try:
        _relay_cache = json.loads(_RELAY_PATH.read_text()) if _RELAY_PATH.exists() else {}
    except Exception:
        _relay_cache = {}
    _relay_cache_ts = time.time()
    return _relay_cache


def _save_relay(data: dict) -> None:
    global _relay_cache, _relay_cache_ts
    _tmp = _RELAY_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps(data, indent=2))
    os.replace(_tmp, _RELAY_PATH)
    _relay_cache    = data
    _relay_cache_ts = time.time()


def write_relay_prime(
    mint: str,
    primed_by: int,
    score: float,
    momentum: float,
    tier: str,
) -> None:
    """Write a sentiment relay prime for a token.

    Called by the INSANE bot when it fires a GEM/HIGHCONV or mean-reversion signal.
    Expires after RELAY_TTL_MINUTES (30 min).  Stale primes are pruned on every write.
    """
    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=_RELAY_TTL_MINUTES)).isoformat()
    data    = _load_relay()

    # Prune expired entries before writing so the file stays small
    data = {
        m: v for m, v in data.items()
        if v.get("expires_at", "") > now.isoformat()
    }

    data[mint] = {
        "primed_by":  primed_by,
        "primed_at":  now.isoformat(),
        "expires_at": expires,
        "score":      round(score, 1),
        "momentum":   round(momentum, 2),
        "tier":       tier,
    }
    _save_relay(data)
    print(
        f"[Relay] ▶ Bot#{primed_by} primed {mint[:8]}... "
        f"tier={tier} score={score:.0f} mom={momentum:.1f}% → expires in {_RELAY_TTL_MINUTES}m",
        flush=True,
    )


def get_relay_prime(mint: str) -> Optional[dict]:
    """Return the active relay prime for a token, or None if absent/expired.

    Called by WILD bot during evaluate() to check if INSANE has flagged this token.
    Re-reads from disk at most once per 30s so cross-process writes are visible
    within one trading cycle.
    """
    data  = _load_relay()
    entry = data.get(mint)
    if not entry:
        return None
    try:
        if datetime.now(timezone.utc) < datetime.fromisoformat(entry["expires_at"]):
            return entry
    except Exception:
        pass
    return None


# ── Execution penalty map ─────────────────────────────────────────────────────
# Tracks per-token slippage quality across the fleet.
# Accumulated in auditor.py after each confirmed sell; read by observer.py to
# reduce the ValidationScore of tokens with a history of bad fills.
#
# Score range: 0.0 (clean fills) → 1.0 (consistently 5 %+ slippage).
# Decay: ×0.80 every ~20 closes (~20-60 min); entries below 0.05 are pruned.
# Fleet-shared so a bad fill on Bot1 warns Bot2 and Bot3 within one cycle.

def get_execution_penalties() -> dict:
    """Return the current penalty map.  Uses in-process cache; re-reads on first call."""
    global _penalty_cache
    if _penalty_cache is None:
        try:
            _penalty_cache = json.loads(_PENALTY_PATH.read_text()) if _PENALTY_PATH.exists() else {}
        except Exception:
            _penalty_cache = {}
    return _penalty_cache


def update_execution_penalty(mint: str, new_penalty: float) -> None:
    """Blend new_penalty into the existing score for mint and persist atomically.

    Weighting: existing 60 % + new 40 %.  Smooths over one-off RPC hiccups while
    building up a real signal for consistently slippery tokens.
    """
    global _penalty_cache
    if not mint:
        return
    penalties = get_execution_penalties()
    existing  = penalties.get(mint, 0.0)
    penalties[mint] = round(min(1.0, existing * 0.6 + new_penalty * 0.4), 4)
    _penalty_cache  = penalties
    _save_penalties(penalties)


def decay_execution_penalties() -> None:
    """Apply 20 % decay to every entry; prune entries that fall below 0.05.

    Called every 20 trade closes (~20-60 min of real trading).
    """
    global _penalty_cache
    penalties  = get_execution_penalties()
    to_prune   = [m for m, v in penalties.items() if v * 0.8 < 0.05]
    for m in to_prune:
        del penalties[m]
    for m in penalties:
        penalties[m] = round(penalties[m] * 0.8, 4)
    _penalty_cache = penalties
    _save_penalties(penalties)
    if to_prune:
        print(f"[PenaltyDecay] pruned {len(to_prune)}, {len(penalties)} active", flush=True)


def _save_penalties(data: dict) -> None:
    tmp = str(_PENALTY_PATH) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, str(_PENALTY_PATH))
