"""
Golden test cases for scripts/track_trades.py's Cost Model (fee + slippage)
------------------------------------------------------------
Locks in the exact regression the 24/9/2026 audit report warned about: a
trade that "won" by status can still be a net loss once realistic costs
are applied - if this test ever breaks, the cost model has been
accidentally removed or zeroed out.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import track_trades as tt  # noqa: E402


class TestCostModel(unittest.TestCase):
    def test_round_trip_cost_is_nonzero(self):
        self.assertGreater(tt.ROUND_TRIP_COST_PCT, 0)

    def test_net_return_is_always_less_than_gross(self):
        trade = {"entry": 100, "actual_entry": 100, "exit_price": 106}
        gross = tt.pct_return(trade)
        net = tt.net_pct_return(trade)
        self.assertLess(net, gross)
        self.assertAlmostEqual(gross - net, tt.ROUND_TRIP_COST_PCT, places=2)

    def test_tiny_nominal_win_becomes_a_net_loss(self):
        """The exact scenario the audit report warned about: a trade closed
        as a 'win' by status, with a gross return smaller than the round-
        trip cost, must show a NEGATIVE net return."""
        trade = {"status": "closed_targets_complete", "entry": 100, "actual_entry": 100, "exit_price": 100.1}
        net = tt.net_pct_return(trade)
        self.assertLess(net, 0)

    def test_stats_for_reports_both_gross_and_net(self):
        trades = [
            {"status": "closed_targets_complete", "entry": 100, "actual_entry": 100,
             "exit_price": 106, "date_closed": "2026-09-20"},
            {"status": "stopped", "entry": 100, "actual_entry": 100,
             "exit_price": 97, "date_closed": "2026-09-21"},
        ]
        stats = tt.stats_for(trades)
        self.assertIn("avg_return_pct", stats)
        self.assertIn("avg_return_pct_gross", stats)
        self.assertIn("estimated_round_trip_cost_pct", stats)
        self.assertLess(stats["avg_return_pct"], stats["avg_return_pct_gross"])

    def test_stats_for_handles_no_trades_without_crashing(self):
        stats = tt.stats_for([])
        self.assertEqual(stats["n_closed"], 0)
        self.assertIsNone(stats["avg_return_pct"])


if __name__ == "__main__":
    unittest.main()
