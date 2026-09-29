import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import hypothesis_check as hc


def tr(arch, conf, win, day="2026-10-05", entry=1.0):
    return {"status": "closed_targets_complete" if win else "stopped", "entry_archetype": arch,
            "confidence_score_at_entry": conf, "created_at": f"{day}T00:00:00+00:00",
            "entry": entry, "exit_price": 1.2 if win else 0.93}


class T(unittest.TestCase):
    def run_with(self, trades):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "t.json"; f.write_text(json.dumps(trades))
            return hc.run(files={"x": f}, out=Path(td) / "o.json")

    def test_in_sample_trades_ignored(self):
        r = self.run_with([tr("extension_continuation", 40, True, day="2026-09-20")])
        self.assertEqual(r["forward_closed_trades"], 0)

    def test_pending_below_min_n(self):
        r = self.run_with([tr("extension_continuation", 40, True)] * 5 + [tr("rotation_lag", 60, False)] * 5)
        self.assertEqual(r["hypotheses"]["H1"]["verdict"], "PENDING")

    def test_supported_and_not_supported(self):
        good = [tr("extension_continuation", 40, i % 10 < 6) for i in range(40)]      # 60% wins
        rest = [tr("rotation_lag", 60, i % 10 < 3) for i in range(40)]                # 30% wins
        r = self.run_with(good + rest)
        self.assertEqual(r["hypotheses"]["H1"]["verdict"], "SUPPORTED")
        self.assertEqual(r["hypotheses"]["H2"]["verdict"], "SUPPORTED")
        flat = [tr("extension_continuation", 40, i % 2 == 0) for i in range(40)] + \
               [tr("rotation_lag", 60, i % 2 == 0) for i in range(40)]
        self.assertEqual(self.run_with(flat)["hypotheses"]["H1"]["verdict"], "NOT_SUPPORTED")


if __name__ == "__main__":
    unittest.main()
