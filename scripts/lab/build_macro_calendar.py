"""
build_macro_calendar.py - RC-1.3 section 2 (macro calendar), run ONCE before any
test; the committed data/lab/macro-calendar.json is what the engine reads.

  CPI and NFP: actual release dates from FRED (release 10 = CPI, 50 = Employment
               Situation), released 08:30 New York time.
  FOMC:        the SCHEDULED statement days (2:00 pm New York). Unscheduled
               emergency actions (e.g. 3 and 15 March 2020) are deliberately
               absent - they were not known in advance (RC: scheduled-information
               filter).
Needs FRED_API_KEY (repo secret already used by macro_gold.py).
"""
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parents[2] / "data" / "lab" / "macro-calendar.json"
NY, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")
FOMC = {
    2020: ["01-29", "04-29", "06-10", "07-29", "09-16", "11-05", "12-16"],
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
}


def to_utc(day: str, hh: int, mm: int) -> str:
    local = datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=NY)
    return local.astimezone(UTC).isoformat()


def fred_dates(release_id: int, key: str) -> list:
    url = (f"https://api.stlouisfed.org/fred/release/dates?release_id={release_id}&api_key={key}"
           "&file_type=json&realtime_start=2019-01-01&realtime_end=9999-12-31"
           "&include_release_dates_with_no_data=true&limit=10000&sort_order=asc")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"FRED request failed for release {release_id}: {e} (check the FRED_API_KEY secret)")
    return sorted({d["date"] for d in data.get("release_dates", []) if "2020-01-01" <= d["date"] <= "2026-12-31"})


def main():
    key = os.environ["FRED_API_KEY"]
    events = []
    for typ, rid in (("CPI", 10), ("NFP", 50)):
        for d in fred_dates(rid, key):
            events.append({"type": typ, "date": d, "scheduled_utc": to_utc(d, 8, 30), "source": f"FRED release {rid}"})
    for y, days in FOMC.items():
        for md in days:
            d = f"{y}-{md}"
            events.append({"type": "FOMC", "date": d, "scheduled_utc": to_utc(d, 14, 0), "source": "Fed scheduled meetings"})
    events.sort(key=lambda e: e["scheduled_utc"])
    for i, e in enumerate(events):
        e["id"] = f"{e['type']}-{e['date']}"
    for y in range(2020, 2027):
        print(y, {t: sum(1 for e in events if e["type"] == t and e["date"].startswith(str(y))) for t in ("CPI", "NFP", "FOMC")})
    # 2025 legitimately has fewer releases (US government shutdown Oct-Nov 2025:
    # releases cancelled or merged), so only an almost-empty year is treated as a FRED failure
    bad = [f"{t}-{y}" for y in range(2020, 2027) for t in ("CPI", "NFP")
           if sum(1 for e in events if e["type"] == t and e["date"].startswith(str(y))) < 6]
    if bad:
        raise SystemExit(f"almost no releases for {bad} - FRED answer looks wrong, calendar NOT written")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"built_at": datetime.now(UTC).isoformat(), "events": events}, indent=1), encoding="utf-8")
    print(f"written {len(events)} events -> {OUT}")


if __name__ == "__main__":
    main()
