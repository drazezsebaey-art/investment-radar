"""
v68 golden tests: budget projection floor, yield decomposition, COT key fix,
BTC.D alt-risk, weighted escalation, peg-behaviour exclusion.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import asset_filters as af  # noqa: E402
import cg_budget  # noqa: E402
import macro_gold as mg  # noqa: E402
import altseason_regime as alt  # noqa: E402
import breakout_check as bc  # noqa: E402


class TestBudgetFloor(unittest.TestCase):
    def _status(self, used, now):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "u.json"
            p.write_text(json.dumps({"month": now.strftime("%Y-%m"), "total": used}))
            return cg_budget.status(now, p)

    def test_day_one_no_false_throttle(self):
        s = self._status(187, datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(s["throttle_level"], 0)          # was level 2 (116%) before v68

    def test_real_overspend_still_throttles(self):
        s = self._status(6000, datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(s["throttle_level"], 2)          # >= 50% used -> no floor

    def test_mid_month_unchanged(self):
        s = self._status(5000, datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(s["throttle_level"], 2)          # 5000/14d*31 = 11071 > 10000


class TestDecomposition(unittest.TestCase):
    def test_real_driven(self):
        m = {"nominal_yield_10y_pct": {"chg_1m": 51.0, "chg_1w": 30.0},
             "real_yield_10y_pct": {"chg_1m": 47.0, "chg_1w": 28.0},
             "breakeven_10y_pct": {"chg_1m": 1.0, "chg_1w": 1.0}}
        d = mg.yield_decomposition(m)
        self.assertEqual(d["chg_1m"]["driver"], "REAL_YIELD_DRIVEN")
        self.assertEqual(d["chg_1m"]["direction"], "UP")

    def test_inflation_driven_and_small(self):
        m = {"nominal_yield_10y_pct": {"chg_1m": 20.0, "chg_1w": 4.0},
             "real_yield_10y_pct": {"chg_1m": 2.0, "chg_1w": 2.0},
             "breakeven_10y_pct": {"chg_1m": 18.0, "chg_1w": 2.0}}
        d = mg.yield_decomposition(m)
        self.assertEqual(d["chg_1m"]["driver"], "INFLATION_EXPECTATIONS_DRIVEN")
        self.assertEqual(d["chg_1w"]["driver"], "SMALL_MOVE")

    def test_missing(self):
        self.assertEqual(mg.yield_decomposition({})["chg_1m"]["driver"], "insufficient_data")


class TestAltRisk(unittest.TestCase):
    def _hist(self, values, start=datetime(2026, 9, 25, tzinfo=timezone.utc)):
        return [{"ts": (start + timedelta(hours=4 * i)).isoformat(), "btc_dominance_pct": v} for i, v in enumerate(values)]

    def test_needs_history(self):
        self.assertEqual(alt.btc_dominance_alt_risk(self._hist([56.0] * 10))["level"], "UNKNOWN")

    def test_flat_is_normal(self):
        self.assertEqual(alt.btc_dominance_alt_risk(self._hist([56.0 + (i % 2) * 0.05 for i in range(30)]))["level"], "NORMAL")

    def test_breakout_holding_and_rising(self):
        vals = [55.5] * 24 + [55.9, 56.1, 56.3, 56.2, 56.25, 56.3]
        r = alt.btc_dominance_alt_risk(self._hist(vals))
        self.assertIn("BTC_DOM_BREAKOUT_HOLDING", r["flags"])
        self.assertIn("BTC_DOM_RISING_3D", r["flags"])
        self.assertEqual(r["level"], "HIGH")


class TestEscalation(unittest.TestCase):
    def test_cluster_lag_alone_does_not_escalate(self):
        self.assertLess(bc.escalation_strength({"cluster_rotation_lag": "layer-1", "structure": {"signal": None}}),
                        bc.ESCALATION_MIN_STRENGTH)

    def test_choch_ranks_above_squeeze(self):
        a = bc.escalation_strength({"structure": {"signal": "CHoCH_bullish"}})
        b = bc.escalation_strength({"volatility_squeeze": True})
        self.assertGreater(a, b)

    def test_selection_orders_by_strength(self):
        coins = [{"id": f"c{i}", "priority_review": True, "early_signals": {"volatility_squeeze": True}} for i in range(30)]
        coins.append({"id": "zz-strong", "priority_review": True,
                      "early_signals": {"structure": {"signal": "CHoCH_bullish"}, "relative_strength_consolidation": True}})
        coins.append({"id": "zz-lag", "priority_review": True, "early_signals": {"cluster_rotation_lag": "rwa"}})
        bc.load_rotation_offset = lambda: 0
        bc.save_rotation_offset = lambda o: None
        old = bc.MAX_CANDIDATES_PER_RUN
        bc.MAX_CANDIDATES_PER_RUN = 0
        try:
            picked = [c["id"] for c in bc.select_rotating_candidates(coins)]
        finally:
            bc.MAX_CANDIDATES_PER_RUN = old
        self.assertEqual(picked[0], "zz-strong")
        self.assertNotIn("zz-lag", picked)


class TestPegBehaviour(unittest.TestCase):
    def test_new_dollar_products(self):
        self.assertEqual(af.exclusion_reason_for({"id": "usa", "symbol": "usat"}), "PEGGED_ASSET")
        self.assertEqual(af.exclusion_reason_for({"id": "cash-4", "symbol": "cash", "price_usd": 0.9994,
                                                  "change_7d_pct": -0.004, "change_24h_pct": 0.01}), "PEGGED_BEHAVIOUR")

    def test_volatile_token_near_one_dollar_kept(self):
        self.assertIsNone(af.exclusion_reason_for({"id": "btse-token", "symbol": "btse", "price_usd": 1.019,
                                                   "change_7d_pct": 0.72}))
        self.assertIsNone(af.exclusion_reason_for({"id": "starknet", "symbol": "strk", "current_price": 0.043,
                                                   "price_change_percentage_7d_in_currency": 0.1}))


class TestCotKey(unittest.TestCase):
    def test_reads_saved_key(self):
        src = (Path(__file__).resolve().parent.parent / "scripts" / "macro_gold.py").read_text(encoding="utf-8")
        self.assertIn('prev.get("cot_gold_managed_money")', src)


if __name__ == "__main__":
    unittest.main()
