"""
v67 golden tests - data-gap hygiene and asset exclusion.
Locks in: (1) a >2h hole in price-history starts a 6h warm-up and a normal
30-min spacing does not; (2) stored score streaks are cleared after a long
pause but survive the normal credit-gate spacing (including throttled 12h);
(3) pegged assets and tokenized equities never reach radar/pre-pump lists.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import asset_filters as af  # noqa: E402
import scan  # noqa: E402
import breakout_check as bc  # noqa: E402
import prepump_watchlist as pp  # noqa: E402
import digest  # noqa: E402

NOW = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)


class TestAssetFilters(unittest.TestCase):
    def test_pegged(self):
        self.assertEqual(af.exclusion_reason("pax-gold", "PAXG"), "PEGGED_ASSET")
        self.assertEqual(af.exclusion_reason("usd-coin", "USDC"), "PEGGED_ASSET")
        self.assertEqual(af.exclusion_reason("ripple-usd", None), "PEGGED_ASSET")

    def test_tokenized_equity(self):
        self.assertEqual(af.exclusion_reason("strategy-pp-variable-xstock", "STRCX"), "TOKENIZED_EQUITY")

    def test_normal_crypto_passes(self):
        for cid, sym in [("edgex", "EDGE"), ("starknet", "STRK"), ("ripple", "XRP"), ("pump-fun", "PUMP")]:
            self.assertIsNone(af.exclusion_reason(cid, sym), cid)


class TestWarmup(unittest.TestCase):
    def test_long_gap_starts_warmup(self):
        st = scan.update_warmup_state({}, NOW - timedelta(hours=83), NOW)
        self.assertTrue(st["active"])
        self.assertEqual(st["gap_hours"], 83.0)
        self.assertEqual(datetime.fromisoformat(st["warmup_until"]), NOW + timedelta(hours=scan.WARMUP_DURATION_HOURS))

    def test_normal_spacing_no_warmup(self):
        st = scan.update_warmup_state({}, NOW - timedelta(minutes=30), NOW)
        self.assertFalse(st["active"])

    def test_warmup_persists_then_expires(self):
        st = scan.update_warmup_state({}, NOW - timedelta(hours=83), NOW)
        later = NOW + timedelta(hours=3)
        self.assertTrue(scan.update_warmup_state(st, later - timedelta(minutes=30), later)["active"])
        done = NOW + timedelta(hours=scan.WARMUP_DURATION_HOURS, minutes=1)
        self.assertFalse(scan.update_warmup_state(st, done - timedelta(minutes=30), done)["active"])

    def test_last_point_time(self):
        h = {"bitcoin": [{"t": "2026-09-27T02:00:00+00:00", "price": 1}, {"t": "bad", "price": 2}]}
        self.assertEqual(scan.last_point_time(h), datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc))
        self.assertIsNone(scan.last_point_time({}))


class TestStreakReset(unittest.TestCase):
    STREAKS = {"stellar": 69, "bittensor": 60, "sui": 43, "near": 0}

    def test_reset_after_outage(self):
        meta = {"last_update": "2026-09-27T02:00:00+00:00"}
        new, info = bc.reset_streaks_after_gap(dict(self.STREAKS), meta, NOW, 3)
        self.assertTrue(all(v == 0 for v in new.values()))
        self.assertEqual(info["streaks_cleared"], 3)

    def test_normal_gate_spacing_keeps_streaks(self):
        meta = {"last_update": (NOW - timedelta(hours=3)).isoformat()}
        new, info = bc.reset_streaks_after_gap(dict(self.STREAKS), meta, NOW, 3)
        self.assertIsNone(info)
        self.assertEqual(new["stellar"], 69)

    def test_throttled_gate_does_not_wipe(self):
        meta = {"last_update": (NOW - timedelta(hours=12)).isoformat()}
        _, info = bc.reset_streaks_after_gap(dict(self.STREAKS), meta, NOW, 12)
        self.assertIsNone(info)
        self.assertEqual(bc.streak_gap_threshold_hours(12), 30.0)
        self.assertEqual(bc.streak_gap_threshold_hours(3), 8.0)

    def test_no_meta_no_reset(self):
        _, info = bc.reset_streaks_after_gap(dict(self.STREAKS), {}, NOW, 3)
        self.assertIsNone(info)


class TestDownstreamFilters(unittest.TestCase):
    def test_prepump_drops_tokenized_equity(self):
        coins = {"strategy-pp-variable-xstock": {"symbol": "STRCX", "change_7d_pct": -2, "market_cap_usd": 1.5e8},
                 "edgex": {"symbol": "EDGE", "change_7d_pct": 2, "market_cap_usd": 2e8}}
        sig = {k: {"C": ["x"], "E": ["y"]} for k in coins}
        ids = [c["coin"] for c in pp.candidates(coins, sig)]
        self.assertEqual(ids, ["edgex"])

    def test_digest_tradeable_keys(self):
        self.assertTrue(digest.is_tradeable_key("ethereum"))
        self.assertFalse(digest.is_tradeable_key("MetaMask Wallet"))
        self.assertFalse(digest.is_tradeable_key("Titan Builder"))
        self.assertFalse(digest.is_tradeable_key("ripple-usd"))
        self.assertFalse(digest.is_tradeable_key("protocol:foo"))


if __name__ == "__main__":
    unittest.main()
