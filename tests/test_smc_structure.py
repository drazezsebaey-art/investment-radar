"""v65.2 golden tests - LuxAlgo-SMC-modelled structure engine."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import smc_structure as smc


def candles(closes, spread=0.004):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({"ts": i, "o": o, "h": max(o, c) * (1 + spread), "l": min(o, c) * (1 - spread), "c": c})
        prev = c
    return out


# up-leg -> peak -> down-leg breaking the last internal low -> bounce -> break of the lower high
PATH = ([10, 10.4, 10.8, 11.2, 11.0, 10.7, 10.9, 11.4, 11.9, 12.4, 12.9, 13.3,   # rally with a HL at ~10.7
         12.9, 12.5, 12.1, 11.7, 11.3, 11.6, 11.9, 11.5, 11.0, 10.6, 10.2,       # down, LH ~11.9, breaks HL
         10.5, 10.9, 11.4, 11.9, 12.3, 12.6, 12.8, 13.0, 13.1, 13.2])            # rally through the LH


class Engine(unittest.TestCase):
    def test_bear_choch_then_bull_choch_internal(self):
        a = smc.analyse(candles(PATH), internal_len=2, swing_len=40)
        ev = [(e["type"], e["dir"]) for e in a["events"] if e["layer"] == "internal"]
        self.assertIn(("CHoCH", "bear"), ev)
        i_bear = ev.index(("CHoCH", "bear"))
        self.assertIn(("CHoCH", "bull"), ev[i_bear + 1:])
        self.assertEqual(a["trend"]["internal"], 1)

    def test_breaks_need_a_close_not_a_wick(self):
        c = candles([10, 11, 12, 11, 10.5, 10.8, 11.2, 11.5, 11.8, 11.9])
        c[-1]["h"] = 13.0            # wick far above the 12 high, close stays below
        a = smc.analyse(c, internal_len=2, swing_len=40)
        self.assertFalse(any(e["dir"] == "bull" and e["level"] >= 12 for e in a["events"]))

    def test_first_break_is_bos_when_no_prior_trend(self):
        a = smc.analyse(candles([10, 10.5, 11, 10.6, 10.3, 10.7, 11.2, 11.6]), internal_len=2, swing_len=40)
        first = [e for e in a["events"] if e["layer"] == "internal"][0]
        self.assertEqual((first["type"], first["dir"]), ("BOS", "bull"))

    def test_bull_order_block_and_mitigation(self):
        a = smc.analyse(candles(PATH), internal_len=2, swing_len=40)
        self.assertTrue(a["bull_obs"])
        ob = a["bull_obs"][0]
        self.assertLess(ob["bottom"], ob["top"])
        # push price under the OB bottom -> mitigated
        c = candles(PATH + [12.0, 11.0, 10.0, 9.0, 8.5])
        a2 = smc.analyse(c, internal_len=2, swing_len=40)
        self.assertFalse(any(abs(o["bottom"] - ob["bottom"]) < 1e-9 for o in a2["bull_obs"]))

    def test_post_peak_read_resuming_flag(self):
        c = candles(PATH)
        old = smc.INTERNAL_LEN
        smc.INTERNAL_LEN = 2
        try:
            r = smc.post_peak_read(c, peak_ts=11)
        finally:
            smc.INTERNAL_LEN = old
        self.assertIn("last_internal_event", r)

    def test_high_volatility_bar_is_parsed(self):
        c = candles([10] * 210)
        c[-1].update(h=20.0, l=5.0)
        ph, pl = smc._parsed(c)
        self.assertEqual((ph[-1], pl[-1]), (5.0, 20.0))


if __name__ == "__main__":
    unittest.main()


class PineAlignment(unittest.TestCase):
    """v65.4 - behaviours taken from the LuxAlgo SMC Pine source."""

    def test_first_pivot_is_a_low(self):
        pts = smc._swing_points(candles([10, 11, 12, 11, 10, 11, 12, 13, 12, 11]), 2)
        self.assertEqual(pts[0][1], "btm")

    def test_atr_strict_is_none_until_length(self):
        a = smc._atr(candles([10] * 5), 200, strict=True)
        self.assertTrue(all(v is None for v in a))

    def test_trailing_labels_follow_swing_trend(self):
        a = smc.analyse(candles(PATH), internal_len=2, swing_len=3)
        t = a["trailing"]
        self.assertIn(t["top_label"], ("Strong High", "Weak High"))
        self.assertIsNotNone(t["equilibrium"])
        if a["trend"]["swing"] == 1:
            self.assertEqual((t["top_label"], t["bottom_label"]), ("Weak High", "Strong Low"))
