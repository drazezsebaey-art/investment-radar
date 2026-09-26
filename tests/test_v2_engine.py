"""
Golden test cases for scripts/v2_engine.py (scoring, gate, target framework, WATCH separation)
------------------------------------------------------------
Run with: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import v2_engine as v2  # noqa: E402


class TestV2Scoring(unittest.TestCase):
    def test_strongest_possible_candidate_scores_100_and_high_priority(self):
        coin = {
            "inducement": {"confidence": "high_quality"},
            "liquidity_sweep": {"confidence": "high_quality"},
            "fair_value_gaps": [{"quality": "exceptional", "mitigated": False}],
            "order_block": {"quality": "strong"},
            "real_structure": {"signal": "CHoCH_bullish"},
            "signal_quality": "idiosyncratic",
        }
        result = v2.score_v2_candidate(coin, {"risk_state": "risk_on"})
        self.assertEqual(result["v2_score"], 100)
        self.assertEqual(result["v2_decision_state"], "HIGH_PRIORITY_SETUP")
        self.assertEqual(result["v2_archetype"], "B_inducement_sweep_choch_fvg")

    def test_zero_evidence_scores_near_zero_and_no_trade(self):
        result = v2.score_v2_candidate({}, {"risk_state": "neutral"})
        self.assertEqual(result["v2_decision_state"], "NO_TRADE")

    def test_liquidity_sequence_prefers_inducement_over_bare_sweep(self):
        """Inducement and liquidity_sweep describe overlapping evidence -
        crediting both in full would double-count the same event."""
        coin = {"inducement": {"confidence": "high_quality"}, "liquidity_sweep": {"confidence": "detected"}}
        points, note = v2.score_liquidity_sequence(coin)
        self.assertEqual(points, 25)
        self.assertIn("inducement", note)

    def test_regime_is_a_bonus_not_a_veto(self):
        """A strong setup should survive a risk_off regime, just scored lower -
        matches V2's own stated principle that regime is context, not a gate."""
        coin = {
            "inducement": {"confidence": "high_quality"},
            "fair_value_gaps": [{"quality": "exceptional", "mitigated": False}],
            "order_block": {"quality": "strong"},
            "real_structure": {"signal": "CHoCH_bullish"},
            "signal_quality": "idiosyncratic",
        }
        risk_on = v2.score_v2_candidate(coin, {"risk_state": "risk_on"})
        risk_off = v2.score_v2_candidate(coin, {"risk_state": "risk_off"})
        self.assertEqual(risk_on["v2_score"] - risk_off["v2_score"], 20)  # +10 vs -10
        self.assertEqual(risk_off["v2_decision_state"], "HIGH_PRIORITY_SETUP")  # still passes despite the penalty


class TestV2EntryQualityGate(unittest.TestCase):
    def test_clean_candidate_passes(self):
        coin = {
            "real_candles": [[0, 1, 2, 3, 4]] * 20, "volume_24h_usd": 5_000_000,
            "latest_funding_rate": 0.0001, "oi_price_relationship": "confirms",
            "volume_confirmed": True, "opportunity_lifecycle": {"decay_state": "fresh"},
            "real_structure": {"signal": "BOS_bullish"},
        }
        self.assertEqual(v2.apply_v2_entry_quality_gate(coin), [])

    def test_illiquid_candidate_is_rejected(self):
        coin = {"real_candles": [[0, 1, 2, 3, 4]] * 20, "volume_24h_usd": 50_000}
        self.assertIn("POOR_LIQUIDITY", v2.apply_v2_entry_quality_gate(coin))

    def test_contradictory_evidence_is_rejected(self):
        coin = {
            "real_candles": [[0, 1, 2, 3, 4]] * 20, "volume_24h_usd": 5_000_000,
            "real_structure": {"signal": "BOS_bearish"}, "inducement": {"confidence": "high_quality"},
        }
        self.assertIn("CONTRADICTORY_EVIDENCE", v2.apply_v2_entry_quality_gate(coin))

    def test_target_timeframe_too_slow_is_rejected(self):
        tf = {"any_target_feasible": False}
        self.assertIn("TARGET_TIMEFRAME_TOO_SLOW", v2.apply_v2_entry_quality_gate({}, tf))

    def test_empty_candidate_degrades_gracefully(self):
        # should never crash on missing fields - just accumulates the checkable reasons
        reasons = v2.apply_v2_entry_quality_gate({})
        self.assertIn("DATA_QUALITY_INSUFFICIENT", reasons)
        self.assertIn("POOR_LIQUIDITY", reasons)


class TestV2TargetFramework(unittest.TestCase):
    def test_quiet_coin_still_feasible_in_extended_window(self):
        """Azez's explicit correction (24/9/2026): don't reject a slower-but-
        real opportunity just because it can't hit a fast target."""
        coin = {"price_usd": 100, "atr_value": 0.5, "all_resistance_levels": []}
        tf = v2.compute_v2_target_framework(coin)
        feasible_labels = [t["horizon_label"] for t in tf["targets"] if t["feasible"]]
        self.assertIn("extended", feasible_labels)

    def test_dead_coin_is_infeasible_on_every_horizon(self):
        coin = {"price_usd": 100, "atr_value": 0.05, "all_resistance_levels": []}
        tf = v2.compute_v2_target_framework(coin)
        self.assertFalse(tf["any_target_feasible"])

    def test_no_ceiling_on_the_upside(self):
        """Azez's explicit instruction: don't cap the target size - a highly
        volatile coin's projected target should stand as computed."""
        coin = {"price_usd": 100, "atr_value": 5.0, "all_resistance_levels": []}
        tf = v2.compute_v2_target_framework(coin)
        fast_target = next(t for t in tf["targets"] if t["horizon_label"] == "fast")
        self.assertGreater(fast_target["target_pct"], 8.0)  # would have been capped at 8% in the old design

    def test_stop_is_atr_relative_not_a_fixed_percentage(self):
        coin_quiet = {"price_usd": 100, "atr_value": 1.0, "all_resistance_levels": []}
        coin_volatile = {"price_usd": 100, "atr_value": 5.0, "all_resistance_levels": []}
        tf_quiet = v2.compute_v2_target_framework(coin_quiet)
        tf_volatile = v2.compute_v2_target_framework(coin_volatile)
        self.assertNotEqual(tf_quiet["stop"], tf_volatile["stop"])


class TestV2WatchSeparation(unittest.TestCase):
    """Locks in Azez's adoption of the audit report's objection (24/9/2026):
    WATCH must never open an executable paper trade - only HIGH_PRIORITY_SETUP
    can. A WATCH-tier candidate is recorded as a separate observation object."""

    def test_watch_is_not_in_tradeable_states(self):
        self.assertNotIn("WATCH", v2.V2_TRADEABLE_STATES)
        self.assertIn("HIGH_PRIORITY_SETUP", v2.V2_TRADEABLE_STATES)

    def test_watch_is_in_its_own_watch_states(self):
        self.assertIn("WATCH", v2.V2_WATCH_STATES)
        self.assertNotIn("HIGH_PRIORITY_SETUP", v2.V2_WATCH_STATES)


if __name__ == "__main__":
    unittest.main()
