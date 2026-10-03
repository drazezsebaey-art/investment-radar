"""Engine part 1: data integrity, 4H resampling, tiers, regime rules and leakage."""
import sys, unittest, json, tempfile, hashlib
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
from engine.data import Lake, resample_4h, DataIntegrityError, H1  # noqa: E402
from engine.universe import Universe, round_trip_cost  # noqa: E402
from engine import regime as R  # noqa: E402

REAL_LAKE = (ROOT / "lake" / "spot-1d-all.tar.gz").exists() and (ROOT / "data/lab/universe/lake-manifest.json").exists()


def hourly(n, start_ms=1704067200000, drop=()):
    rows = []
    for i in range(n):
        if i in drop:
            continue
        t = start_ms + i * H1
        rows.append({"open_time": t, "open": 1 + i, "high": 2 + i, "low": i, "close": 1.5 + i, "volume": 1,
                     "quote_volume": 10, "taker_buy_quote": 5})
    return pd.DataFrame(rows)


class TestData(unittest.TestCase):
    def test_resample_alignment_and_completeness(self):
        b = resample_4h(hourly(8, drop={5}))
        self.assertEqual(len(b), 2)
        self.assertEqual(int(b["open_time"].iloc[0]) % 14_400_000, 0)
        self.assertEqual(b["high"].iloc[0], 2 + 3)
        self.assertEqual(b["close"].iloc[0], 1.5 + 3)
        self.assertEqual(list(b["complete"]), [True, False])

    def test_checksum_mismatch_stops(self):
        d = Path(tempfile.mkdtemp())
        (d / "x.tar.gz").write_bytes(b"abc")
        (d / "lake-manifest.json").write_text(json.dumps({"dataset_version": "v", "files": {"x.tar.gz": {"sha256": "0" * 64}}}))
        (d / "lake-config.json").write_text("{}")
        with self.assertRaises(DataIntegrityError):
            Lake(lake_dir=d, uni_dir=d, download=False).path("x.tar.gz")

    def test_costs(self):
        self.assertAlmostEqual(round_trip_cost("L1"), 0.0026)
        self.assertAlmostEqual(round_trip_cost("L3"), 0.006)


class TestRegimeRules(unittest.TestCase):
    def test_vol_percentile_uses_strict_past(self):
        idx = pd.date_range("2020-01-01", periods=800, freq="D", tz="UTC")
        c = pd.Series(np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.02, 800))), index=idx)
        btc = pd.DataFrame({"close": c, "high": c * 1.01, "low": c * 0.99})
        full = R.btc_features(btc)["vol_pct"]
        cut = R.btc_features(btc.iloc[:600])["vol_pct"]
        pd.testing.assert_series_equal(full.iloc[:600], cut, check_names=False)


@unittest.skipUnless(REAL_LAKE, "real lake not present")
class TestRealLake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lake = Lake(download=False)
        cls.daily = cls.lake.daily()
        cls.uni = Universe(ROOT / "data/lab/universe")

    def test_regime_truncation_no_leakage(self):
        full = R.compute_regimes(self.daily, self.uni, "2022-01-01", "2022-12-31")
        cut_daily = {s: d.loc[:"2022-06-30"] for s, d in self.daily.items()}
        cut = R.compute_regimes(cut_daily, self.uni, "2022-01-01", "2022-07-01")
        self.assertTrue((full.loc[:"2022-07-01", "regime"] == cut["regime"]).all())

    def test_known_periods(self):
        reg = R.compute_regimes(self.daily, self.uni, "2022-06-15", "2022-06-25")
        self.assertTrue((reg["regime"] == "RISK_OFF").all())          # post-LUNA / 3AC crash


if __name__ == "__main__":
    unittest.main()
