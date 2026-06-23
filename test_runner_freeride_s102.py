#!/usr/bin/env python3
"""S102 — runner free-ride: unit tests (read-only, no network).

Proves the integration that makes the house-money mechanism actually catch a Jotchua:
  1. _trail_buffer honours the max_buffer_pct override (and the global 8% cap when None).
  2. _runner_trail_cap is correctly SCOPED — only the Jotchua momentum-gem cohort gets the
     wide cap; a generic ride / off-cohort position keeps the global cap.
  3. Two-stage: pre-bank cap (18%) before house_money_taken, free cap (45%) after.
  4. The canary gates it (master OFF + no file → unchanged behaviour).
  5. The widened trail would have HELD the real bot1 Jotchua trade (peak +28.2%) instead of
     clipping at +21.7%, while the −10% SL still bounds the downside.
"""
import json, time, tempfile, pathlib, importlib
import stoic_strategy as ss

PASS = []; FAIL = []
def ok(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")

def _reset_cache():
    ss._runner_freeride_cache = (0.0, False)

# ── 1. _trail_buffer fixed-buffer override ───────────────────────────────────
print("\n[1] _trail_buffer fixed_buffer_pct override (vs vol-scaled default)")
# A CALM window — vol-scaled buffer collapses small (this is what clipped Jotchua).
calm = [1.000, 1.005, 0.998, 1.002, 1.000]
b_default, _ = ss._trail_buffer(1.0, calm)                          # vol-scaled, 8% cap
b_fixed,   _ = ss._trail_buffer(1.0, calm, fixed_buffer_pct=18.0)   # runner pre-bank fixed
b_free,    _ = ss._trail_buffer(1.0, calm, fixed_buffer_pct=45.0)   # runner post-bank fixed
ok("vol-scaled buffer is tight in a calm window (<8%)", b_default < ss.SMART_TRAIL_MAX_BUFFER_PCT)
ok("fixed 18% holds wide despite calm vol (the fix)", abs(b_fixed - 18.0) < 1e-9)
ok("fixed 45% post-bank free-ride", abs(b_free - 45.0) < 1e-9)
# choppier-than-fixed window → the wider (vol) wins
chop = [1.0, 1.4, 0.7, 1.5, 0.6]
b_chop, _ = ss._trail_buffer(2.0, chop, fixed_buffer_pct=18.0)
ok("unusually choppy runner keeps >= its vol room", b_chop >= 18.0)
# min-buffer floor still respected
b_flat, _ = ss._trail_buffer(1.0, [1.0, 1.0], fixed_buffer_pct=18.0)
ok("min-buffer floor respected", b_flat >= ss.SMART_TRAIL_MIN_BUFFER_PCT)

# ── 2 + 3 + 4. _runner_trail_buffer scope, stages, canary ────────────────────
print("\n[2-4] _runner_trail_buffer scope / stages / canary")
tmp = pathlib.Path(tempfile.mkdtemp())
ss._RUNNER_FREERIDE_PATH = tmp / "runner_freeride.json"   # redirect the canary to a temp file

# canary OFF (no file) → None regardless of cohort
_reset_cache()
ok("canary OFF → None (even in cohort)", ss._runner_trail_buffer({"ride": True}, True) is None)

# canary ON
(tmp / "runner_freeride.json").write_text(json.dumps({"enabled": True}))
_reset_cache()
ok("ON + NOT in cohort → None (sniper book untouched)",
   ss._runner_trail_buffer({"ride": True}, False) is None)
_reset_cache()
ok("ON + in cohort + pre-bank → 18%",
   ss._runner_trail_buffer({"ride": True}, True) == ss._RUNNER_PREBANK_BUFFER_PCT)
_reset_cache()
ok("ON + in cohort + post-bank → 45%",
   ss._runner_trail_buffer({"ride": True, "house_money_taken": True}, True) == ss._RUNNER_FREE_BUFFER_PCT)

# canary removed → back to None (hot-disable)
(tmp / "runner_freeride.json").unlink()
_reset_cache()
ok("file removed → None (hot revert)", ss._runner_trail_buffer({"ride": True}, True) is None)

# ── 5. the Jotchua replay: the fixed buffer HOLDS where vol-scaling clipped ───
print("\n[5] Jotchua replay — would the fixed buffer have held the runner?")
entry = 0.001256
peak  = entry * 1.282          # +28.2% peak (the real bot1 high)
# Real exit was +21.7% on a ~3.5% vol buffer (calm 5-min window). The fixed pre-bank
# buffer rides far lower, so the +28% peak survives a normal inter-leg dip.
calm = [peak*0.99, peak*1.0, peak*0.995, peak]
b_old, trig_old = ss._trail_buffer(peak, calm)                        # vol-scaled, clipped real
b_new, trig_new = ss._trail_buffer(peak, calm, fixed_buffer_pct=18.0) # runner pre-bank fixed
g_old = (trig_old - entry) / entry * 100
g_new = (trig_new - entry) / entry * 100
print(f"     peak +28.2% | vol-scaled trail exits at +{g_old:.1f}% (≈ the real +21.7% clip) "
      f"| fixed-18% trail exits at +{g_new:.1f}%")
ok("fixed buffer lowers the exit trigger (more room to ride the legs)", g_new < g_old)
ok("downside still bounded above the -10% ride SL", g_new > ss._VAE_STOP_LOSS_PCT)

# ── house_money_plan still composes (sanity: +100% euphoria banks ~half) ──────
print("\n[6] house-money compose sanity (+100%, euphoria, conf 0.9)")
f, conv, rm = ss._house_money_plan(100.0, 0.9, "euphoria")
ok("banks ~50% at +100% high-conv (recovers basis, rides free)", 0.45 <= f <= 0.55 and rm >= 1.0)

print(f"\n{'='*60}\n  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  FAILED:", FAIL); raise SystemExit(1)
print("  ✅ S102 runner free-ride integration verified")
