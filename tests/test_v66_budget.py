"""v66 golden tests - CoinGecko ledger/throttle, OKX-first data shapes, digest."""
import json, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cg_budget
import breakout_check as bc
import digest


class Budget(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.usage = Path(self.td.name) / "u.json"
        self.cfg = Path(self.td.name) / "c.json"
        self.cfg.write_text(json.dumps({"monthly_limit": 10000}))
        cg_budget.CONFIG = self.cfg
        cg_budget._pending.clear()

    def tearDown(self):
        self.td.cleanup()

    def test_record_flush_and_levels(self):
        now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)      # ~9.5 days elapsed
        cg_budget.record("scan", 2000)
        cg_budget.flush(now, self.usage)
        s = cg_budget.status(now, self.usage)
        self.assertEqual(s["used"], 2000)
        self.assertEqual(s["throttle_level"], 0)                   # ~6.5k projected < 90%
        self.assertAlmostEqual(s["projection"], round(2000 / 9.5 * 31), delta=2)

    def test_level_thresholds(self):
        now = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)       # 15 days elapsed
        cg_budget.record("scan", 4000); cg_budget.flush(now, self.usage)
        self.assertEqual(cg_budget.throttle_level(now, self.usage), 0)      # ~8.3k projected
        cg_budget.record("breakout_check", 800); cg_budget.flush(now, self.usage)
        self.assertEqual(cg_budget.throttle_level(now, self.usage), 1)      # ~9.9k > 90%
        cg_budget.record("breakout_check", 1000); cg_budget.flush(now, self.usage)
        self.assertEqual(cg_budget.throttle_level(now, self.usage), 2)      # > 10k

    def test_month_rollover_resets(self):
        cg_budget.record("scan", 9999); cg_budget.flush(datetime(2026, 9, 30, tzinfo=timezone.utc), self.usage)
        s = cg_budget.status(datetime(2026, 10, 1, 1, tzinfo=timezone.utc), self.usage)
        self.assertEqual(s["used"], 0)
        cg_budget.record("scan", 1); cg_budget.flush(datetime(2026, 10, 1, 1, tzinfo=timezone.utc), self.usage)
        u = json.loads(self.usage.read_text())
        self.assertEqual(u["history"][-1], {"month": "2026-09", "total": 9999})


class OkxShapes(unittest.TestCase):
    def test_ohlc_close_stamped_oldest_first(self):
        rows = [[str(1_000_000 + i * 14_400_000), "1", "2", "0.5", "1.5", "10", "10", "15", "1"] for i in range(3)][::-1]
        orig = bc._okx_rows
        bc._okx_rows = lambda inst, bar, limit, after=None: rows
        try:
            out = bc.okx_ohlc_4h("abc")
        finally:
            bc._okx_rows = orig
        self.assertEqual(out[0][0], 1_000_000 + 14_400_000)
        self.assertLess(out[0][0], out[-1][0])
        self.assertEqual(out[0][1:], [1.0, 2.0, 0.5, 1.5])

    def test_rolling_24h_volume(self):
        page = [[str(i * 3_600_000), "1", "1", "1", "1", "1", "1", "10", "1"] for i in range(50)][::-1]
        orig = bc._okx_rows
        bc._okx_rows = lambda inst, bar, limit, after=None: page if after is None else []
        try:
            out = bc.okx_rolling_24h_volumes("abc", hours=10)
        finally:
            bc._okx_rows = orig
        self.assertEqual(len(out), 10)
        self.assertTrue(all(v == 240 for _, v in out))              # 24 bars x 10

    def test_fetch_ohlc_falls_back_to_coingecko(self):
        calls = []
        o1, o2 = bc.okx_ohlc_4h, bc.fetch_json
        bc.okx_ohlc_4h = lambda s: (_ for _ in ()).throw(RuntimeError("no market"))
        bc.fetch_json = lambda url: calls.append(url) or [[1, 1, 1, 1, 1]]
        try:
            bc.fetch_ohlc("x", "XYZ")
        finally:
            bc.okx_ohlc_4h, bc.fetch_json = o1, o2
        self.assertIn("/ohlc", calls[0])


class Digest(unittest.TestCase):
    def test_digest_small_and_honest_about_missing(self):
        with tempfile.TemporaryDirectory() as td:
            digest.D = Path(td)
            text = digest.build(datetime(2026, 10, 2, tzinfo=timezone.utc))
            self.assertIn("missing inputs", text)
            self.assertLess(len(text.encode()), 4000)
            (Path(td) / "correction-monitor.json").write_text(json.dumps({
                "updated_at": "2026-10-02T00:00:00+00:00",
                "btc_q4_scenario": {"scenario": "A", "price": 84000.0, "last_weekly_close": 83500.0,
                                    "key_level": 82800.0, "sma_50w": 70000.0, "dist_to_50w_pct": 20.0},
                "coins_in_correction": [{"symbol": "JUP", "status": "RESET_DONE", "entry_ready": True,
                                         "impulse_gain_pct": 77.0, "retracement_now": 0.5, "levels": {}}]}))
            text = digest.build(datetime(2026, 10, 2, tzinfo=timezone.utc))
            self.assertIn("BTC scenario **A**", text)
            self.assertIn("JUP: **RESET_DONE** ENTRY_READY", text)


if __name__ == "__main__":
    unittest.main()
