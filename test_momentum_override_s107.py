#!/usr/bin/env python3
"""S107 — momentum-override lane + anti-dead floor: unit tests (read-only, no network).

Covers the three live integration points:
  1. Canary readers gate correctly (default OFF; ON via file; min_v5 honoured) — observer + stoic.
  2. open_position stores the momentum_override flag → it reaches the close row.
  3. check_exits applies the RIDE remap (no fixed-TP bank, SL+trail) for a momentum_override
     position ONLY when the canary is ON — and never for an off-cohort/canary-off position.
  4. The admission-signature predicate (m5≥10 ∧ bs≥1.2 ∧ v5≥2000 ∧ liq≥15k) matches the
     research cohort and rejects the near-misses.
"""
import json, time, tempfile, pathlib
from datetime import datetime, timezone
import observer as ob
import stoic_strategy as ss

PASS = []; FAIL = []
def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}{('  — ' + extra) if (extra and not cond) else ''}")

# ── 1. canary readers ────────────────────────────────────────────────────────
print("\n[1] canary readers (default OFF; ON via file; min_v5 honoured)")
tmp = pathlib.Path(tempfile.mkdtemp())

ob._MOM_OVERRIDE_PATH = tmp / "momentum_override.json"
ob._mom_override_cache = (0.0, False)
ok("observer momentum-override OFF by default", ob._momentum_override_on() is False)
(tmp / "momentum_override.json").write_text(json.dumps({"enabled": True}))
ob._mom_override_cache = (0.0, False)
ok("observer momentum-override ON via file", ob._momentum_override_on() is True)

ob._ANTIDEAD_V5_PATH = tmp / "antidead_v5.json"
ob._antidead_v5_cache = (0.0, (False, 0.0))
en, mv = ob._antidead_v5_floor()
ok("anti-dead floor OFF by default", en is False)
(tmp / "antidead_v5.json").write_text(json.dumps({"enabled": True, "min_v5": 1500}))
ob._antidead_v5_cache = (0.0, (False, 0.0))
en, mv = ob._antidead_v5_floor()
ok("anti-dead floor ON + min_v5 read", en is True and abs(mv - 1500.0) < 1e-9, f"{en},{mv}")

ss._MOM_OVERRIDE_RIDE_PATH = tmp / "momentum_override.json"  # same file as admission
ss._mom_override_ride_cache = (0.0, False)
ok("stoic ride reader ON via the SAME file", ss._momentum_override_ride_on() is True)

# ── 2. open_position stores the flag ─────────────────────────────────────────
print("\n[2] open_position stores momentum_override → reaches close row")
s = ss.StoicStrategy()
s._save_positions = lambda: None
mint = "So1111111111111111111111111111111111111111"
s.open_position(mint, entry_price=1.0, size_sol=0.04, size_usd=6.0, momentum=12.0,
                tier_label="momentum", mode="insane", regime="normal",
                momentum_override=True)
ok("position carries momentum_override=True", s.positions[mint].get("momentum_override") is True)
# control: a normal entry does NOT carry it
s.open_position("Bo1111111111111111111111111111111111111111", entry_price=1.0, size_sol=0.04,
                size_usd=6.0, momentum=12.0, tier_label="gem", mode="insane", regime="normal")
ok("normal entry has no momentum_override", not s.positions["Bo1111111111111111111111111111111111111111"].get("momentum_override"))

# ── 3. check_exits RIDE remap (canary ON vs OFF / off-cohort) ─────────────────
print("\n[3] check_exits applies the RIDE (no fixed-TP) only for momentum_override + canary ON")
def _mk(mint, override=False, tier="momentum"):
    return {
        "entry_price": 1.0, "size_sol": 0.04, "size_usd": 6.0,
        "momentum_at_entry": 12.0, "volume_5m_at_entry": 3000.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "peak_price": 1.0, "trail_active": False, "price_history": [1.0],
        "stack_count": 0, "tp1_taken": False, "remaining_fraction": 1.0,
        "regime": "normal", "confidence": 0.6, "ride": False,
        "insane_tier": tier, "mode": "insane", "entry_liq": 22000.0,
        "take_profit_pct": 20.0, "stop_loss_pct": -6.0, "max_hold_hours": 3.0,
        **({"momentum_override": True} if override else {}),
    }

# canary ON
(tmp / "momentum_override.json").write_text(json.dumps({"enabled": True}))
ss._mom_override_ride_cache = (0.0, False)
s2 = ss.StoicStrategy(); s2._save_positions = lambda: None
mo = "Mo1111111111111111111111111111111111111111"
s2.positions = {mo: _mk(mo, override=True)}
s2.check_exits({mo: 1.05}, cycle=1)   # +5%: under gem TP 20 → no exit; remap should have fired
p = s2.positions[mo]
ok("RIDE active: pos['ride'] set True", p.get("ride") is True)
ok("RIDE active: fixed TP1 disabled", p.get("tp1_enabled") is False)

# off-cohort (no override flag) with canary ON → no remap
oc = "Oc1111111111111111111111111111111111111111"
s2.positions[oc] = _mk(oc, override=False, tier="gem")
s2.check_exits({oc: 1.05}, cycle=2)
ok("off-cohort position NOT force-ridden by the canary",
   s2.positions[oc].get("tp1_enabled") is not False or s2.positions[oc].get("ride") is not True
   or s2.positions[oc].get("momentum_override") is None)

# canary OFF → momentum_override pos does NOT get the ride remap from THIS canary
(tmp / "momentum_override.json").write_text(json.dumps({"enabled": False}))
ss._mom_override_ride_cache = (0.0, False)
ok("canary OFF → ride reader False", ss._momentum_override_ride_on() is False)

# ── 4. admission-signature predicate ─────────────────────────────────────────
print("\n[4] admission signature (RUNNEREDGE): m5≥15 ∧ bs≥1.3 ∧ v5≥1000 ∧ liq≥3000")
def sig_ok(m5, bs, v5, liq):
    return (m5 >= ob._MOM_OVR_M5_MIN and bs >= ob._MOM_OVR_BS_MIN
            and v5 >= ob._MOM_OVR_V5_MIN and liq >= ob._MOM_OVR_LIQ_MIN)
ok("runner (m5=18,bs=1.4,v5=3k,liq=23k) ADMITS", sig_ok(18, 1.4, 3000, 23000))
ok("★ low-liq runner (liq=$8k — the edge) ADMITS", sig_ok(18, 1.4, 3000, 8000))
ok("weak momentum (m5=12<15) REJECTS", not sig_ok(12, 1.4, 3000, 23000))
ok("weak buy pressure (bs=1.2<1.3) REJECTS", not sig_ok(18, 1.2, 3000, 23000))
ok("single-trade spike (v5=$500<1000) REJECTS", not sig_ok(18, 1.4, 500, 23000))
ok("below exitability floor (liq=$2k<3k) REJECTS", not sig_ok(18, 1.4, 3000, 2000))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*52}\n  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL: print(f"   ✗ {f}")
    raise SystemExit(1)
print("  ALL PASS")
