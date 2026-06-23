#!/usr/bin/env python3
"""
test_admit_guard_allowlist_s121.py — Phase 1.4 (V2): admit_guard allowlist mode.

Offline, no network, no live DB: monkeypatches admit_guard._canary (the per-bot JSON reader)
and admit_guard._cached_stats (the cell-EV table) so the pure should_skip() logic is exercised
deterministically. Run: python3 test_admit_guard_allowlist_s121.py
"""
import admit_guard as AG

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  got={got!r} want={want!r}")


def with_canary(cfg, stats=None):
    """Install a fake canary + stats table; returns nothing (sets module globals)."""
    AG._canary = lambda bot: dict(cfg) if cfg is not None else {}
    AG._cached_stats = lambda: dict(stats or {})


def skip(play, regime, lane=None, bot=1):
    enforce, v = AG.should_skip(play, regime, bot=bot, lane=lane)
    return enforce


# ─────────────────────────────────────────────────────────────────────────────
print("ALLOWLIST MODE — lanes:['momentum_override']")
ALLOW = {"enabled": True, "mode": "allowlist", "lanes": ["momentum_override"]}
with_canary(ALLOW, stats={})   # empty stats → every cell NEUTRAL
# blocks the entire anonymous price-action book + the disproven deep_pool
check("allowlist blocks gem",        skip("gem", "normal"),                 True)
check("allowlist blocks relay",      skip("relay", "normal"),               True)
check("allowlist blocks quick",      skip("quick", "aggressive"),           True)
check("allowlist blocks bank",       skip("bank", "normal"),                True)
check("allowlist blocks momentum",   skip("momentum", "aggressive"),        True)
check("allowlist blocks deep_pool",  skip("deep_pool", "sniper"),           True)   # gate-protection LIFTED
check("allowlist blocks brain_rule", skip("brain_rule", "euphoria"),        True)
# admits the one allowed validated lane (carries play=gem/momentum but lane=momentum_override)
check("allowlist admits momentum_override (play=gem)",      skip("gem", "normal", lane="momentum_override"),      False)
check("allowlist admits momentum_override (play=momentum)", skip("momentum", "aggressive", lane="momentum_override"), False)
# normal_slice is NOT in the allowlist → blocked even though it is a special lane
check("allowlist blocks normal_slice (not listed)",         skip("deep_pool", "normal", lane="normal_slice"),     True)

print("ALLOWLIST MODE — allowed plain play still faces the statistical third layer")
# proven −EV cell: n>=MIN_N, whole CI below 0
NEG = {("gem", "normal"): {"n": 30, "wr": 30.0, "ev": -0.01, "net": -0.30,
                           "ev_lo": -0.02, "ev_hi": -0.005}}
with_canary({"enabled": True, "mode": "allowlist", "lanes": ["gem"]}, stats=NEG)
check("allowlist+stat skips an allowed but proven −EV play", skip("gem", "normal"), True)
# proven +EV / neutral allowed play → admitted
POS = {("gem", "normal"): {"n": 30, "wr": 70.0, "ev": 0.01, "net": 0.30,
                           "ev_lo": 0.005, "ev_hi": 0.02}}
with_canary({"enabled": True, "mode": "allowlist", "lanes": ["gem"]}, stats=POS)
check("allowlist admits an allowed +EV play", skip("gem", "normal"), False)

print("MALFORMED ALLOWLIST → fail-open (never a lockout)")
with_canary({"enabled": True, "mode": "allowlist"}, stats={})            # no 'lanes' key
check("missing lanes → fail-open admit gem",        skip("gem", "normal"),    False)
check("missing lanes → fail-open admit momentum",   skip("momentum", "aggressive"), False)
with_canary({"enabled": True, "mode": "allowlist", "lanes": []}, stats={})  # empty list
check("empty lanes → fail-open admit gem",          skip("gem", "normal"),    False)

print("CANARY ABSENT / DISABLED → byte-identical to no-guard (admit all)")
with_canary(None, stats={})                                              # _canary → {}
check("absent canary → admit gem",      skip("gem", "normal"),       False)
check("absent canary → admit deep_pool",skip("deep_pool", "sniper"), False)
with_canary({"enabled": False, "mode": "allowlist", "lanes": ["x"]}, stats={})
check("disabled canary → admit gem",    skip("gem", "normal"),       False)

print("LIVE / FORCE_SKIP MODE preserved (S116 backward-compat)")
LIVE = {"enabled": True, "mode": "live", "force_skip": ["gem×normal", "relay×normal"]}
with_canary(LIVE, stats={})
check("force_skip cuts gem×normal",          skip("gem", "normal"),        True)
check("force_skip cuts relay×normal",        skip("relay", "normal"),      True)
check("force_skip leaves gem×aggressive",    skip("gem", "aggressive"),    False)
check("force_skip leaves quick×normal",      skip("quick", "normal"),      False)
# validated special lanes EXEMPT in force_skip mode (the RUNNEREDGE exemption, now inside should_skip)
check("force_skip EXEMPTS momentum_override (play=gem×normal)", skip("gem", "normal", lane="momentum_override"), False)
check("force_skip EXEMPTS normal_slice (play=deep_pool×normal)", skip("deep_pool", "normal", lane="normal_slice"), False)
# deep_pool gate-protected in force_skip mode even if force-listed
with_canary({"enabled": True, "mode": "live", "force_skip": ["deep_pool×normal"]}, stats={})
check("force_skip canNOT cut gate-protected deep_pool", skip("deep_pool", "normal"), False)
# statistical SKIP fires in live mode on a proven −EV side play
with_canary({"enabled": True, "mode": "live"}, stats=NEG)
check("live statistical SKIP on proven −EV gem×normal", skip("gem", "normal"), True)

print("SHADOW MODE → never enforces")
with_canary({"enabled": True, "mode": "shadow", "force_skip": ["gem×normal"]}, stats=NEG)
check("shadow never enforces force_skip", skip("gem", "normal"), False)

print("allowlist_blocks() — the cheap gate for the deep_pool/brain_rule loop (S121-dpgap)")
with_canary({"enabled": True, "mode": "allowlist", "lanes": ["momentum_override"]}, stats={})
check("allowlist_blocks deep_pool",  AG.allowlist_blocks("deep_pool", bot=1), True)
check("allowlist_blocks brain_rule", AG.allowlist_blocks("brain_rule", bot=1), True)
check("allowlist_blocks admits the allowed runner lane", AG.allowlist_blocks("gem", bot=1, lane="momentum_override"), False)
with_canary({"enabled": True, "mode": "live", "force_skip": ["gem×normal"]}, stats={})
check("force_skip mode does NOT allowlist-block deep_pool", AG.allowlist_blocks("deep_pool", bot=1), False)
with_canary(None, stats={})
check("no canary → allowlist_blocks fail-open (False)", AG.allowlist_blocks("deep_pool", bot=1), False)
with_canary({"enabled": True, "mode": "allowlist"}, stats={})  # malformed: no lanes
check("malformed allowlist → allowlist_blocks fail-open (False)", AG.allowlist_blocks("deep_pool", bot=1), False)

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*60}")
raise SystemExit(1 if FAIL else 0)
