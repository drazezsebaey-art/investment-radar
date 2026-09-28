"""v63 golden tests - derivatives snapshot (OKX mocked, no network)."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import derivatives_snapshot as ds

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)
H = lambda k: str(MS(NOW - timedelta(hours=k)))


def fake_okx(sym_has_perp=True, funding_now="0.0003", oi_now=1_600_000_000, oi_24h=1_500_000_000):
    def get(path, params):
        if path == "/public/instruments":
            return [{"instId": params["instId"], "ctVal": "10"}] if sym_has_perp else []
        if path == "/public/funding-rate":
            return [{"fundingRate": funding_now}]
        if path == "/public/funding-rate-history":
            return [{"fundingTime": H(8 * k), "realizedRate": str(0.0001 if k % 5 else -0.0001)} for k in range(90)]
        if path == "/rubik/stat/contracts/open-interest-volume":
            return [[H(k), str(oi_now if k == 0 else (oi_24h if k >= 24 else oi_now)), "100000000"] for k in range(30)]
        if path == "/rubik/stat/contracts/long-short-account-ratio":
            return [[H(0), "1.55"], [H(1), "1.40"]]
        if path == "/rubik/stat/contracts/long-short-account-ratio-contract-top-trader":
            return [[H(0), "1.63"]]
        if path == "/rubik/stat/contracts/long-short-position-ratio-contract-top-trader":
            return [[H(0), "3.11"]]
        if path == "/rubik/stat/taker-volume":
            return [[H(k), "120", "100"] for k in range(30)]
        if path == "/public/liquidation-orders":
            return [{"details": [
                {"ts": H(1), "posSide": "short", "side": "buy", "sz": "1000", "bkPx": "5.2"},
                {"ts": H(2), "posSide": "short", "side": "buy", "sz": "500", "bkPx": "5.0"},
                {"ts": H(3), "posSide": "long", "side": "sell", "sz": "100", "bkPx": "5.1"},
                {"ts": H(30), "posSide": "long", "side": "sell", "sz": "9999", "bkPx": "4.0"}]}]
        raise AssertionError(path)
    return get


COIN = {"id": "near", "symbol": "NEAR", "reasons": ["watchlist"], "market_cap_usd": 6_910_000_000, "change_24h_pct": 7.6}


class Snapshot(unittest.TestCase):
    def test_full_coin(self):
        m = ds.snapshot_coin(COIN, get=fake_okx(), now=NOW)
        self.assertEqual(m["status"], "ok")
        self.assertAlmostEqual(m["oi_to_mcap_pct"], 23.15, places=1)
        self.assertAlmostEqual(m["oi_change_24h_pct"], 6.67, places=1)
        self.assertEqual(m["top_trader_position_ratio"], 3.11)
        self.assertEqual(m["ls_accounts_ratio"], 1.55)
        self.assertEqual(m["taker_buy_sell_24h"], round(100 / 120, 3))
        self.assertEqual(m["liq_short_24h_usd"], 1000 * 10 * 5.2 + 500 * 10 * 5.0)
        self.assertEqual(m["liq_long_24h_usd"], 100 * 10 * 5.1)   # the 30h-old one excluded
        self.assertTrue(m["liq_window_complete"])
        self.assertEqual(m["funding_percentile_30d"], 100.0)
        for flag in ("LEVERAGE_EXTREME", "CROWDED_LONG", "AGGRESSIVE_SELLING", "SHORT_SQUEEZE_24H"):
            self.assertIn(flag, m["flags"])

    def test_no_perp(self):
        m = ds.snapshot_coin(COIN, get=fake_okx(sym_has_perp=False), now=NOW)
        self.assertEqual(m["status"], "no_perp")

    def test_endpoint_failure_is_partial_not_fatal(self):
        base = fake_okx()
        def get(path, params):
            if path.startswith("/rubik"):
                raise RuntimeError("OKX code=51001 not supported")
            return base(path, params)
        m = ds.snapshot_coin(COIN, get=get, now=NOW)
        self.assertEqual(m["status"], "partial")
        self.assertIsNone(m["top_trader_position_ratio"])
        self.assertIsNotNone(m["funding_now_pct"])

    def test_negative_funding_with_rising_oi_is_squeeze_fuel(self):
        m = ds.snapshot_coin(COIN, get=fake_okx(funding_now="-0.0002", oi_now=2_000_000_000), now=NOW)
        self.assertIn("SHORT_SQUEEZE_FUEL", m["flags"])

    def test_net_mode_liquidation_side(self):
        r = ds.liquidation_metrics([{"details": [{"ts": H(1), "posSide": "net", "side": "sell", "sz": "1", "bkPx": "10"}]}], 1, NOW)
        self.assertEqual(r["liq_long_24h_usd"], 10)


class Selection(unittest.TestCase):
    def test_selection_order_cap_and_symbols(self):
        flags = {"coins": [{"id": f"c{i}", "symbol": f"C{i}", "confidence_score": i} for i in range(40)]}
        sel = ds.select_coins(flags, {"always_include": ["bitcoin", "near"]}, {"coins": {"cardano": {}}},
                              [[{"asset_id": "tellor", "status": "open"}, {"asset_id": "x", "status": "closed"}]])
        ids = [c["id"] for c in sel]
        self.assertEqual(ids[:4], ["bitcoin", "near", "cardano", "tellor"])
        self.assertNotIn("x", ids)
        self.assertIn("c39", ids)
        self.assertLessEqual(len(sel), ds.MAX_COINS)
        self.assertEqual(sel[3]["symbol"], "TRB")


class Gate(unittest.TestCase):
    def test_gate(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ds.OUT, ds.FLAGS, ds.WATCHLIST, ds.ETF_WATCH = td / "o.json", td / "f.json", td / "w.json", td / "e.json"
            ds.TRADE_FILES = []
            ds.WATCHLIST.write_text(json.dumps({"always_include": ["near"]}))
            out = ds.run(now=NOW, get=fake_okx())
            self.assertEqual(out["summary"]["n_coins"], 1)
            self.assertIsNone(ds.run(now=NOW + timedelta(minutes=20), get=fake_okx()))
            self.assertIsNotNone(ds.run(now=NOW + timedelta(minutes=61), get=fake_okx()))


if __name__ == "__main__":
    unittest.main()
