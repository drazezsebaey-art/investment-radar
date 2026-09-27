"""
trials_check.py (v62) - multiple-testing guard (observability only).

Reads config/trials-log.json and compares every tracked parameter's
current_value with the literal assignment actually in the code. A mismatch
means a threshold was changed WITHOUT being logged as a trial - exactly the
silent parameter-fitting the backtest-discipline audit warned about. Also
lists trials below the minimum sample size that were never re-validated.
Writes data/trials-report.json; never fails the workflow.
"""
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "config" / "trials-log.json"
OUT = ROOT / "data" / "trials-report.json"


def read_constant(source: str, name: str):
    m = re.search(rf"^{re.escape(name)}\s*=\s*(.+?)(?:\s+#.*)?$", source, re.M)
    if not m:
        return None, "not_found"
    try:
        return ast.literal_eval(m.group(1).strip()), "ok"
    except (ValueError, SyntaxError):
        return m.group(1).strip(), "unparseable"


def same(a, b) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def run(root: Path = ROOT):
    log = json.loads((root / "config" / "trials-log.json").read_text(encoding="utf-8"))
    min_n = log.get("min_sample_for_change", 30)
    untracked, missing = [], []
    for p in log.get("tracked_parameters", []):
        f = root / p["file"]
        if not f.exists():
            missing.append({**p, "problem": "file_missing"})
            continue
        val, status = read_constant(f.read_text(encoding="utf-8"), p["name"])
        if status != "ok":
            missing.append({**p, "problem": status})
        elif not same(val, p["current_value"]):
            untracked.append({"name": p["name"], "file": p["file"], "logged": p["current_value"], "in_code": val})
    weak = [t for t in log.get("trials", [])
            if t.get("n") is not None and t["n"] < min_n and not t.get("revalidated")]
    report = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "trials_count_total": len(log.get("trials", [])),
        "untracked_parameter_changes": untracked,
        "tracking_problems": missing,
        "unvalidated_below_min_sample": [{"id": t["id"], "parameter": t["parameter"], "n": t["n"]} for t in weak],
        "status": "WARN" if (untracked or missing) else "OK",
    }
    (root / "data" / "trials-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Trials check: {report['status']} - {len(untracked)} untracked change(s), "
          f"{len(weak)} trial(s) below n={min_n} not yet re-validated.")
    for u in untracked:
        print(f"  ! {u['name']} in {u['file']}: code={u['in_code']} but trials-log says {u['logged']} - log it as a trial")
    return report


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"trials_check failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
