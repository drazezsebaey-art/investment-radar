"""v65 golden tests - correction monitor (OKX mocked)."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import correction_monitor as cm

NOW = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
DAY, H4 = 86400_000, 4 * 3600_000
NOW_MS = int(NOW.timestamp() * 1000)


def row(ts, o, h, l, c, vq=1000.0, closed=True):
    return [str(ts), str(o), str(h), str(l), str(c), "0", "0", str(vq), "1" if closed else "0"]


def daily_path(prices, vols=None):
    """prices oldest-first closes; returns OKX rows newest-first."""
    rows = []
    n = len(prices)
    for i, p in enumerate(prices):
        ts = NOW_MS - (n - 1 - i) * DAY
        rows.append(row(ts, p, p * 1.01, p * 0.99, p, (vols or [1000] * n)[i]))
    return list(reversed(rows))


class Btc(unittest.TestCase):
    def weekly(self, last_close, base=70000):
        rows = [row(NOW_MS - (60 - i) * 7 * DAY, base, base, base, base) for i in range(59)]
        rows.append(row(NOW_MS - 7 * DAY, last_close, last_close, last_close, last_close))
        rows.append(row(NOW_MS, 99999, 99999, 1, 99999, closed=False))   # open candle ignored
        return cm.parse_candles(rows)

    def test_scenarios(self):
        d = cm.parse_candles(daily_path([83500] * 40))
        self.assertEqual(cm.btc_scenario(self.weekly(83500), d)["scenario"], "A")
        self.assertEqual(cm.btc_scenario(self.weekly(81000), d)["scenario"], "B")
        low = cm.parse_candles(daily_path([71000] * 40))
        self.assertEqual(cm.btc_scenario(self.weekly(81000), low)["scenario"], "C")   # near 50W SMA

    def test_insufficient(self):
        self.assertIsNone(cm.btc_scenario([], [])["scenario"])


class Impulse(unittest.TestCase):
    def test_find_and_skip(self):
        up = [2.0] * 10 + [2.0 + 0.3 * i for i in range(11)] + [5.0 - 0.1 * i for i in range(10)]
        imp = cm.find_impulse(cm.parse_candles(daily_path(up)), NOW_MS)
        self.assertGreater(imp["gain_pct"], 100)
        flat = cm.parse_candles(daily_path([2.0 + 0.001 * i for i in range(40)]))
        self.assertIsNone(cm.find_impulse(flat, NOW_MS))


class Structure(unittest.TestCase):
    def c4(self, closes, start_ms):
        return [{"ts": start_ms + i * H4, "o": c, "h": c * 1.002, "l": c * 0.998, "c": c, "vq": 1, "closed": True}
                for i, c in enumerate(closes)]

    def test_choch_up_detected(self):
        # peak 10 -> down to 8 with a lower-high bounce at 9 -> low 7 -> rally closes above 9
        path = [10, 9.6, 9.2, 8.8, 8.4, 8.6, 9.0, 8.6, 8.2, 7.8, 7.4, 7.0, 7.3, 7.8, 8.4, 9.2, 9.5]
        r = cm.choch_up(self.c4(path, 0), 0)
        self.assertTrue(r["choch_up"])
        self.assertAlmostEqual(r["last_lower_high"], 9.0 * 1.002, places=4)

    def test_no_choch_while_below_lower_high(self):
        path = [10, 9.6, 9.2, 8.8, 8.4, 8.6, 9.0, 8.6, 8.2, 7.8, 7.4, 7.0, 7.3, 7.8, 8.2, 8.0, 7.9]
        self.assertFalse(cm.choch_up(self.c4(path, 0), 0)["choch_up"])


class Classify(unittest.TestCase):
    base = {"price": 5.0, "impulse_low": 2.0}

    def test_broken(self):
        self.assertEqual(cm.classify({**self.base, "retracement_max": 0.85}), "BROKEN")
        self.assertEqual(cm.classify({"price": 1.9, "impulse_low": 2.0, "retracement_max": 0.5}), "BROKEN")

    def test_resuming(self):
        self.assertEqual(cm.classify({**self.base, "retracement_max": 0.5, "choch_up": True}), "RESUMING")

    def test_reset_done_needs_three_incl_depth(self):
        m = {**self.base, "retracement_max": 0.5, "oi_drawdown_pct": 30, "funding_now_pct": -0.001, "volume_ratio": 1.2}
        self.assertEqual(cm.classify(m), "RESET_DONE")
        m2 = {**self.base, "retracement_max": 0.2, "oi_drawdown_pct": 30, "funding_now_pct": -0.001, "volume_ratio": 0.5}
        self.assertEqual(cm.classify(m2), "ONGOING")      # too shallow = not a real reset yet

    def test_oi_drawdown(self):
        self.assertEqual(cm.oi_drawdown([["3", "70"], ["2", "100"], ["1", "80"]]), 30.0)


class RunEndToEnd(unittest.TestCase):
    def test_run(self):
        up = [2.0] * 10 + [2.0 + 0.3 * i for i in range(11)] + [5.0 - 0.12 * i for i in range(12)]
        imp_vol = [1000] * 21 + [400] * 12

        def get(path, params):
            inst = params.get("instId", "")
            if path == "/market/candles" and inst == "BTC-USDT":
                if params["bar"] == "1W":
                    rows = [row(NOW_MS - (60 - i) * 7 * DAY, 70000, 70000, 70000, 70000) for i in range(59)]
                    rows.append(row(NOW_MS - 7 * DAY, 83500, 84000, 83000, 83500))
                    return list(reversed(rows))
                return daily_path([83500] * 40)
            if path == "/market/candles" and params["bar"] == "1D":
                return daily_path(up, imp_vol)
            if path == "/market/candles":
                return []
            if path.startswith("/rubik"):
                return [["2", "70"], ["1", "100"]]
            if path == "/public/funding-rate":
                return [{"fundingRate": "-0.00002"}]
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cm.OUT, cm.FLAGS, cm.DERIV = td / "o.json", td / "f.json", td / "d.json"
            cm.WATCHLIST, cm.ETF_WATCH = td / "w.json", td / "e.json"
            cm.WATCHLIST.write_text(json.dumps({"always_include": ["near", "bitcoin"]}))
            out = cm.run(now=NOW, get=get)
            self.assertEqual(out["btc_q4_scenario"]["scenario"], "A")
            self.assertEqual(len(out["coins_in_correction"]), 1)
            r = out["coins_in_correction"][0]
            self.assertEqual(r["coin"], "near")
            self.assertEqual(r["oi_drawdown_pct"], 30.0)
            self.assertLess(r["volume_ratio"], 0.7)
            self.assertEqual(r["status"], "RESET_DONE")
            self.assertIsNone(cm.run(now=NOW + timedelta(minutes=30), get=get))


if __name__ == "__main__":
    unittest.main()
