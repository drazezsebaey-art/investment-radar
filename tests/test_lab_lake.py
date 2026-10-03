"""Data lake: offline tests of the rules that define the data (RC-1.2)."""
import sys, tempfile, unittest, hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lab"))
import build_data_lake as L  # noqa: E402


def day_rows(start: date, n: int, qv: float, close: float = 10.0):
    out = []
    for i in range(n):
        ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000) + i * 86_400_000
        out.append([ms, close, close * 1.05, close * 0.95, close * (1 + 0.01 * (i % 5)), 1.0, ms + 86_399_999, qv, 10, 0.5, qv / 2])
    return out


class T(unittest.TestCase):
    def test_timestamp_units(self):
        self.assertEqual(L.to_ms("1704067200000"), 1704067200000)
        self.assertEqual(L.to_ms("1748736000000000"), 1748736000000)

    def test_exclusions(self):
        self.assertTrue(L.is_excluded("USDCUSDT"))
        self.assertTrue(L.is_excluded("BTCUPUSDT"))
        self.assertTrue(L.is_excluded("PAXGUSDT"))
        self.assertFalse(L.is_excluded("JUPUSDT"))      # ends with UP but is not a leveraged token
        self.assertFalse(L.is_excluded("SOLUSDT"))

    def test_peg_behaviour(self):
        self.assertTrue(L.looks_pegged([1.0, 1.001, 0.999] * 10))
        self.assertFalse(L.looks_pegged([10.0, 11.0, 9.5] * 10))

    def test_eligibility_point_in_time(self):
        daily = {"AAAUSDT": day_rows(date(2021, 1, 1), 120, 6e6), "BBBUSDT": day_rows(date(2021, 1, 1), 120, 1e6)}
        by_day = L.eligibility_by_day(daily)
        days = sorted(by_day)
        self.assertEqual(days[0], (date(2021, 1, 1) + timedelta(days=60)).isoformat())  # 60-day history rule
        self.assertTrue(all(s == "AAAUSDT" for d in by_day for s, _ in by_day[d]))      # low volume never eligible

    def test_volume_window_excludes_day_itself(self):
        rows = day_rows(date(2021, 1, 1), 100, 1e6)
        rows[80][7] = 9e9                       # huge volume on day 80 only
        by_day = L.eligibility_by_day({"AAAUSDT": rows})
        self.assertNotIn((date(2021, 1, 1) + timedelta(days=80)).isoformat(), by_day)

    def test_start_date_and_splits(self):
        by_day = {"2021-03-01": [("X", 1)] * 99, "2021-03-02": [("X", 1)] * 100}
        self.assertEqual(L.start_date(by_day), date(2021, 3, 2))
        s = L.scaled_splits(date(2024, 1, 1))
        self.assertEqual(s["validation"][0], "2025-01-01")
        self.assertEqual(s["out_of_sample"][0], "2025-07-01")
        s2 = L.scaled_splits(date(2021, 3, 2))
        self.assertLess(s2["development"][1], s2["validation"][0])
        self.assertEqual(s2["out_of_sample"][1], "2026-09-30")

    def test_hourly_last_sample(self):
        h = 3_600_000
        rows = [[0, 1], [5 * 60_000, 2], [h + 60_000, 3], [h + 55 * 60_000, 4]]
        self.assertEqual(L.hourly_last_samples(rows), [[5 * 60_000, 2], [h + 55 * 60_000, 4]])

    def test_deterministic_tar(self):
        d = Path(tempfile.mkdtemp())
        L.write_tar(d / "a.tar.gz", {"1h/X.csv": [[1, 2]]}, ["a", "b"])
        L.write_tar(d / "b.tar.gz", {"1h/X.csv": [[1, 2]]}, ["a", "b"])
        self.assertEqual(hashlib.sha256((d / "a.tar.gz").read_bytes()).hexdigest(),
                         hashlib.sha256((d / "b.tar.gz").read_bytes()).hexdigest())

    def test_gap_report(self):
        r = L.gap_report([[0], [3_600_000], [10_800_000], [10_800_000]], 3_600_000)
        self.assertEqual((r["gaps"], r["duplicates"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
