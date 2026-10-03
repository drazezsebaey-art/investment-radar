"""Execution simulator: the playbooks' ordering/golden cases that belong to the engine."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lab"))
from engine.execution import Spec, Candle, simulate, simulate_both  # noqa: E402

H4 = 14_400_000


def C(i, o, h, l, c, complete=True):
    return Candle(i * H4, o, h, l, c, complete)


def spec(**kw):
    base = dict(signal_time=0, stop=95.0, targets=[], target_fracs=[0.5, 0.5], time_stop_candles=30,
                cost=0.0026, slippage=0.0003, atr=4.0,
                targets_fn=lambda f, r: [f + 1.5 * r + 0.0026 * f, f + 3 * r + 0.0026 * f])
    base.update(kw)
    return Spec(**base)


class T(unittest.TestCase):
    def test_G1_market_entry_t1_then_t2(self):
        cs = [C(1, 100, 101, 99, 100.5), C(2, 100.5, 108, 100, 107), C(3, 107, 116, 106, 115)]
        r = simulate(spec(), cs, H4)
        self.assertEqual(r["fill_price"], 100)
        self.assertEqual([e[2] for e in r["exits"]], ["T1", "T2"])
        self.assertGreater(r["r_net"], 2)

    def test_G4_stop_and_t1_same_candle_is_stop(self):
        cs = [C(1, 100, 101, 99, 100), C(2, 100, 109, 94, 100)]
        r = simulate_both(spec(), cs, H4)
        self.assertEqual(r["exit_reason"], "STOP")
        self.assertIn("AMBIGUOUS", r["flags"])
        self.assertGreater(r["optimistic_r_net"], r["r_net"])

    def test_entry_candle_targets_do_not_count(self):
        cs = [C(1, 100, 120, 99, 101), C(2, 101, 102, 100, 101)]
        r = simulate(spec(time_stop_candles=2), cs, H4)
        self.assertEqual(r["exit_reason"], "TIME")

    def test_G6_time_stop_at_close_of_candle_30(self):
        cs = [C(i, 100, 101, 99, 100.2) for i in range(1, 40)]
        r = simulate(spec(), cs, H4)
        self.assertEqual(r["exits"][-1][3], 30 * H4)          # candle 30 = entry candle + 29

    def test_G8_gap_below_stop_exits_at_open(self):
        cs = [C(1, 100, 101, 99, 100), C(2, 90, 92, 89, 91)]
        r = simulate(spec(), cs, H4)
        self.assertEqual((r["exit_reason"], r["exits"][-1][1]), ("STOP_GAP", 90))

    def test_G9_t1_intrabar_then_failure_close(self):
        fail = lambda c, s: c.c < 99                      # failure rule applies only before T1 (hit==0)
        cs = [C(1, 100, 101, 99.5, 100), C(2, 100, 108.5, 98, 98.5), C(3, 98.4, 99, 97, 98)]
        r = simulate(spec(failure_fn=fail), cs, H4)
        self.assertEqual(r["exits"][0][2], "T1")           # half at T1, stop moved to breakeven
        self.assertEqual(r["exits"][1][2], "STOP_GAP")     # next open 98.4 < breakeven -> gap exit

    def test_failure_close_exits_next_open(self):
        fail = lambda c, s: c.c < 99
        cs = [C(1, 100, 101, 99.5, 100), C(2, 100, 100.5, 98, 98.5), C(3, 98.6, 99, 97, 98)]
        r = simulate(spec(failure_fn=fail), cs, H4)
        self.assertEqual((r["exit_reason"], r["exits"][-1][1]), ("FAILURE", 98.6))

    def test_G11_gap_above_t1(self):
        cs = [C(1, 100, 101, 99, 100), C(2, 109, 110, 108, 109)]
        r = simulate(spec(time_stop_candles=2), cs, H4)
        self.assertEqual((r["exits"][0][2], r["exits"][0][1]), ("T1_GAP", 109))

    def test_G12_entry_gap_invalid(self):
        cs = [C(1, 110, 111, 109, 110)]                     # stop 95 -> 13.6% > 8%
        r = simulate(spec(), cs, H4)
        self.assertEqual((r["status"], r["reason"], r["sub_reason"]), ("INVALID", "ENTRY_GAP", "stop_too_wide"))

    # ---- limit entries (T2) ----
    def lim(self, **kw):
        return spec(entry="limit", limit_price=100.0, stop=96.0, cancel_close_below=96.5,
                    cancel_if_high_reaches=106.0, **kw)

    def test_G7_touch_is_not_a_fill(self):
        cs = [C(1, 101, 102, 100.0, 101)] * 6
        self.assertEqual(simulate(self.lim(), cs, H4)["status"], "EXPIRED_UNFILLED")

    def test_trade_through_fills_at_limit(self):
        cs = [C(1, 101, 102, 99.9, 101), C(2, 101, 102, 100.5, 101.5)]
        self.assertEqual(simulate(self.lim(time_stop_candles=2), cs, H4)["fill_price"], 100.0)

    def test_G15_fill_and_t1_same_candle_is_missed_move(self):
        cs = [C(1, 101, 107, 99.5, 104)]
        r = simulate_both(self.lim(), cs, H4)
        self.assertEqual((r["status"], r["reason"]), ("INVALID", "MISSED_MOVE"))
        self.assertIn("AMBIGUOUS", r["flags"])

    def test_G16_fill_and_stop_same_candle_is_loss(self):
        cs = [C(1, 101, 102, 95.5, 97)]
        r = simulate(self.lim(), cs, H4)
        self.assertEqual(r["exit_reason"], "STOP")
        self.assertLess(r["r_net"], -1)

    def test_G8_sweep_broken_cancels(self):
        cs = [C(1, 101, 102, 100.5, 96.0)]
        self.assertEqual(simulate(self.lim(), cs, H4)["reason"], "SWEEP_BROKEN")

    def test_G18_macro_blackout_with_fill(self):
        cs = [C(1, 101, 102, 99.5, 101)]
        r = simulate(self.lim(blackout=[(H4 + 1000, H4 + 2000)]), cs, H4)
        self.assertEqual(r["reason"], "MACRO_WINDOW")

    def test_t3_structural_target_rr_check(self):
        s = spec(targets=[103.0], target_fracs=[1.0], targets_fn=None, min_rr_net=1.5, breakeven_after_t1=False)
        r = simulate(s, [C(1, 100, 101, 99, 100)], H4)
        self.assertEqual(r["sub_reason"], "rr_too_low")


if __name__ == "__main__":
    unittest.main()
