"""v62 golden tests - backtest-discipline audit fixes."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import track_trades as tt
import auto_paper_trade as apt
import scalp_signals as ss
import backtest as bt
import archive_snapshot as arc
import trials_check as tc
import v2_track_trades as v2t

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)


class OkxBackfill(unittest.TestCase):
    def setUp(self):
        tt.time.sleep = lambda s: None

    def test_backfills_gap_after_outage(self):
        since = NOW - timedelta(hours=60)
        newest = [(MS(NOW - timedelta(minutes=5 * k)), 1.0, 1.0) for k in range(300)]  # only ~25h
        def page(sym, older_than):
            start = datetime.fromtimestamp(older_than / 1000, tz=timezone.utc)
            return [(MS(start - timedelta(minutes=5 * k)), 1.0, 0.5) for k in range(1, 101)]
        rows, covered = tt.backfill_okx_rows("ABC", MS(since), newest, fetch_page=page)
        self.assertTrue(covered)
        self.assertLessEqual(rows[0][0], MS(since) + tt.OKX_COVERAGE_TOLERANCE_MS)
        self.assertEqual(min(r[2] for r in rows), 0.5)  # the stop-touch inside the gap is now visible

    def test_reports_uncovered_when_history_unavailable(self):
        since = NOW - timedelta(hours=60)
        newest = [(MS(NOW - timedelta(minutes=5 * k)), 1.0, 1.0) for k in range(300)]
        rows, covered = tt.backfill_okx_rows("ABC", MS(since), newest, fetch_page=lambda s, t: None)
        self.assertFalse(covered)

    def test_no_backfill_needed(self):
        since = NOW - timedelta(hours=2)
        rows = [(MS(NOW - timedelta(minutes=5 * k)), 1.0, 1.0) for k in range(30)]
        called = []
        _, covered = tt.backfill_okx_rows("ABC", MS(since), rows, fetch_page=lambda s, t: called.append(1))
        self.assertTrue(covered)
        self.assertEqual(called, [])


class Survivorship(unittest.TestCase):
    def test_trade_on_coin_missing_from_scan_is_still_checked(self):
        filled = (NOW - timedelta(hours=3)).isoformat()
        trade = {"asset_id": "deadcoin", "symbol": "DEAD", "status": "open", "entry": 1.0, "stop": 0.9,
                 "targets": [1.2], "filled_at": filled}
        cache = {"DEAD": [(MS(NOW - timedelta(hours=1)), 1.0, 0.5)]}
        tt.OKX_COVERAGE.clear()
        tt.process_trades([trade], lookup={}, price_history={}, okx_cache=cache)
        self.assertEqual(trade["status"], "stopped")
        self.assertTrue(trade["out_of_scan_universe"])


class RegimeAndFill(unittest.TestCase):
    COIN = {"id": "abc", "symbol": "ABC", "price_usd": 1.0}
    REG = {"risk_state": "risk_on", "volatility_state": "normal"}

    def test_auto_trade_carries_regime_and_observed_fill_time(self):
        obs = (NOW - timedelta(minutes=20)).isoformat()
        t = apt.build_trade(self.COIN, 1.0, 0.9, kind="auto", used_tf_stop=False, now=NOW,
                            market_regime=self.REG, price_observed_at=obs)
        self.assertEqual(t["market_regime_at_entry"], self.REG)
        self.assertEqual(t["filled_at"], obs)
        self.assertEqual(t["created_at"], NOW.isoformat())

    def test_future_or_missing_observation_falls_back_to_now(self):
        self.assertEqual(apt.resolve_fill_time(None, NOW), NOW.isoformat())
        self.assertEqual(apt.resolve_fill_time((NOW + timedelta(minutes=5)).isoformat(), NOW), NOW.isoformat())
        self.assertEqual(ss.resolve_fill_time("garbage", NOW), NOW.isoformat())

    def test_summary_split_by_regime(self):
        mk = lambda st, ex, reg: {"status": st, "entry": 1.0, "exit_price": ex, "date_closed": "2026-10-02",
                                  "market_regime_at_entry": reg}
        trades = [mk("closed_targets_complete", 1.2, self.REG), mk("stopped", 0.9, {"risk_state": "risk_off"}),
                  mk("stopped", 0.9, None)]
        s = tt.summarize(trades)
        self.assertEqual(set(s["by_regime"]), {"risk_on|normal", "risk_off", "untagged"})
        self.assertEqual(s["by_regime"]["risk_on|normal"]["wins"], 1)
        self.assertFalse(s["by_regime"]["risk_on|normal"]["meaningful"])
        v2 = v2t.compute_performance_summary(trades)
        self.assertIn("risk_off", v2["by_regime"])


class BacktestFixes(unittest.TestCase):
    def candles(self):
        c, p = [], 100.0
        for i in range(120):
            o = p
            p = p * (1.004 if i % 7 else 0.99)
            c.append([i, o, max(o, p) * 1.01, min(o, p) * 0.99, p])
        return c

    def test_no_overlapping_signal_windows_and_next_open_entry(self):
        c = self.candles()
        res = bt.simulate_coin("x", c)
        sig = sorted(r["candle_index"] for r in res if r["is_signal"])
        for a, b in zip(sig, sig[1:]):
            self.assertGreaterEqual(b - a, bt.FORWARD_WINDOW_CANDLES)
        for r in res:
            self.assertEqual(r["entry_close"], c[r["candle_index"] + 1][1])

    def test_costs_applied(self):
        c = [[i, 100, 100, 100, 100] for i in range(100)]
        res = bt.simulate_coin("flat", c)
        self.assertTrue(all(r["pct_change_after_window"] == -bt.ROUND_TRIP_COST_PCT for r in res))


class Archive(unittest.TestCase):
    def test_archive_gate_slim_and_stale_skip(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            flags = td / "flags.json"
            flags.write_text(json.dumps({"updated_at": "2026-10-01T11:50:00+00:00", "market_regime": {"risk_state": "neutral"},
                                         "coins": [{"id": "a", "real_candles": [[1, 2]], "confidence_score": 50}]}))
            kw = dict(flags_path=flags, archive_dir=td / "archive", state_path=td / "state.json")
            p = arc.run(now=NOW, **kw)
            snap = json.loads(p.read_text())
            self.assertNotIn("real_candles", snap["coins"][0])
            self.assertEqual(snap["coins"][0]["confidence_score"], 50)
            self.assertIsNone(arc.run(now=NOW + timedelta(minutes=30), **kw))       # gate
            self.assertIsNone(arc.run(now=NOW + timedelta(hours=3), **kw))          # same scan = stale
            flags.write_text(json.dumps({"updated_at": "2026-10-01T14:50:00+00:00", "coins": [{"id": "a"}]}))
            self.assertIsNotNone(arc.run(now=NOW + timedelta(hours=3), **kw))

    def test_prune_old_days(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td)
            (a / "2026-01-01").mkdir()
            (a / (NOW.strftime("%Y-%m-%d"))).mkdir()
            self.assertEqual(arc.prune(NOW, a), 1)


class Trials(unittest.TestCase):
    def test_detects_untracked_change(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "config").mkdir(); (r / "data").mkdir(); (r / "scripts").mkdir()
            (r / "scripts" / "x.py").write_text("THRESH = 45  # tweaked\nLIST = [1.5, 2.5]\n")
            (r / "config" / "trials-log.json").write_text(json.dumps({
                "tracked_parameters": [{"name": "THRESH", "file": "scripts/x.py", "current_value": 40},
                                       {"name": "LIST", "file": "scripts/x.py", "current_value": [1.5, 2.5]}],
                "trials": [{"id": "T1", "parameter": "THRESH", "n": 7}]}))
            rep = tc.run(r)
            self.assertEqual(rep["status"], "WARN")
            self.assertEqual([u["name"] for u in rep["untracked_parameter_changes"]], ["THRESH"])
            self.assertEqual(rep["unvalidated_below_min_sample"][0]["id"], "T1")

    def test_real_repo_is_consistent(self):
        rep = tc.run(Path(__file__).resolve().parent.parent)
        self.assertEqual(rep["untracked_parameter_changes"], [])
        self.assertEqual(rep["tracking_problems"], [])


if __name__ == "__main__":
    unittest.main()
