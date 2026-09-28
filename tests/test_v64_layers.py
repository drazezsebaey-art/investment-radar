"""v64 golden tests - altseason, fundamentals, macro/gold, pre-pump (all network mocked)."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import altseason_regime as al
import fundamentals_revenue as fr
import macro_gold as mg
import prepump_watchlist as pp

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class Altseason(unittest.TestCase):
    def test_ethbtc(self):
        rows = [(i, 0.035 if i == 0 else 0.030) for i in range(40)]
        m = al.ethbtc_metrics(rows)
        self.assertTrue(m["eth_btc_above_trigger"])
        self.assertAlmostEqual(m["eth_btc_7d_pct"], 16.67, places=1)

    def test_breadth(self):
        flags = {"coins": [{"id": "bitcoin", "change_7d_pct": 5}] +
                 [{"id": f"a{i}", "change_7d_pct": 10 if i < 8 else 1} for i in range(10)]}
        self.assertEqual(al.breadth_proxy(flags)["breadth_7d_pct"], 80.0)

    def test_stablecoins_and_flows(self):
        day = 86400
        total = [{"date": str(1_000_000 + k * day), "totalCirculatingUSD": {"peggedUSD": 100 + k}} for k in range(40)]
        m = al.stable_metrics(total, [{"name": "Solana", "totalCirculatingUSD": {"peggedUSD": 120_000_000}}])
        self.assertAlmostEqual(m["stablecoin_7d_pct"], (139 / 132 - 1) * 100, places=1)
        flows = al.chain_flows(m["_by_chain"], {"Solana": 100_000_000})
        self.assertEqual(flows[0]["change_pct"], 20.0)

    def test_regime_needs_trend(self):
        hist = [{"btc_dominance_pct": 59.5 - i * 0.2} for i in range(6)]
        label, sig = al.regime({"btc_dominance_pct": 58.4, "eth_btc_above_trigger": True}, hist)
        self.assertIn("BTC_DOM_FALLING_BELOW_60", sig)
        self.assertEqual(label, "ROTATION_BUILDING")
        label2, _ = al.regime({"btc_dominance_pct": 58.4}, [{"btc_dominance_pct": 58.0}] * 6)
        self.assertEqual(label2, "BTC_LED")

    def test_run_isolated_errors(self):
        with tempfile.TemporaryDirectory() as td:
            al.OUT, al.FLAGS = Path(td) / "a.json", Path(td) / "f.json"
            def boom(): raise OSError("x")
            out = al.run(now=NOW, fetchers={"global": lambda: {"btc_dominance_pct": 58.5}, "ethbtc": boom,
                                            "stable": lambda: {"_by_chain": {}}})
            self.assertEqual(len(out["errors"]), 1)
            self.assertIsNone(al.run(now=NOW + timedelta(minutes=30), fetchers={}))


class Fundamentals(unittest.TestCase):
    REV = [{"name": "pump.fun", "displayName": "pump.fun", "slug": "pump.fun", "category": "Launchpad",
            "total24h": 1.9e6, "total7d": 11e6, "total30d": 40e6, "change_30dover30d": 45, "change_7dover7d": 12},
           {"name": "tiny", "slug": "tiny", "total30d": 1000}]
    HOLD = [{"name": "pump.fun", "displayName": "pump.fun", "total30d": 20e6}]
    INDEX = [{"slug": "pump.fun", "name": "pump.fun", "gecko_id": "pump-fun"}]

    def test_rows_and_flags(self):
        rows = fr.protocol_rows(self.REV, self.HOLD, fr.build_gecko_map(self.INDEX),
                                {"pump-fun": {"market_cap_usd": 1.25e9}}, {})
        r = rows["pump-fun"]
        self.assertEqual(r["revenue_annualised_usd"], round(40e6 * 365 / 30))
        self.assertAlmostEqual(r["holder_yield_pct"], 19.47, places=1)
        self.assertIn("REVENUE_ACCELERATING", r["flags"])
        self.assertIn("HIGH_HOLDER_YIELD", r["flags"])
        self.assertIn("CHEAP_VS_REVENUE", r["flags"])
        self.assertNotIn("protocol:tiny", rows)

    def test_governance(self):
        props = [{"id": "p1", "title": "Activate fee switch and buyback", "end": 1790000000,
                  "space": {"id": "uniswapgovernance.eth", "name": "Uniswap"}},
                 {"id": "p2", "title": "Grant for hackathon", "end": 1790000000, "space": {"id": "x", "name": "X"}}]
        g = fr.governance_catalysts(props, {"uniswap": "uniswap"})
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["coin"], "uniswap")


class MacroGold(unittest.TestCase):
    def test_fred_csv(self):
        rows = mg.parse_fred_csv("observation_date,DFII10\n2026-09-24,2.10\n2026-09-25,.\n2026-09-26,2.25\n")
        self.assertEqual(rows[0], ("2026-09-26", 2.25))
        self.assertEqual(len(rows), 2)

    def test_series_bp(self):
        rows = [(f"d{i}", 2.30 - i * 0.01) for i in range(30)]
        m = mg.series_metrics(rows, bp=True)
        self.assertEqual(m["chg_1m"], 20.0)

    def test_cot(self):
        rows = [{"report_date_as_yyyy_mm_dd": f"2026-09-{26 - i:02d}T00:00:00", "m_money_positions_long_all": str(200 - i),
                 "m_money_positions_short_all": "50", "open_interest_all": "500"} for i in range(20)]
        m = mg.cot_metrics(rows)
        self.assertEqual(m["mm_net_contracts"], 150)
        self.assertEqual(m["mm_net_percentile_3y"], 100.0)
        self.assertIn("MM_CROWDED_LONG", m["flags"])

    def test_pillar6_deviation(self):
        p = mg.pillar6({"chg_1m": 5.0}, {"chg_1m": 25.0}, {"chg_1m": 0.2})
        self.assertEqual(p["status"], "DEVIATING")
        self.assertIn("GOLD_UP_DESPITE_REAL_YIELD_UP", p["flags"])
        self.assertEqual(mg.pillar6({"chg_1m": -3}, {"chg_1m": 20}, {})["status"], "CONSISTENT_OR_NEUTRAL")

    def test_run_cot_cached(self):
        with tempfile.TemporaryDirectory() as td:
            mg.OUT = Path(td) / "m.json"
            calls = []
            fetch = {"fred": lambda s: [(f"d{i}", 2.0) for i in range(30)],
                     "gold": lambda: [(f"d{i}", 4300.0) for i in range(30)],
                     "cot": lambda: calls.append(1) or []}
            mg.run(now=NOW, fetch=fetch)
            mg.run(now=NOW + timedelta(hours=7), fetch=fetch)
            self.assertEqual(len(calls), 1)            # COT refreshed once per 24h


class PrePump(unittest.TestCase):
    def setUp(self):
        self.flags = {"coins": [
            {"id": "qnt", "symbol": "QNT", "price_usd": 100, "change_7d_pct": 5, "market_cap_usd": 1.4e9,
             "flags": ["قوة نسبية خفية أثناء التماسك — ..."]},
            {"id": "leader", "symbol": "LDR", "price_usd": 1, "change_7d_pct": 80, "market_cap_usd": 2e9, "flags": []},
            {"id": "hot", "symbol": "HOT", "price_usd": 1, "change_7d_pct": 45, "market_cap_usd": 2e9,
             "flags": ["قوة نسبية خفية"]}]}
        self.etf = {"items": [{"coin": "qnt", "event": "filing", "title": "x", "detected_at": NOW.isoformat()},
                              {"coin": "hot", "event": "filing", "title": "y", "detected_at": NOW.isoformat()}]}
        self.cats = {"categories": {"interop": ["qnt", "leader"]}}

    def test_candidates_need_two_categories_and_not_pumped(self):
        coins, sig = pp.collect_signals(self.flags, self.etf, {}, {}, self.cats, NOW)
        self.assertEqual(sorted(sig["qnt"]), ["A", "C", "E"])
        c = pp.candidates(coins, sig)
        self.assertEqual([x["coin"] for x in c], ["qnt"])   # 'hot' excluded: already +45% 7d

    def test_forward_test(self):
        coins, sig = pp.collect_signals(self.flags, self.etf, {}, {}, self.cats, NOW)
        log = pp.update_log({"entries": []}, pp.candidates(coins, sig), coins, NOW, lambda s: None)
        self.assertEqual(len(log["entries"]), 1)
        coins["qnt"]["price_usd"] = 150
        log = pp.update_log(log, [], coins, NOW + timedelta(days=3), lambda s: None)
        coins["qnt"]["price_usd"] = 120
        log = pp.update_log(log, [], coins, NOW + timedelta(days=7, hours=1), lambda s: None)
        cp = log["entries"][0]["checkpoints"]["d7"]
        self.assertEqual(cp["return_pct"], 20.0)
        self.assertEqual(cp["max_gain_pct"], 50.0)
        st = pp.stats(log)
        self.assertEqual(st["d7"]["ALL"]["n"], 1)
        self.assertEqual(st["d7"]["cat_A"]["hit_rate_pct"], 100.0)
        self.assertFalse(st["d7"]["ALL"]["meaningful"])
        log = pp.update_log(log, pp.candidates(coins, sig), coins, NOW + timedelta(days=8), lambda s: None)
        self.assertEqual(len(log["entries"]), 1)         # not re-logged inside 14 days

    def test_out_of_universe_uses_fallback_price(self):
        log = {"entries": [{"coin": "gone", "symbol": "GONE", "first_seen": NOW.isoformat(), "price_at_signal": 1.0,
                            "categories": ["A", "B"], "max_price_seen": 1.0, "checkpoints": {}}]}
        log = pp.update_log(log, [], {}, NOW + timedelta(days=7), lambda s: 0.5)
        self.assertEqual(log["entries"][0]["checkpoints"]["d7"]["return_pct"], -50.0)
        self.assertTrue(log["entries"][0]["out_of_universe"])


if __name__ == "__main__":
    unittest.main()
