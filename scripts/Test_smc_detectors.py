"""
Golden test cases for scripts/breakout_check.py's real-candle SMC detectors
------------------------------------------------------------
Per the 24/9/2026 audit report (item 65-67): these lock in the exact
behavior verified by hand during development (22-24/9/2026) so a future
change to swing-detection or any shared helper can't silently break a
detector that was already proven correct. Run with:

    python -m unittest discover -s tests -v

Candle format throughout: [timestamp, open, high, low, close] - matches
CoinGecko's OHLC response shape used by fetch_ohlc() in production.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import breakout_check as bc  # noqa: E402


def candle(o, h, l, c, ts=0):
    return [ts, o, h, l, c]


class TestFairValueGap(unittest.TestCase):
    def test_exceptional_unmitigated_gap(self):
        candles = [
            candle(100, 101, 99, 100), candle(100, 102, 99, 101), candle(101, 102, 100, 101),
            candle(101, 103, 100, 102),   # c1: high=103
            candle(102, 115, 101, 114),   # c2: displacement, body 12/range 14
            candle(114, 116, 110, 115),   # c3: low=110 -> gap 103..110
            candle(115, 117, 114, 116), candle(116, 118, 115, 117),
        ]
        gaps = bc.detect_fair_value_gaps(candles, atr_value=5.0)
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["gap_low"], 103)
        self.assertEqual(gap["gap_high"], 110)
        self.assertEqual(gap["quality"], "exceptional")
        self.assertFalse(gap["mitigated"])
        self.assertTrue(gap["fresh"])

    def test_mitigated_gap_downgrades_quality(self):
        candles = [
            candle(100, 101, 99, 100), candle(100, 102, 99, 101), candle(101, 102, 100, 101),
            candle(101, 103, 100, 102), candle(102, 115, 101, 114), candle(114, 116, 110, 115),
            candle(115, 117, 105, 116),   # dips back to 105 - inside the [103,110] gap
            candle(116, 118, 115, 117),
        ]
        gaps = bc.detect_fair_value_gaps(candles, atr_value=5.0)
        self.assertTrue(gaps[0]["mitigated"])
        self.assertFalse(gaps[0]["fresh"])
        self.assertEqual(gaps[0]["quality"], "moderate")

    def test_no_gap_when_candles_overlap(self):
        flat = [candle(100, 102, 98, 100, i) for i in range(10)]
        self.assertEqual(bc.detect_fair_value_gaps(flat, atr_value=5.0), [])

    def test_returns_empty_without_atr(self):
        candles = [candle(100 + i, 101 + i, 99 + i, 100 + i, i) for i in range(10)]
        self.assertEqual(bc.detect_fair_value_gaps(candles, atr_value=None), [])

    def test_returns_empty_with_too_few_candles(self):
        self.assertEqual(bc.detect_fair_value_gaps([candle(1, 2, 0, 1)], atr_value=1.0), [])


class TestLiquiditySweep(unittest.TestCase):
    def test_high_quality_genuine_pool_plus_displacement(self):
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100, 101), candle(103, 104, 102, 103), candle(104, 105, 102, 103)]
        candles += [candle(102, 103, 100.3, 101.5), candle(103, 104, 102, 103), candle(104, 105, 102, 103)]
        candles += [candle(103, 105, 102, 104), candle(104, 106, 103, 105)]
        candles += [candle(103, 104, 98, 101.5)]     # sweep: wicks below the ~100 pool, closes back above
        candles += [candle(101.5, 110, 101, 109)]    # real displacement after
        candles += [candle(109, 111, 108, 110)]
        result = bc.detect_liquidity_sweep(candles, atr_value=3.0)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_genuine_pool"])
        self.assertEqual(result["n_equal_lows"], 2)
        self.assertTrue(result["displacement_confirmed"])
        self.assertEqual(result["confidence"], "high_quality")

    def test_detected_only_no_pool_no_displacement(self):
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100, 101)]
        fillers = [102.5, 103.1, 102.8, 103.4, 102.6, 103.2]
        candles += [candle(103, 105, f, 104) for f in fillers]
        candles += [candle(103, 104, 98, 101.5)]   # sweep, weak follow-through only
        candles += [candle(101.5, 102.0, 101.3, 101.8)]   # genuinely weak: range=0.7, well under 0.5*ATR(3.0)=1.5
        result = bc.detect_liquidity_sweep(candles, atr_value=3.0)
        self.assertIsNotNone(result)
        self.assertFalse(result["is_genuine_pool"])
        self.assertFalse(result["displacement_confirmed"])
        self.assertEqual(result["confidence"], "detected")

    def test_no_sweep_on_flat_series(self):
        flat = [candle(100, 101, 99, 100, i) for i in range(15)]
        self.assertIsNone(bc.detect_liquidity_sweep(flat, atr_value=3.0))


class TestInducement(unittest.TestCase):
    def test_high_quality_two_stage_sweep(self):
        candles = [candle(105, 106, 104.5, 105) for _ in range(7)]
        candles += [candle(103, 104, 101, 102)]                              # L1 = 101 (the bait)
        candles += [candle(102, 105, 101.5, 104), candle(104, 107, 103, 106)]  # genuine bounce above L1
        candles += [candle(105, 106, 102, 103), candle(103, 104, 100.5, 101.2)]
        candles += [candle(101, 102, 97, 98)]      # L2 = 97, sweeps below L1 too
        candles += [candle(98, 108, 97.5, 107)]    # displacement, closes back above L1
        candles += [candle(107, 110, 106, 109)]
        result = bc.detect_inducement(candles, atr_value=3.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["induced_level"], 101)
        self.assertEqual(result["swept_level"], 97)
        self.assertTrue(result["genuine_structure"])
        self.assertTrue(result["displacement_confirmed"])
        self.assertEqual(result["confidence"], "high_quality")

    def test_none_when_second_low_not_deeper(self):
        # a single flat-ish series with no L2 < L1 sequence should not fire
        candles = [candle(100 + (i % 3) * 0.1, 101, 99, 100, i) for i in range(20)]
        self.assertIsNone(bc.detect_inducement(candles, atr_value=3.0))

    def test_none_without_atr_or_too_few_candles(self):
        self.assertIsNone(bc.detect_inducement([candle(1, 2, 0, 1)] * 5, atr_value=None))
        self.assertIsNone(bc.detect_inducement([candle(1, 2, 0, 1)] * 5, atr_value=1.0))


class TestOrderBlock(unittest.TestCase):
    def setUp(self):
        import random
        random.seed(3)
        self.padding = [
            candle(105 + random.uniform(-.2, .2), 106 + random.uniform(-.1, .3),
                   104.5 + random.uniform(-.2, .2), 105)
            for _ in range(4)
        ]
        self.ob_candle = candle(103, 104, 101, 101.5)          # last bearish candle
        self.displacement = candle(101.5, 110, 101.3, 109)     # strong bullish displacement
        self.after_fresh = [candle(109, 111, 108, 110), candle(110, 112, 109, 111)]

    def test_fresh_strong_order_block(self):
        candles = self.padding + [self.ob_candle, self.displacement] + self.after_fresh
        result = bc.detect_order_block(candles, atr_value=3.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["ob_low"], 101)
        self.assertEqual(result["ob_high"], 104)
        self.assertFalse(result["mitigated"])
        self.assertEqual(result["quality"], "strong")

    def test_mitigated_order_block_is_weak(self):
        after_mitigated = [candle(109, 111, 102, 110)]  # dips back into [101,104]
        candles = self.padding + [self.ob_candle, self.displacement] + after_mitigated
        result = bc.detect_order_block(candles, atr_value=3.0)
        self.assertTrue(result["mitigated"])
        self.assertEqual(result["quality"], "weak")

    def test_no_order_block_without_a_bearish_origin_candle(self):
        all_bullish = [candle(100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100.5 + i * 0.1, i) for i in range(10)]
        self.assertIsNone(bc.detect_order_block(all_bullish, atr_value=3.0))


class TestRealStructure(unittest.TestCase):
    @staticmethod
    def _zigzag(swing_points):
        candles = []
        prev = swing_points[0][1]
        for _, price in swing_points[1:]:
            for s in range(1, 4):
                level = prev + (price - prev) * s / 3
                o = level - (price - prev) / 3 * 0.3
                c = level
                h, l = max(o, c) + 0.4, min(o, c) - 0.4
                candles.append(candle(o, h, l, c))
            prev = price
        return candles

    def test_uptrend_break_is_bos_bullish(self):
        swings = [(False, 100), (True, 110), (False, 104), (True, 118), (False, 108), (True, 125)]
        candles = self._zigzag(swings) + [candle(120, 131, 119, 130)]  # closes above the last swing high
        result = bc.detect_structure_real(candles)
        self.assertIsNotNone(result)
        self.assertEqual(result["prior_trend"], "up")
        self.assertEqual(result["signal"], "BOS_bullish")

    def test_downtrend_break_above_is_choch_bullish(self):
        swings = [(True, 130), (False, 118), (True, 126), (False, 110), (True, 120), (False, 104)]
        candles = self._zigzag(swings) + [candle(115, 126, 114, 125)]  # closes above the last (lower) high
        result = bc.detect_structure_real(candles)
        self.assertIsNotNone(result)
        self.assertEqual(result["prior_trend"], "down")
        self.assertEqual(result["signal"], "CHoCH_bullish")

    def test_none_on_empty_or_too_short(self):
        self.assertIsNone(bc.detect_structure_real([]))
        self.assertIsNone(bc.detect_structure_real([candle(1, 2, 0, 1)] * 5))


if __name__ == "__main__":
    unittest.main()
