#!/usr/bin/env python3
"""
test_taxonomy.py — TAXONOMY v2 resolver tests (offline; temp dirs; no live writes).

The spec's required truth table: every old play × regime → new cell, canary off = no-op,
DEEP frozen beyond table reach, exit-family ≡ legacy flag outcomes, shim, hysteresis, clamps.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import regime_policy as rp

LEGACY_PLAYS = ["gem", "momentum", "relay", "quick", "bank", "highconv", "mean_reversion",
                "probe", "force_fire", "deep_pool", "brain_rule"]
LEGACY_REGIMES = ["euphoria", "aggressive", "normal", "sniper", "dead"]

TABLE = json.loads((Path(__file__).parent / "regime_policy_table.json").read_text())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="taxo_"))
        (self.tmp / "bots" / "bot1").mkdir(parents=True)
        (self.tmp / "shared_memory").mkdir()
        self._orig = (rp.TABLE_PATH, rp.BOTS_DIR, rp.SNAP_PATH)
        rp.TABLE_PATH = self.tmp / "regime_policy_table.json"
        rp.BOTS_DIR = self.tmp / "bots"
        rp.SNAP_PATH = self.tmp / "shared_memory" / "discovery_snapshot.json"
        self.write_table(TABLE)
        self._reset()

    def tearDown(self):
        rp.TABLE_PATH, rp.BOTS_DIR, rp.SNAP_PATH = self._orig
        self._reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset(self):
        rp._cfg_cache.clear()
        rp._table_cache.update(ts=-1e9, table=None, sha=None)
        rp._snap_cache.update(ts=-1e9, agg=None)
        rp._band_state["band"] = None

    def write_table(self, t):
        rp.TABLE_PATH.write_text(json.dumps(t))
        self._reset()

    def enable(self, bot=1, layers=None):
        cfg = {"enabled": True}
        if layers is not None:
            cfg["layers"] = layers
        (rp.BOTS_DIR / f"bot{bot}").mkdir(parents=True, exist_ok=True)
        (rp.BOTS_DIR / f"bot{bot}" / "regime_policy.json").write_text(json.dumps(cfg))
        self._reset()

    def set_agg(self, agg):
        rp.SNAP_PATH.write_text(json.dumps({"ts": time.time(), "agg_vol_5m": agg,
                                            "market_data": {"m": {}}}))
        self._reset()


class TestPlayV2TruthTable(unittest.TestCase):
    def test_every_legacy_play(self):
        for p in ["gem", "momentum", "relay", "quick", "bank", "highconv",
                  "mean_reversion", "probe", "force_fire", "weird_new_play", None]:
            self.assertEqual(rp.play_v2(p), "SCALP", p)
        for p in ("deep_pool", "brain_rule"):
            self.assertEqual(rp.play_v2(p), "DEEP")

    def test_flags_resolve_before_labels(self):
        self.assertEqual(rp.play_v2("gem", momentum_override=True), "RUNNER")
        self.assertEqual(rp.play_v2("momentum", momentum_override=True), "RUNNER")
        self.assertEqual(rp.play_v2("gem", normal_slice=True), "DEEP")
        self.assertEqual(rp.play_v2("gem", dust_shadow=True), "DUST")
        # DUST wins over everything; DEEP wins over the runner flag (frozen beats lane)
        self.assertEqual(rp.play_v2("deep_pool", momentum_override=True, dust_shadow=True), "DUST")
        self.assertEqual(rp.play_v2("deep_pool", momentum_override=True), "DEEP")


class TestRegimeV2(Base):
    def test_shim_all_five_names(self):
        exp = {"euphoria": "RISK_OFF", "aggressive": "RISK_OFF", "normal": "RISK_ON",
               "sniper": "RISK_ON", "dead": "RISK_ON"}
        for k, v in exp.items():
            self.assertEqual(rp.regime_v2_from_legacy(k), v, k)
        self.assertIsNone(rp.regime_v2_from_legacy("nonsense"))
        self.assertIsNone(rp.regime_v2_from_legacy(None))

    def test_band_edges(self):
        c1 = TABLE["regimes_v2"]["cuts"]["c1"]; c2 = TABLE["regimes_v2"]["cuts"]["c2"]
        self.assertEqual(rp.regime_v2(agg=c1 - 1), "RISK_ON")
        self._reset()
        self.assertEqual(rp.regime_v2(agg=c1 + 1), "NEUTRAL")
        self._reset()
        self.assertEqual(rp.regime_v2(agg=c2 + 1), "RISK_OFF")

    def test_hysteresis_holds_band_through_wobble(self):
        c1 = TABLE["regimes_v2"]["cuts"]["c1"]
        self.assertEqual(rp.regime_v2(agg=c1 * 0.80), "RISK_ON")     # settle in RISK_ON
        self.assertEqual(rp.regime_v2(agg=c1 * 1.05), "RISK_ON")     # +5% past cut: held
        self.assertEqual(rp.regime_v2(agg=c1 * 1.15), "NEUTRAL")     # +15%: flips
        self.assertEqual(rp.regime_v2(agg=c1 * 0.95), "NEUTRAL")     # −5% back: held
        self.assertEqual(rp.regime_v2(agg=c1 * 0.85), "RISK_ON")     # −15%: flips back

    def test_stale_snapshot_holds_last_band(self):
        self.set_agg(100_000)
        self.assertEqual(rp.regime_v2(), "RISK_ON")
        rp.SNAP_PATH.write_text(json.dumps({"ts": time.time() - 9999, "agg_vol_5m": 999_999}))
        rp._snap_cache.update(ts=-1e9, agg=None)
        self.assertEqual(rp.regime_v2(), "RISK_ON")   # stale → hold, never flip blind


class TestCanaryOffByteIdentical(Base):
    def test_all_no_ops(self):
        for play in LEGACY_PLAYS:
            self.assertEqual(rp.admission_skip(1, play, None, 50_000), (False, "off"))
            self.assertEqual(rp.size_mult(1, play, None, 50_000), (1.0, "off"))
        self.assertEqual(rp.tape_mult(1), 1.0)
        self.assertIsNone(rp.exit_family({"insane_tier": "gem", "entry_liq": 10_000}, 1))
        self.assertFalse(rp.observer_blocks(1, "deep_pool"))


class TestAdmissionTruthTable(Base):
    """THE truth table: every old play × old regime → the v2 cell decision."""

    def test_full_grid(self):
        self.enable(1)
        for legacy_regime in LEGACY_REGIMES:
            # drive the live band to the shim target so the cell matches the legacy regime
            v2 = TABLE["regimes_v2"]["legacy_shim"][legacy_regime]
            agg = {"RISK_ON": 100_000, "NEUTRAL": 250_000, "RISK_OFF": 400_000}[v2]
            self._reset(); self.set_agg(agg); self.enable(1)
            for play in LEGACY_PLAYS:
                skip, why = rp.admission_skip(1, play, None, 60_000)
                if play in ("deep_pool", "brain_rule"):
                    self.assertTrue(skip, f"{play}×{legacy_regime} must be frozen")
                    self.assertIn("frozen", why)
                else:
                    self.assertTrue(skip, f"{play}×{legacy_regime} → SCALP×{v2} admit=skip")
                # the runner lane (any play label) admits everywhere under the belt
                skip_r, why_r = rp.admission_skip(1, play, "momentum_override", 60_000)
                if play in ("deep_pool", "brain_rule"):
                    self.assertTrue(skip_r, "frozen beats the lane flag")
                else:
                    self.assertFalse(skip_r, f"RUNNER×{v2} admits ({why_r})")

    def test_depth_belt(self):
        self.enable(1); self.set_agg(100_000)
        self.assertFalse(rp.admission_skip(1, "gem", "momentum_override", 249_999)[0])
        skip, why = rp.admission_skip(1, "gem", "momentum_override", 250_000)
        self.assertTrue(skip); self.assertIn("belt", why)
        # unknown depth fails open for an admitted row
        self.assertFalse(rp.admission_skip(1, "gem", "momentum_override", None)[0])

    def test_dust_never_blocked(self):
        self.enable(1)
        skip, why = rp.admission_skip(1, "gem", None, 60_000, dust=True)
        self.assertFalse(skip)
        self.assertEqual(why, "shadow")
        self.assertEqual(rp.play_v2("gem", dust_shadow=True), "DUST")

    def test_deep_frozen_beyond_table_reach(self):
        t = json.loads(json.dumps(TABLE))
        t["grid"]["DEEP"] = {r: {"admit": "full", "size_mult": 1.0} for r in rp.REGIMES}
        t["plays_v2"]["SCALP"]["from_plays"].append("deep_pool")     # tamper harder
        self.write_table(t); self.enable(1); self.set_agg(100_000)
        self.assertTrue(rp.admission_skip(1, "deep_pool", None, 100_000)[0])
        self.assertTrue(rp.admission_skip(1, "brain_rule", None, 100_000)[0])
        self.assertTrue(rp.admission_skip(1, "gem", "normal_slice", 100_000)[0])
        self.assertTrue(rp.observer_blocks(1, "deep_pool"))
        self.assertEqual(rp.size_mult(1, "deep_pool", None, 100_000)[0], 1.0)

    def test_observer_blocks_only_when_on(self):
        self.assertFalse(rp.observer_blocks(1, "deep_pool"))
        self.enable(1)
        self.assertTrue(rp.observer_blocks(1, "deep_pool"))
        self.assertTrue(rp.observer_blocks(1, "brain_rule"))
        self.assertFalse(rp.observer_blocks(1, "gem"))


class TestSizing(Base):
    def test_runner_cell_absorbs_lane_mult(self):
        self.enable(1); self.set_agg(100_000)
        m, why = rp.size_mult(1, "gem", "momentum_override", 8_000)
        self.assertEqual(m, 0.5)
        self.assertIn("RUNNER", why)

    def test_skip_cells_dont_zero_size(self):
        self.enable(1); self.set_agg(100_000)
        self.assertEqual(rp.size_mult(1, "gem", None, 60_000), (1.0, "admission-layer-owns"))

    def test_clamps(self):
        t = json.loads(json.dumps(TABLE))
        t["grid"]["RUNNER"]["RISK_ON"]["size_mult"] = 2.5     # upsize attempt
        t["tape_mult"] = 3.0
        self.write_table(t); self.enable(1); self.set_agg(100_000)
        self.assertEqual(rp.size_mult(1, "gem", "momentum_override", 8_000)[0], 1.0)
        self.assertEqual(rp.tape_mult(1), 1.0)


class TestExitFamilyEquivalence(Base):
    """family ≡ the legacy flag outcomes (_dp/_mo/_mg/_small) for a canary-ON bot."""

    CASES = [
        ({"insane_tier": "deep_pool", "entry_liq": 500_000}, "RIDE_TIGHT"),
        ({"insane_tier": "brain_rule", "entry_liq": 90_000}, "RIDE_TIGHT"),
        ({"insane_tier": "gem", "normal_slice": True, "entry_liq": 90_000}, "RIDE_TIGHT"),
        ({"insane_tier": "gem", "momentum_override": True, "entry_liq": 8_000}, "RIDE_WIDE"),
        ({"insane_tier": "gem", "entry_liq": 10_000}, "RIDE_WIDE"),                  # _small <50k
        ({"insane_tier": "gem", "entry_liq": 90_000, "momentum_at_entry": 5}, "RIDE_WIDE"),   # _mg
        ({"insane_tier": "momentum", "entry_liq": 200_000, "momentum_at_entry": 3}, "RIDE_WIDE"),
        ({"insane_tier": "gem", "entry_liq": 90_000, "momentum_at_entry": 1}, "SCALP_OUT"),   # no mg mom
        ({"insane_tier": "quick", "entry_liq": 90_000, "momentum_at_entry": 9}, "SCALP_OUT"), # tier not mg
        ({"insane_tier": "gem", "entry_liq": 300_000, "momentum_at_entry": 9}, "SCALP_OUT"),  # above mg band
        ({"insane_tier": "bank", "entry_liq": 90_000}, "BANK"),
        ({"insane_tier": "bank", "entry_liq": 10_000}, "RIDE_WIDE"),                 # small wins over bank
    ]

    def test_cases(self):
        self.enable(1)
        for pos, want in self.CASES:
            self.assertEqual(rp.exit_family(pos, 1), want, pos)

    def test_off_and_failopen(self):
        self.assertIsNone(rp.exit_family({"insane_tier": "gem", "entry_liq": 10_000}, 2))
        self.enable(1)
        rp.TABLE_PATH.write_text("{broken")
        self._reset()
        self.assertIsNone(rp.exit_family({"insane_tier": "gem", "entry_liq": 10_000}, 1))


class TestTagsAndFailOpen(Base):
    def test_tags_from_agg_and_shim(self):
        t = rp.tags("gem", momentum_override=True, agg_at_open=100_000)
        self.assertEqual(t, {"play_v2": "RUNNER", "regime_v2": "RISK_ON"})
        t = rp.tags("deep_pool", legacy_regime="aggressive")
        self.assertEqual(t, {"play_v2": "DEEP", "regime_v2": "RISK_OFF"})
        t = rp.tags("relay", agg_at_open=250_000)
        self.assertEqual(t, {"play_v2": "SCALP", "regime_v2": "NEUTRAL"})
        t = rp.tags("gem")                      # nothing known → play still tags
        self.assertEqual(t["play_v2"], "SCALP"); self.assertIsNone(t["regime_v2"])

    def test_malformed_everything_fails_open(self):
        (rp.BOTS_DIR / "bot1" / "regime_policy.json").write_text("{nope")
        self._reset()
        self.assertFalse(rp.admission_on(1))
        self.enable(1)
        rp.TABLE_PATH.unlink()
        self._reset()
        self.assertFalse(rp.admission_on(1))
        self.assertEqual(rp.admission_skip(1, "gem", None, 1000), (False, "off"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
