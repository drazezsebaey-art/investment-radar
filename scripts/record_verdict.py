"""
Investment Radar - Quick Agent Room verdict recorder (new, v6)
------------------------------------------------------------
Tiny CLI so logging a verdict after a full Agent Room review is one line
instead of hand-editing data/agent-room-log.json. Run agent_room_stats.py
afterward to refresh the KPI summary.

Usage:
  python scripts/record_verdict.py --coin optimism --symbol OP \\
      --triggered-by breakout_signal --quality beta_driven_or_cluster \\
      --verdict WAIT --confidence 55 \\
      --note "Base exit gutted buyback revenue; beta~2 to BTC"

--quality is optional (pass it if you have the coin's signal_quality field
from that run's radar-flags.json/signal-log.json handy) - omit it and it's
logged as "unknown" rather than guessed.
"""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_PATH = DATA_DIR / "agent-room-log.json"

VALID_VERDICTS = {"BUY NOW", "ACCUMULATE", "WAIT", "HOLD", "REDUCE", "EXIT"}
VALID_QUALITIES = {"idiosyncratic", "beta_driven_or_cluster", "unknown"}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--coin", required=True, help="coin_id as used elsewhere in the repo, e.g. optimism")
    p.add_argument("--symbol", required=True, help="e.g. OP")
    p.add_argument("--triggered-by", required=True,
                    help="which radar signal led to this review, e.g. breakout_signal")
    p.add_argument("--quality", default="unknown", choices=sorted(VALID_QUALITIES))
    p.add_argument("--verdict", required=True, choices=sorted(VALID_VERDICTS))
    p.add_argument("--confidence", required=True, type=int, choices=range(0, 101), metavar="0-100")
    p.add_argument("--note", default="")
    p.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (UTC)")
    args = p.parse_args()

    entries = []
    if LOG_PATH.exists():
        try:
            entries = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Warning: {LOG_PATH} was not valid JSON - starting a fresh list (old content NOT overwritten "
                  f"on disk yet, fix it manually if this is unexpected).")
            return

    entries.append({
        "date": args.date or datetime.now(timezone.utc).date().isoformat(),
        "coin_id": args.coin,
        "symbol": args.symbol,
        "triggered_by": args.triggered_by,
        "signal_quality_at_trigger": args.quality,
        "verdict": args.verdict,
        "confidence": args.confidence,
        "note": args.note,
    })

    LOG_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Logged: {args.symbol} -> {args.verdict} ({args.confidence}/100). "
          f"Total entries: {len(entries)}. Run agent_room_stats.py to refresh KPIs.")


if __name__ == "__main__":
    main()
