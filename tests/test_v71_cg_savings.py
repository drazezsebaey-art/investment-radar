"""v71: ATH price cache (72h) with live distance; CoinGecko fallback cache (6h)."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import breakout_check as bc  # noqa: E402


class TestATH(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "ath.json"
        self.p = mock.patch.object(bc, "ATH_CACHE_PATH", self.tmp); self.p.start()
    def tearDown(self):
        self.p.stop()

    def test_live_then_cached_distance_moves_with_price(self):
        with mock.patch.object(bc, "_fetch_coin_market_data_live", return_value=(100.0, -50.0)) as live:
            self.assertAlmostEqual(bc.fetch_coin_market_data("x", 50.0, 55.0), -50.0)
            self.assertAlmostEqual(bc.fetch_coin_market_data("x", 80.0, 82.0), -20.0)   # no new call, live price used
            self.assertEqual(live.call_count, 1)

    def test_new_high_updates_ath_locally(self):
        with mock.patch.object(bc, "_fetch_coin_market_data_live", return_value=(100.0, 0.0)) as live:
            bc.fetch_coin_market_data("x", 99.0, 99.5)
            self.assertAlmostEqual(bc.fetch_coin_market_data("x", 108.0, 110.0), (108 / 110 - 1) * 100)
            self.assertEqual(json.loads(self.tmp.read_text())["x"]["ath"], 110.0)
            self.assertEqual(live.call_count, 1)

    def test_refresh_after_72h_and_old_cache_format(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
        self.tmp.write_text(json.dumps({"x": {"ath": 100.0, "at": old}, "y": {"ath_change": -40, "at": old}}))
        with mock.patch.object(bc, "_fetch_coin_market_data_live", return_value=(120.0, -50.0)) as live:
            self.assertAlmostEqual(bc.fetch_coin_market_data("x", 60.0, 61.0), -50.0)
            bc.fetch_coin_market_data("y", 60.0, 61.0)
            self.assertEqual(live.call_count, 2)


class TestFallbackCache(unittest.TestCase):
    def test_second_call_within_6h_is_free(self):
        tmp = Path(tempfile.mkdtemp()) / "fb.json"
        with mock.patch.object(bc, "CG_FALLBACK_CACHE_PATH", tmp), \
             mock.patch.object(bc, "okx_ohlc_4h", side_effect=RuntimeError("no OKX market")), \
             mock.patch.object(bc, "fetch_json", return_value=[[1, 1, 2, 0.5, 1.5]]) as fj:
            bc.fetch_ohlc("coin", "COIN"); bc.fetch_ohlc("coin", "COIN")
            self.assertEqual(fj.call_count, 1)


if __name__ == "__main__":
    unittest.main()
