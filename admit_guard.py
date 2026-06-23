#!/usr/bin/env python3
"""
admit_guard.py — S85 evidence-gated admission-quality layer  (SHADOW-first, read-only by default)

WHY (S85 diagnosis): the live bleed is NOT a throughput problem. The fresh-token flood the strict gate
rejects is genuinely −EV (forward_obs n=4013, ev_lo −1.43), and 82% of realized losses are GHOSTS, not
the edge. The honest lever toward the deploy gate is to ADMIT only the (play × regime) cells PROVEN +EV
and SKIP the ones PROVEN −EV — so clean net climbs toward ≥0 and the gate opens. Frequency only helps
when it's +EV frequency.

DESIGN (mirrors arm_genes / THE ONE OPERATOR RULE):
  • Single source of truth = regime_ev (same ghost exclusion + clean era), so verdicts never drift.
  • A cell flips to ADMIT only when proven +EV WITH CONFIDENCE (n≥MIN_N AND ev_lo ≥ 0); flips to SKIP only
    when proven −EV WITH CONFIDENCE (n≥MIN_N AND ev_hi < 0); otherwise NEUTRAL = today's behavior.
  • INERT until the data earns each cell. With current data almost everything is NEUTRAL (correct) — the
    tool auto-activates ADMIT/SKIP per cell exactly as the sample grows.
  • Can NEVER admit a −EV path. Can NEVER force the gate. Read-only unless a per-bot canary opts in.

LIVE WIRING (not applied — operator flips it):  in the entry path, before admitting a candidate:
    import admit_guard
    v = admit_guard.assess(play, regime, bot=BOT_ID)
    if v.action == "SKIP":      # only ever fires for PROVEN −EV cells when the canary is in "live" mode
        continue
  Canary  bots/botN/admit_guard.json :  {"enabled": true, "mode": "shadow"}   (shadow = log only, no skip)
                                        {"enabled": true, "mode": "live"}     (enforce SKIP verdicts)
  Absent/disabled  → NEUTRAL everywhere (byte-identical to today). Revert: rm the canary / rm this file.

CLI:
    python3 admit_guard.py --table        # current per-cell verdicts (what would activate)
    python3 admit_guard.py --shadow       # replay clean closes: SOL the SKIP verdicts would have avoided
    python3 admit_guard.py --all          # use all-time instead of clean era
"""
from __future__ import annotations
import json, math, sqlite3, argparse, re
from dataclasses import dataclass
from pathlib import Path

import regime_ev as R   # reuse _is_ghost, CLEAN_ERA, BOTS, MIN_TRUST_N — single source of truth

MIN_N = R.MIN_TRUST_N   # 8 — below this a cell is noise → NEUTRAL
Z = 1.64                # ~95% one-sided bound (matches strategy_brain ev_lo convention)

# ★ GATE SAFETY: never SKIP the plays that feed the EV-sizing deploy gate (gate needs deep_pool n≥15).
# Skipping the core edge cell would starve the gate AND freeze the S80 exit-fix maturation — self-
# defeating. Those plays' quality is already managed by observer (REQUIRE_STRONG, regime skip) + the
# gate itself. The guard only ever prunes proven −EV SIDE plays (momentum/gem/relay/quick…).
GATE_PROTECTED_PLAYS = {"deep_pool", "brain_rule"}

# S121 V2 — validated special lanes (their own pre-registered lane_watch verdict governs them).
# In force_skip/live mode these are EXEMPT (a play-keyed −EV cut must never eat a validated lane that
# merely shares a play label — the S118 RUNNEREDGE exemption). In allowlist mode an ALLOWED special
# lane admits unconditionally (never killed by its play's cell stats).
SPECIAL_LANES = {"momentum_override", "normal_slice"}


@dataclass
class Verdict:
    action: str          # "ADMIT" | "SKIP" | "NEUTRAL"
    reason: str
    n: int = 0
    ev: float = 0.0      # ◎/trade
    ev_lo: float = 0.0
    ev_hi: float = 0.0


def cell_stats(all_time: bool = False) -> dict:
    """{(play,regime): {n, wr, ev, ev_lo, ev_hi, net}} over CLEAN fleet closes (ghost-free).
    Recomputed here (not via regime_ev.collect) because we need per-trade dispersion for ev_lo/ev_hi."""
    series: dict = {}
    for bot in R.BOTS:
        db = Path(f"bots/bot{bot}/trades.db")
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = [json.loads(r[0]) for r in
                    c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid")]
            c.close()
        except Exception:
            continue
        for d in rows:
            if R._is_ghost(d):
                continue
            if not all_time and str(d.get("ts", ""))[:10] < R.CLEAN_ERA:
                continue
            play   = d.get("play") or d.get("tier") or "?"
            regime = d.get("regime") or "?"
            series.setdefault((play, regime), []).append(float(d.get("pnl_sol", 0) or 0.0))
    out = {}
    for key, xs in series.items():
        n = len(xs)
        mean = sum(xs) / n
        if n >= 2:
            var = sum((x - mean) ** 2 for x in xs) / n      # population sd (conservative for small n)
            se = math.sqrt(var) / math.sqrt(n)
        else:
            se = float("inf")
        wins = sum(1 for x in xs if x > 0)
        out[key] = {"n": n, "wr": 100 * wins / n, "ev": mean, "net": sum(xs),
                    "ev_lo": mean - Z * se, "ev_hi": mean + Z * se}
    return out


def verdict_for(play: str, regime: str, stats: dict) -> Verdict:
    s = stats.get((play, regime))
    if not s or s["n"] < MIN_N:
        n = s["n"] if s else 0
        return Verdict("NEUTRAL", f"n={n}<{MIN_N} (unproven — current behavior)", n)
    if s["ev_lo"] >= 0:
        return Verdict("ADMIT", f"+EV proven (ev_lo {s['ev_lo']:+.4f}◎ ≥ 0)",
                       s["n"], s["ev"], s["ev_lo"], s["ev_hi"])
    if s["ev_hi"] < 0:
        if play in GATE_PROTECTED_PLAYS:
            return Verdict("NEUTRAL",
                           f"gate-protected: {play} feeds the deploy gate — NOT skipped while proving "
                           f"(−EV ev_hi {s['ev_hi']:+.4f}, let S80 exit-fix mature)",
                           s["n"], s["ev"], s["ev_lo"], s["ev_hi"])
        return Verdict("SKIP", f"−EV proven (ev_hi {s['ev_hi']:+.4f}◎ < 0)",
                       s["n"], s["ev"], s["ev_lo"], s["ev_hi"])
    return Verdict("NEUTRAL", f"ambiguous (ev_lo {s['ev_lo']:+.4f}, ev_hi {s['ev_hi']:+.4f})",
                   s["n"], s["ev"], s["ev_lo"], s["ev_hi"])


def _canary(bot) -> dict:
    if bot is None:
        return {}
    p = Path(f"bots/bot{bot}/admit_guard.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def assess(play: str, regime: str, bot=None, _stats: dict | None = None) -> Verdict:
    """Entry-path hook. Returns a Verdict. SKIP is only *enforceable* when the bot's canary mode=='live';
    in shadow/off the caller should log v but NOT skip (the action still reports the proven verdict)."""
    stats = _stats if _stats is not None else cell_stats()
    v = verdict_for(play, regime, stats)
    cfg = _canary(bot)
    if not cfg.get("enabled"):
        # off → never enforce; surface the verdict for logging only
        v.reason = f"[canary off] {v.reason}"
    elif cfg.get("mode") != "live":
        v.reason = f"[shadow] {v.reason}"
    return v


# ── LIVE ENTRY HOOK (wired into main.py after classify_play) ────────────────────
# cell_stats() reads all 3 trades.db on every call → far too heavy to run per
# candidate per cycle. Cache it with a TTL so a hot loop pays the I/O at most once
# per _STATS_TTL seconds regardless of call frequency.
_STATS_CACHE: dict = {"ts": 0.0, "stats": None}
_STATS_TTL = 300.0   # recompute the per-cell EV table at most every 5 min


def _cached_stats() -> dict:
    import time as _t
    now = _t.time()
    if _STATS_CACHE["stats"] is None or (now - _STATS_CACHE["ts"]) > _STATS_TTL:
        try:
            _STATS_CACHE["stats"] = cell_stats()
        except Exception:
            # never let a DB read failure break admission — fall back to inert behaviour
            _STATS_CACHE["stats"] = _STATS_CACHE["stats"] or {}
        _STATS_CACHE["ts"] = now
    return _STATS_CACHE["stats"]


def _cell_matches(play: str, regime: str, entry) -> bool:
    """True if a force_skip entry names this (play, regime) cell.
    Accepts 'gem×normal' / 'gem x normal' / 'gem:normal' / ['gem','normal']
    (play names carry underscores — deep_pool / mean_reversion — so we split on
    explicit separators, not on every char)."""
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        p, r = entry
    else:
        parts = re.split(r'[×x:|/,\s]+', str(entry).strip())
        if len(parts) < 2:
            return False
        p, r = parts[0], parts[1]
    return p.strip().lower() == play.lower() and r.strip().lower() == regime.lower()


def _env_bot():
    import os
    try:
        return int(os.getenv("BOT_ID", "1"))
    except Exception:
        return 1


def allowlist_blocks(play: str, bot=None, lane=None) -> bool:
    """S121-dpgap: CHEAP allowlist-only gate (NO stats / NO DB read) for admission paths that
    DON'T flow through should_skip()'s statistical hook — specifically the deep_pool/brain_rule
    loop in observer.py, which is admitted on its own loop and bypassed the allowlist freeze.

    Returns True ⇒ this bot's canary is in allowlist mode AND this play/lane is not allowed ⇒
    the caller should SUPPRESS the admission. Only relevant in allowlist mode; in off / live
    (force_skip) mode this ALWAYS returns False (deep_pool stays gate-protected as before).
    Fail-open on any error or malformed/empty allowlist (never freezes the fleet on a bad file)."""
    try:
        cfg = _canary(_env_bot() if bot is None else bot)
        if not cfg.get("enabled") or cfg.get("mode") != "allowlist":
            return False
        lanes = cfg.get("lanes") or []
        if not lanes:
            return False                              # malformed/empty → fail-open
        key = lane if lane in SPECIAL_LANES else play
        return key not in lanes
    except Exception:
        return False


def should_skip(play: str, regime: str, bot=None, lane=None) -> tuple[bool, Verdict]:
    """Live entry hook → (enforce_skip, verdict). THREE canary modes:

    OFF / absent / malformed canary  → enforce_skip=False (byte-identical to no-guard; fail-open).

    "live" (legacy S116 force_skip)  → enforce_skip True for EITHER:
      (1) a PROVEN −EV SKIP — n≥MIN_N and the whole 95% bound below 0 (statistical core); OR
      (2) an explicit operator force_skip of a NAMED cell (e.g. gem×normal, relay×normal).
      Safe-direction: can ONLY skip a side play; deep_pool/brain_rule are gate-protected (NEUTRAL
      in verdict_for so never skipped); validated SPECIAL_LANES are EXEMPT (never eaten by a
      play-keyed cut — the S118 RUNNEREDGE exemption, now enforced INSIDE this function).

    "allowlist" (S121 V2)  → trade ONLY the named lanes; block EVERY other entry.
      The candidate's allowlist key = `lane` if it is a special lane, else `play`.
      • key ∉ lanes               → enforce SKIP (this is how deep_pool/brain_rule + the whole
        anonymous price-action book get blocked — gate-protection is intentionally LIFTED here,
        the kill_criterion already FAILED so the uncontaminated-sample purpose is complete, S117).
      • allowed SPECIAL lane      → admit unconditionally (its own lane_watch governs it).
      • allowed plain play        → admit, but the statistical-proof SKIP stays as a THIRD layer
        (can only further restrict, never admit).
      • empty/missing `lanes`     → fail-open (never lock the fleet out on a malformed allowlist).

    The verdict is always returned so the caller can shadow-log even when not enforcing.
    """
    try:
        v = verdict_for(play, regime, _cached_stats())
    except Exception:
        return False, Verdict("NEUTRAL", "error → inert", 0)
    cfg = _canary(bot)
    if not cfg.get("enabled"):
        return False, v                                    # off / absent / malformed → fail-open
    mode = cfg.get("mode")

    # ── ALLOWLIST MODE ──────────────────────────────────────────────────────────
    if mode == "allowlist":
        lanes = cfg.get("lanes") or []
        if not lanes:                                      # malformed allowlist → never lock out
            return False, Verdict("NEUTRAL", "[allowlist] empty lanes → fail-open", v.n)
        key = lane if lane in SPECIAL_LANES else play
        if key not in lanes:
            return True, Verdict("SKIP", f"[allowlist] '{key}' not in allowed lanes {lanes}",
                                 v.n, v.ev, v.ev_lo, v.ev_hi)
        if lane in SPECIAL_LANES:                          # validated lane → admit unconditionally
            return False, Verdict("ADMIT", f"[allowlist] lane '{lane}' allowed",
                                  v.n, v.ev, v.ev_lo, v.ev_hi)
        if v.action == "SKIP":                             # third layer on an allowed plain play
            return True, Verdict("SKIP",
                                 f"[allowlist+stat] {play}×{regime} proven −EV (ev_hi {v.ev_hi:+.4f}◎)",
                                 v.n, v.ev, v.ev_lo, v.ev_hi)
        return False, v

    # ── LIVE / FORCE_SKIP MODE (legacy) ─────────────────────────────────────────
    live = mode == "live"
    if lane in SPECIAL_LANES:                              # validated lane → EXEMPT in force_skip mode
        return False, Verdict("NEUTRAL", f"[exempt] validated lane '{lane}'",
                              v.n, v.ev, v.ev_lo, v.ev_hi)
    enforce = live and v.action == "SKIP"                  # (1) statistical −EV (gate-protected = NEUTRAL)
    if live and not enforce and play not in GATE_PROTECTED_PLAYS:   # (2) operator force_skip of a named cell
        for entry in (cfg.get("force_skip") or []):
            if _cell_matches(play, regime, entry):
                enforce = True
                v = Verdict("SKIP",
                            f"operator force_skip {play}×{regime} (n={v.n}, ev/tr {v.ev:+.4f}◎)",
                            v.n, v.ev, v.ev_lo, v.ev_hi)
                break
    return enforce, v


# ----------------------------- CLI -----------------------------
def _table(all_time: bool):
    stats = cell_stats(all_time)
    if not stats:
        print("No clean closes yet — every cell NEUTRAL (current behavior).")
        return
    print(f"  ADMIT-GUARD verdicts  ({'all-time' if all_time else 'clean era since '+R.CLEAN_ERA})  "
          f"MIN_N={MIN_N}")
    print(f"  {'play × regime':28s} {'n':>3} {'WR':>5} {'EV◎/tr':>9} {'ev_lo':>9} {'ev_hi':>9}  verdict")
    for (play, regime), s in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        v = verdict_for(play, regime, stats)
        tag = {"ADMIT": "✅ ADMIT", "SKIP": "⛔ SKIP", "NEUTRAL": "·· NEUTRAL"}[v.action]
        print(f"  {play+' × '+regime:28s} {s['n']:>3} {s['wr']:>4.0f}% {s['ev']:>+9.4f} "
              f"{s['ev_lo']:>+9.4f} {s['ev_hi']:>+9.4f}  {tag}  ({v.reason})")
    n_admit = sum(1 for k in stats if verdict_for(*k, stats).action == "ADMIT")
    n_skip  = sum(1 for k in stats if verdict_for(*k, stats).action == "SKIP")
    print(f"\n  → {n_admit} cell(s) proven ADMIT · {n_skip} proven SKIP · rest NEUTRAL "
          f"(inert until n≥{MIN_N} with a confident sign).")


def _shadow(all_time: bool):
    """Replay clean closes; report the realized ◎ that LIVE-mode SKIP verdicts would have avoided."""
    stats = cell_stats(all_time)
    avoided = 0.0; kept = 0.0; n_skip = 0; n_admit = 0
    for bot in R.BOTS:
        db = Path(f"bots/bot{bot}/trades.db")
        if not db.exists():
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = [json.loads(r[0]) for r in
                c.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid")]
        c.close()
        for d in rows:
            if R._is_ghost(d):
                continue
            if not all_time and str(d.get("ts", ""))[:10] < R.CLEAN_ERA:
                continue
            v = verdict_for(d.get("play") or d.get("tier") or "?", d.get("regime") or "?", stats)
            ps = float(d.get("pnl_sol", 0) or 0.0)
            if v.action == "SKIP":
                avoided += ps; n_skip += 1
            elif v.action == "ADMIT":
                kept += ps; n_admit += 1
    print(f"  SHADOW replay ({'all-time' if all_time else 'clean era'}):")
    print(f"    [note] verdicts use full-sample cell stats (not leave-one-out); indicative, not a backtest.")
    print(f"    LIVE-mode would SKIP {n_skip} trade(s) → net ◎ avoided: {-avoided:+.4f}  "
          f"(positive = bleed removed)")
    print(f"    ADMIT-proven {n_admit} trade(s) → net ◎ kept: {kept:+.4f}")
    if n_skip == 0 and n_admit == 0:
        print("    (no cell is proven yet — guard is fully NEUTRAL; identical to today. Correct.)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--all", action="store_true", help="all-time instead of clean era")
    a = ap.parse_args()
    if a.shadow:
        _shadow(a.all)
    else:
        _table(a.all)
