"""
archive_snapshot.py (v62) - decision-snapshot archive.

Why: every run overwrites data/radar-flags.json, so the system keeps no record
of WHAT IT SAW when it made each decision. Without that record a walk-forward
test / Event Replay (deferred, "Phase 5") is impossible - and every day
without archiving is data that can never be recovered. This must be running
from the first run after the CoinGecko quota resets (1/10/2026).

What: a slimmed copy of radar-flags.json (every coin + every decision field,
minus the heavy real_candles arrays, which can be re-fetched from exchanges
later) written as minified JSON to data/archive/YYYY-MM-DD/HHMMZ.json.
Plain JSON on purpose: git compresses and delta-packs text itself, which
works better than committing opaque .gz blobs.

Cadence: at most once per ARCHIVE_EVERY_MINUTES (state-based gate, robust to
the irregular external cron). Retention: snapshot folders older than
RETENTION_DAYS are removed from the working tree (they remain in git history).
Never fails the workflow.
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAGS = ROOT / "data" / "radar-flags.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
STATE = ROOT / "data" / "archive-state.json"

ENGINE_VERSION = "archive-v62"
ARCHIVE_EVERY_MINUTES = 120
RETENTION_DAYS = 60
DROP_COIN_KEYS = {"real_candles", "real_candles_meta"}
KEEP_TOP_LEVEL = ("updated_at", "count", "market_breadth_pct_green", "market_regime")


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def gate_open(state: dict, now: datetime) -> bool:
    last = state.get("last_archived_at")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=ARCHIVE_EVERY_MINUTES - 5)
    except ValueError:
        return True


def slim(flags: dict) -> dict:
    out = {k: flags.get(k) for k in KEEP_TOP_LEVEL}
    out["archive_engine_version"] = ENGINE_VERSION
    out["coins"] = [{k: v for k, v in c.items() if k not in DROP_COIN_KEYS} for c in flags.get("coins", [])]
    return out


def prune(now: datetime, archive_dir: Path = ARCHIVE_DIR) -> int:
    removed = 0
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date()
    if not archive_dir.exists():
        return 0
    for d in archive_dir.iterdir():
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d.is_dir() and day < cutoff:
            shutil.rmtree(d)
            removed += 1
    return removed


def run(now: datetime = None, flags_path: Path = FLAGS, archive_dir: Path = ARCHIVE_DIR, state_path: Path = STATE):
    now = now or datetime.now(timezone.utc)
    state = load(state_path, {})
    if not gate_open(state, now):
        print(f"Archive gate closed (every {ARCHIVE_EVERY_MINUTES} min). Last: {state.get('last_archived_at')}")
        return None
    flags = load(flags_path, None)
    if not flags or not flags.get("coins"):
        print("Archive skipped: radar-flags.json missing or empty (nothing trustworthy to archive).")
        return None
    if state.get("last_source_updated_at") == flags.get("updated_at"):
        print("Archive skipped: radar-flags.json unchanged since the last snapshot (stale scan).")
        return None
    day_dir = archive_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{now.strftime('%H%M')}Z.json"
    path.write_text(json.dumps(slim(flags), separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    removed = prune(now, archive_dir)
    state = {"last_archived_at": now.isoformat(), "last_source_updated_at": flags.get("updated_at"),
             "last_path": str(path.relative_to(archive_dir.parent.parent)) if archive_dir.parent.parent in path.parents else str(path),
             "n_snapshots_total": state.get("n_snapshots_total", 0) + 1}
    Path(state_path).write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"Archived snapshot -> {path.name} ({path.stat().st_size // 1024} KB), pruned {removed} old day folder(s).")
    return path


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"archive_snapshot failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
