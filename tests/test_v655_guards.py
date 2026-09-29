"""v65.5 golden tests - minimum stop distance + pegged-asset exclusion."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import auto_paper_trade as apt
import scalp_signals as ss
import v2_engine as v2


class Guards(unittest.TestCase):
    def test_pegged_detection_all_engines(self):
        for mod in (apt, ss, v2):
            self.assertTrue(mod.is_pegged({"id": "ethena-usde", "symbol": "USDe"}))
            self.assertTrue(mod.is_pegged({"id": "x", "symbol": "PAXG"}))
            self.assertFalse(mod.is_pegged({"id": "near", "symbol": "NEAR"}))

    def test_pick_stop_skips_too_tight_and_falls_back(self):
        coin = {"suggested_stop_resistance_based": 99.5,   # 0.5% - too tight
                "suggested_stop_trendline_based": 95.0}     # 5%  - valid
        stop, tf = apt.pick_stop(coin, 100.0)
        self.assertEqual(stop, 95.0)

    def test_pick_stop_none_when_only_tight(self):
        stop, _ = apt.pick_stop({"suggested_stop_resistance_based": 99.9}, 100.0)
        self.assertIsNone(stop)

    def test_tight_trend_following_stop_ignored(self):
        coin = {"trend_following_eligible": True, "trend_following_stop": 99.0,
                "suggested_stop_resistance_based": 96.0}
        self.assertEqual(apt.pick_stop(coin, 100.0), (96.0, False))

    def test_scalp_fast_reject_tight_stop(self):
        r = ss.fast_reject_reason({"indicators": {"rsi14": 50}}, 100.0, 99.0, [102.0, 103.0])
        self.assertIn("min stop distance", r)
        self.assertIsNone(ss.fast_reject_reason({"indicators": {"rsi14": 50}}, 100.0, 97.0, [106.0]))


if __name__ == "__main__":
    unittest.main()
