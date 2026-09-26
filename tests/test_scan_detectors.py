"""
Golden test cases for scripts/scan.py's Layer 2 pattern detectors
------------------------------------------------------------
The double_bottom test here is a REGRESSION LOCK: v36 (24/9/2026) tightened
its thresholds after the original ones were measured firing on 28% of a
real 264-coin sample - a bug that pushed run time past an hour by flooding
priority_review escalation. This test exists specifically so a future
"let's loosen this a bit" edit can't silently reintroduce that failure
mode without a test breaking first.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scan  # noqa: E402


def candle(o, h, l, c, t=0):
    """scan.py's synthetic candles are DICTS ({'open','high','low','close','t'}),
    unlike breakout_check.py's real candles which are lists - this
    inconsistency is itself flagged in the 24/9/2026 audit report (item
    29-30, shared schema needed) - matching the production format exactly
    here rather than "fixing" it is deliberate, so this test stays a real
    regression check against the code as it exists today."""
    return {"open": o, "high": h, "low": l, "close": c, "t": t}


class TestDoubleBottomThresholds(unittest.TestCase):
    def test_loose_near_lows_are_rejected_by_v36_thresholds(self):
        """Two lows within the OLD 3% tolerance but outside the NEW 1.2%
        tolerance must NOT fire - this is exactly the class of noise that
        caused the v36 incident."""
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100.0, 101)]          # low #1 = 100.0
        candles += [candle(103, 106, 102, 105), candle(105, 107, 103, 106)]
        candles += [candle(103, 104, 102.5, 103)]          # low #2 = 102.5 -> 2.5% apart, inside OLD 3% but outside NEW 1.2%
        candles += [candle(103, 105, 102, 104)]
        result = scan.detect_double_bottom(candles)
        self.assertIsNone(result)

    def test_genuinely_close_lows_with_clear_neckline_still_fire(self):
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100.0, 101)]           # low #1 = 100.0 (index 3)
        candles += [candle(103, 108, 102, 107), candle(107, 109, 105, 108),
                    candle(107, 108, 104, 106), candle(106, 107, 103, 105)]  # neckline area, keeps separation >= 5
        candles += [candle(103, 104, 100.5, 103)]           # low #2 = 100.5 (index 8) -> separation 5, 0.5% apart
        candles += [candle(103, 105, 102, 104), candle(104, 106, 103, 105)]  # trailing padding for the swing window
        result = scan.detect_double_bottom(candles)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["lower_low"], 100.0)

    def test_neckline_too_close_to_lows_is_rejected(self):
        """v36 also requires the neckline to clear the lows by >=2% -
        without this, a nearly-flat 'neckline' isn't a meaningful reversal
        structure."""
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100.0, 101)]
        candles += [candle(101, 100.9, 100.3, 100.6), candle(100.6, 100.85, 100.2, 100.5)]  # neckline barely above the lows
        candles += [candle(101, 101.5, 100.2, 101)]
        candles += [candle(101, 102, 100.5, 101.5)]
        result = scan.detect_double_bottom(candles)
        self.assertIsNone(result)

    def test_too_short_separation_between_lows_is_rejected(self):
        """v36 also raised the minimum candle separation from 3 to 5."""
        candles = [candle(105, 106, 104, 105) for _ in range(3)]
        candles += [candle(102, 103, 100.0, 101)]
        candles += [candle(103, 108, 102, 107)]
        candles += [candle(103, 104, 100.3, 103)]   # only 2 candles after low #1 - under the new 5-candle minimum
        candles += [candle(103, 105, 102, 104)]
        result = scan.detect_double_bottom(candles)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
