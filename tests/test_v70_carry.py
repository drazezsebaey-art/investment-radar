"""v70: last deep evaluation carried forward for display only."""
import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scan  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class TestCarry(unittest.TestCase):
    def test_fresh_deep_eval_carried(self):
        prev = {"deep_eval_at": (NOW - timedelta(hours=1)).isoformat(),
                "coins": [{"id": "near", "confidence_score": 52, "signal_quality": "idiosyncratic", "breakout_signal": True}]}
        c = scan.carry_deep_evals(prev, NOW)
        self.assertEqual(c["near"]["confidence_score"], 52)
        self.assertTrue(c["near"]["breakout_signal"])

    def test_chain_carry_and_expiry(self):
        rec = {"confidence_score": 40, "evaluated_at": (NOW - timedelta(hours=5)).isoformat()}
        self.assertIn("sui", scan.carry_deep_evals({"coins": [{"id": "sui", "last_deep_eval": rec}]}, NOW))
        old = {"confidence_score": 40, "evaluated_at": (NOW - timedelta(hours=13)).isoformat()}
        self.assertNotIn("sui", scan.carry_deep_evals({"coins": [{"id": "sui", "last_deep_eval": old}]}, NOW))

    def test_never_writes_top_level_fields(self):
        prev = {"deep_eval_at": NOW.isoformat(), "coins": [{"id": "x", "confidence_score": 60, "breakout_signal": True}]}
        rec = scan.carry_deep_evals(prev, NOW)["x"]
        self.assertIn("evaluated_at", rec)   # nested record only - caller stores it under last_deep_eval


if __name__ == "__main__":
    unittest.main()
