"""v69: pending expiry + V2 snapshot fallback."""
import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import track_trades as tt  # noqa: E402
import v2_track_trades as v2  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class TestPendingExpiry(unittest.TestCase):
    def test_old_pending_expires(self):
        t = {"status": "pending", "created_at": (NOW - timedelta(hours=261)).isoformat()}
        self.assertTrue(tt.expire_stale_pending(t, NOW))
        self.assertEqual(t["status"], "expired_unfilled")

    def test_fresh_pending_kept(self):
        t = {"status": "pending", "created_at": (NOW - timedelta(hours=10)).isoformat()}
        self.assertFalse(tt.expire_stale_pending(t, NOW))
        self.assertEqual(t["status"], "pending")

    def test_expired_not_counted_as_closed(self):
        self.assertNotIn("expired_unfilled", tt.CLOSED_STATUSES)


class TestV2Fallback(unittest.TestCase):
    def test_history_rows(self):
        pts = [{"t": "2026-09-24T19:00:00+00:00", "price": 2.7}, {"t": "2026-09-30T19:00:00+00:00", "price": 2.6},
               {"t": "2026-09-20T00:00:00+00:00", "price": 9.9}]
        rows = v2.history_rows_since(pts, "2026-09-24T18:27:08+00:00")
        self.assertEqual(len(rows), 2)
        self.assertEqual(min(r[2] for r in rows), 2.6)

    def test_below_stop_gets_stopped(self):
        t = {"status": "open", "entry": 2.74, "stop": 2.6686, "created_at": "2026-09-24T18:27:08+00:00",
             "targets": [{"horizon_label": "fast", "max_hours": 48, "target_price": 3.6}]}
        rows = v2.history_rows_since([{"t": "2026-09-30T19:00:00+00:00", "price": 2.6}], t["created_at"])
        v2.check_trade(t, rows, 2.59, NOW)
        self.assertEqual(t["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
