"""v65.3 golden tests - break validation, sweeps, premium/discount, volume profile, invalidation."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import smc_structure as smc


def bar(i, o, h, l, c, vq=100.0):
    return {"ts": i, "o": o, "h": h, "l": l, "c": c, "vq": vq}


def base_with_low(n_after=8):
    """Down to a swing low at 9.0 (bar 5), bounce, then drift back toward it."""
    closes = [10.0, 9.8, 9.6, 9.4, 9.2, 9.05, 9.3, 9.6, 9.9, 10.1, 10.0, 9.8, 9.6, 9.4, 9.3]
    c = []
    for i, cl in enumerate(closes):
        o = closes[i - 1] if i else cl
        c.append(bar(i, o, max(o, cl) + 0.05, min(o, cl) - 0.05, cl))
    c[5]["l"] = 9.0
    return c


class Validation(unittest.TestCase):
    def test_wick_through_low_and_close_back_is_sweep_and_hold_signal(self):
        c = base_with_low()
        c.append(bar(15, 9.3, 9.35, 8.80, 9.25))      # wick under 9.0, close back above
        v = smc.validate_breaks(c, n=2)
        sweeps = [x for x in v if x["side"] == "low" and x["result"] == "SWEEP"]
        self.assertTrue(sweeps)
        self.assertTrue(smc.hold_signal(c, v)["bullish_hold_signal"])

    def test_decisive_close_below_is_confirmed_break(self):
        c = base_with_low()
        c.append(bar(15, 9.3, 9.3, 8.2, 8.3))         # closes far below 9.0 (beyond ATR zone)
        v = smc.validate_breaks(c, n=2)
        self.assertTrue(any(x["side"] == "low" and x["result"] == "CONFIRMED" for x in v))
        self.assertFalse(smc.hold_signal(c, v)["bullish_hold_signal"])

    def test_close_just_through_then_back_is_failed_break_sweep(self):
        c = base_with_low()
        c.append(bar(15, 9.3, 9.3, 8.95, 8.98))       # closes barely below 9.0 (inside zone)
        c.append(bar(16, 8.98, 9.2, 8.97, 9.15))      # next bar closes back above -> failed break
        v = smc.validate_breaks(c, n=2)
        self.assertTrue(any(x["side"] == "low" and x["result"] == "SWEEP" for x in v))


class Zones(unittest.TestCase):
    def test_premium_discount(self):
        self.assertEqual(smc.premium_discount(0.28, 0.21, 0.3725)["zone"], "DISCOUNT")
        self.assertEqual(smc.premium_discount(0.34, 0.21, 0.3725)["zone"], "PREMIUM")
        self.assertEqual(smc.premium_discount(0.26, 0.21, 0.3725)["zone"], "DEEP_DISCOUNT")
        self.assertIsNone(smc.premium_discount(1, None, 2))

    def test_volume_profile_poc_at_heavy_level(self):
        c = [bar(i, 10, 10.2, 9.8, 10, 1000.0) for i in range(50)] + \
            [bar(50 + i, 12, 12.2, 11.8, 12, 10.0) for i in range(50)]
        vp = smc.volume_profile(c, lookback=100, bins=20)
        self.assertLess(abs(vp["poc"] - 10.0), 0.3)
        self.assertTrue(vp["value_area_low"] <= vp["poc"] <= vp["value_area_high"])

    def test_structure_invalidation_is_latest_unbroken_low(self):
        c = base_with_low()
        r = smc.structure_invalidation(c, n=2)
        self.assertEqual(r["long_invalidation_level"], 9.0)
        c.append(bar(15, 9.3, 9.3, 8.5, 8.6))        # close below 9.0 -> that low is broken
        self.assertNotEqual(smc.structure_invalidation(c, n=2)["long_invalidation_level"], 9.0)

    def test_levels_report_shape(self):
        c = base_with_low() + [bar(15, 9.3, 9.35, 8.8, 9.25)]
        old = smc.INTERNAL_LEN
        r = smc.levels_report(c, 8.0, 11.0)
        self.assertIn("volume_profile_30d", r)
        self.assertIn("premium_discount_impulse", r)


if __name__ == "__main__":
    unittest.main()
