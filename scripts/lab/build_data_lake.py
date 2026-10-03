"""
build_data_lake.py - Research Contract RC-1.2, step 2 ("data lake").

Stages (run by .github/workflows/lab-data-lake.yml):

  universe   All Binance USDT spot pairs ever listed (incl. delisted), daily
             candles for all of them, the point-in-time eligible universe for
             every day (RC section 3), the computed start date (first day with
             >= 100 eligible pairs) and the scaled split dates (RC section 8).
             Writes data/lab/universe/*  (small, committed to the repo) and
             lake/spot-1d-all.tar.gz (release asset).
  spot Y     1H candles for every pair that is in the top-150 universe on any
             day of year Y (or within the 365-day warm-up before the start),
             -> lake/spot-1h-Y.tar.gz
  futures Y  USDT-M perpetual funding (8H) and metrics (OI + long/short ratios,
             reduced to the last 5-min sample of each hour) for the same pairs,
             -> lake/futures-Y.tar.gz

Rules implemented here (not in the engine) because they define the data:
  * timestamps normalised to milliseconds UTC (Binance spot files switched to
    microseconds in 2025 - confirmed by the probe on 2026-10-03)
  * monthly archive first, daily files for months not yet published
  * point-in-time eligibility: >= 60 days of history before D, 30-day median
    daily quote volume (days D-30..D-1) >= $5M, not a stablecoin / pegged /
    leveraged token; top 150 by that median
  * nothing is fetched from the REST API (geo-blocked from GitHub runners)

Standard library only.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import statistics
import sys
import tarfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_data as pd  # noqa: E402  (S3 listing + http helpers)

ROOT = Path(__file__).resolve().parents[2]
UNI_DIR = ROOT / "data" / "lab" / "universe"
LAKE = ROOT / "lake"
CONTRACT = "RC-1.2"

MIN_HISTORY_DAYS = 60
MIN_MEDIAN_QUOTE_VOL = 5_000_000
VOL_WINDOW = 30
TOP_N = 150
MIN_ELIGIBLE_FOR_START = 100
WARMUP_DAYS = 365
END_DATE = date(2026, 9, 30)
THREADS = 24

# Original RC split (2024-01-01 .. 2026-09-30) - scaled with the same proportions
ORIG_START, ORIG_VAL, ORIG_OOS, ORIG_END = date(2024, 1, 1), date(2025, 1, 1), date(2025, 7, 1), date(2026, 9, 30)

STABLE_BASES = {"USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "USDSB", "SUSD", "EUR", "GBP",
                "AUD", "AEUR", "EURI", "USDE", "PYUSD", "RLUSD", "XUSD", "BFUSD", "USD1", "UST", "USTC",
                "PAXG", "XAUT", "BKRW", "IDRT", "BIDR", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR"}
LEVERAGED_ROOTS = {"BTC", "ETH", "BNB", "ADA", "XRP", "DOT", "LINK", "EOS", "TRX", "XTZ", "LTC", "SXP", "FIL",
                   "YFI", "BCH", "AAVE", "SUSHI", "UNI", "1INCH", "XLM"}

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
              "trades", "taker_buy_base", "taker_buy_quote"]


# ------------------------------------------------------------- helpers ----
def to_ms(ts) -> int:
    """Binance archive timestamps: ms before 2025, microseconds after."""
    t = int(float(ts))
    return t // 1000 if t >= 10 ** 15 else t


def is_excluded(symbol: str) -> bool:
    base = symbol[:-4]
    if base in STABLE_BASES:
        return True
    for suf in ("UP", "DOWN", "BULL", "BEAR"):
        if base.endswith(suf) and base[: -len(suf)] in LEVERAGED_ROOTS:
            return True
    return False


def looks_pegged(closes: list) -> bool:
    """Peg behaviour (catches stablecoins not in the list): median close within
    3% of $1 and a 30-day range under 1.2%."""
    if len(closes) < 30:
        return False
    tail = closes[-30:]
    med = statistics.median(tail)
    return abs(med - 1) <= 0.03 and (max(tail) - min(tail)) / med <= 0.012


def read_zip_rows(blob: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as f:
            rows = list(csv.reader(io.TextIOWrapper(f, "utf-8")))
    return [r for r in rows if r and r[0].strip().lstrip("-").replace(".", "").isdigit()]  # drop header


def kline_rows(blob: bytes) -> list:
    out = []
    for r in read_zip_rows(blob):
        out.append([to_ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                    to_ms(r[6]), float(r[7]), int(float(r[8])), float(r[9]), float(r[10])])
    return out


def fetch(path: str):
    status, blob, err = pd.http(f"{pd.ARCHIVE}/{path}", binary=True)
    return blob if status == 200 else None


def parallel(fn, items):
    with ThreadPoolExecutor(THREADS) as ex:
        return list(ex.map(fn, items))


def months_between(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def days_of_month(ym: str):
    y, m = map(int, ym.split("-"))
    d = date(y, m, 1)
    while d.month == m and d <= END_DATE:
        yield d.isoformat()
        d += timedelta(days=1)


def klines_for(symbol: str, interval: str, months: list, published: set) -> list:
    """Monthly file when published, otherwise the daily files of that month."""
    rows = []
    for ym in months:
        if ym in published:
            blob = fetch(f"data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip")
            if blob:
                rows += kline_rows(blob)
                continue
        for d in days_of_month(ym):
            blob = fetch(f"data/spot/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d}.zip")
            if blob:
                rows += kline_rows(blob)
    rows.sort(key=lambda r: r[0])
    dedup, last = [], None
    for r in rows:
        if r[0] != last:
            dedup.append(r)
            last = r[0]
    return dedup


# ------------------------------------------------- universe and splits ----
def eligibility_by_day(daily: dict) -> dict:
    """daily: {symbol: [[open_ms, o, h, l, c, v, close_ms, quote_v, ...], ...]}
    -> {iso_day: [(symbol, median_quote_vol), ...] eligible on that day, sorted desc}."""
    by_day = {}
    for sym, rows in daily.items():
        if is_excluded(sym) or not rows:
            continue
        first_day = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).date()
        days = [datetime.fromtimestamp(r[0] / 1000, timezone.utc).date() for r in rows]
        qv = [r[7] for r in rows]
        closes = [r[4] for r in rows]
        for i in range(VOL_WINDOW, len(rows)):
            d = days[i]
            if (d - first_day).days < MIN_HISTORY_DAYS or d > END_DATE:
                continue
            window = qv[i - VOL_WINDOW:i]                      # days D-30 .. D-1, never day D itself
            med = statistics.median(window)
            if med < MIN_MEDIAN_QUOTE_VOL or looks_pegged(closes[i - VOL_WINDOW:i]):
                continue
            by_day.setdefault(d.isoformat(), []).append((sym, round(med)))
    for d in by_day:
        by_day[d].sort(key=lambda x: (-x[1], x[0]))
    return by_day


def start_date(by_day: dict):
    for d in sorted(by_day):
        if len(by_day[d]) >= MIN_ELIGIBLE_FOR_START:
            return date.fromisoformat(d)
    return None


def scaled_splits(start: date, end: date = END_DATE) -> dict:
    total = (ORIG_END - ORIG_START).days
    f_val = (ORIG_VAL - ORIG_START).days / total
    f_oos = (ORIG_OOS - ORIG_START).days / total
    span = (end - start).days

    def month_floor(d):
        return date(d.year, d.month, 1)
    val = month_floor(start + timedelta(days=round(span * f_val)))
    oos = month_floor(start + timedelta(days=round(span * f_oos)))
    return {"development": [start.isoformat(), (val - timedelta(days=1)).isoformat()],
            "validation": [val.isoformat(), (oos - timedelta(days=1)).isoformat()],
            "out_of_sample": [oos.isoformat(), end.isoformat()],
            "warmup_from": (start - timedelta(days=WARMUP_DAYS)).isoformat(),
            "proportions": {"development": round(f_val, 4), "validation": round(f_oos - f_val, 4),
                            "out_of_sample": round(1 - f_oos, 4)}}


def stage_universe():
    prefixes, _ = pd.s3_list("data/spot/monthly/klines/")
    symbols = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)
    symbols = [s for s in symbols if s.endswith("USDT")]
    print(f"USDT pairs in archive: {len(symbols)}")
    published = {}

    def months_for(sym):
        try:
            _, keys = pd.s3_list(f"data/spot/monthly/klines/{sym}/1d/", delimiter="")
            return sym, sorted({m for m in (pd.month_of(k) for k in keys) if m})
        except Exception:  # noqa: BLE001
            return sym, []
    for sym, months in parallel(months_for, symbols):
        published[sym] = months
    latest = max((m[-1] for m in published.values() if m), default=None)
    print(f"latest published month: {latest}")

    def daily_for(sym):
        months = published.get(sym) or []
        if not months:
            return sym, []
        extra = [m for m in months_between(date.fromisoformat(latest + "-01"), END_DATE)][1:] if months[-1] == latest else []
        return sym, klines_for(sym, "1d", months + extra, set(months))
    daily = dict(parallel(daily_for, symbols))
    daily = {s: r for s, r in daily.items() if r}

    by_day = eligibility_by_day(daily)
    start = start_date(by_day)
    if not start:
        raise SystemExit("no day with >= 100 eligible pairs - stop (RC section 15, decision 2)")
    splits = scaled_splits(start)
    top = {d: [s for s, _ in v[:TOP_N]] for d, v in by_day.items()}
    needed = sorted({s for d, v in top.items() if d >= splits["warmup_from"] for s in v})

    UNI_DIR.mkdir(parents=True, exist_ok=True)
    LAKE.mkdir(parents=True, exist_ok=True)
    with gzip.open(UNI_DIR / "eligible-by-day.json.gz", "wt", encoding="utf-8") as f:
        json.dump(by_day, f)
    with gzip.open(UNI_DIR / "top150-by-day.json.gz", "wt", encoding="utf-8") as f:
        json.dump(top, f)
    delisted = sorted(s for s, m in published.items() if m and m[-1] != latest)
    config = {"contract": CONTRACT, "built_at": datetime.now(timezone.utc).isoformat(), "venue": "BINANCE",
              "universe_tier": "A", "start_date": start.isoformat(), "end_date": END_DATE.isoformat(),
              "splits": splits, "eligible_on_start": len(by_day[start.isoformat()]),
              "pairs_total": len(symbols), "pairs_delisted": len(delisted), "pairs_needed": len(needed),
              "needed_delisted": len([s for s in needed if s in set(delisted)]),
              "rules": {"min_history_days": MIN_HISTORY_DAYS, "min_median_quote_vol": MIN_MEDIAN_QUOTE_VOL,
                        "vol_window_days": VOL_WINDOW, "top_n": TOP_N, "start_rule": "first day with >= 100 eligible"}}
    (UNI_DIR / "lake-config.json").write_text(json.dumps(config, indent=1), encoding="utf-8")
    (UNI_DIR / "needed-pairs.json").write_text(json.dumps(needed), encoding="utf-8")
    write_tar(LAKE / "spot-1d-all.tar.gz", {f"1d/{s}.csv": rows for s, rows in daily.items()}, KLINE_COLS)
    print(json.dumps(config, indent=1))


# ----------------------------------------------------- spot and futures ----
def write_tar(path: Path, tables: dict, header):
    """Deterministic .tar.gz (fixed mtimes, no gzip filename) so the same data
    always has the same sha256. header: a list, or a function name -> list."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, rows in sorted(tables.items()):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header(name) if callable(header) else header)
            w.writerows(rows)
            data = buf.getvalue().encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 0
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(raw.getvalue())


def gap_report(rows: list, step_ms: int) -> dict:
    gaps = sum(1 for a, b in zip(rows, rows[1:]) if b[0] - a[0] > step_ms)
    dups = sum(1 for a, b in zip(rows, rows[1:]) if b[0] == a[0])
    return {"rows": len(rows), "gaps": gaps, "duplicates": dups}


def year_window(year: int, cfg: dict):
    lo = max(date(year, 1, 1), date.fromisoformat(cfg["splits"]["warmup_from"]))
    hi = min(date(year, 12, 31), END_DATE)
    return lo, hi


def stage_spot(year: int):
    cfg = json.loads((UNI_DIR / "lake-config.json").read_text())
    needed = json.loads((UNI_DIR / "needed-pairs.json").read_text())
    lo, hi = year_window(year, cfg)
    if lo > hi:
        print("year outside the data window - nothing to do")
        return
    months = list(months_between(lo, hi))

    def get(sym):
        try:
            _, keys = pd.s3_list(f"data/spot/monthly/klines/{sym}/1h/", delimiter="")
            published = {m for m in (pd.month_of(k) for k in keys) if m}
        except Exception:  # noqa: BLE001
            published = set()
        return sym, klines_for(sym, "1h", months, published)
    tables, report = {}, {}
    for sym, rows in parallel(get, needed):
        if rows:
            tables[f"1h/{sym}.csv"] = rows
            report[sym] = gap_report(rows, 3_600_000)
    LAKE.mkdir(parents=True, exist_ok=True)
    write_tar(LAKE / f"spot-1h-{year}.tar.gz", tables, KLINE_COLS)
    (LAKE / f"spot-1h-{year}.report.json").write_text(json.dumps(report), encoding="utf-8")
    print(f"spot {year}: {len(tables)} pairs, {sum(r['gaps'] for r in report.values())} gaps")


def hourly_last_samples(rows: list) -> list:
    """metrics rows [create_ms, oi, oi_value, top_count_ls, top_sum_ls, count_ls, taker_ls] ->
    the last 5-min sample of every hour (engine applies the 15-minute freshness rule)."""
    by_hour = {}
    for r in rows:
        by_hour[r[0] // 3_600_000] = r                          # rows sorted -> last wins
    return [by_hour[h] for h in sorted(by_hour)]


def stage_futures(year: int):
    cfg = json.loads((UNI_DIR / "lake-config.json").read_text())
    needed = json.loads((UNI_DIR / "needed-pairs.json").read_text())
    lo, hi = year_window(year, cfg)
    if lo > hi:
        print("year outside the data window - nothing to do")
        return
    prefixes, _ = pd.s3_list("data/futures/um/monthly/fundingRate/")
    perps = {p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes}
    syms = [s for s in needed if s in perps]
    months = list(months_between(lo, hi))
    days = [d for ym in months for d in days_of_month(ym) if lo.isoformat() <= d <= hi.isoformat()]

    def get(sym):
        funding = []
        for ym in months:
            blob = fetch(f"data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{ym}.zip")
            if blob:
                funding += [[to_ms(r[0]), float(r[2])] for r in read_zip_rows(blob)]
        metrics = []
        for d in days:
            blob = fetch(f"data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d}.zip")
            if not blob:
                continue
            with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(z.namelist()[0]) as f:
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                    try:
                        ts = int(datetime.strptime(r["create_time"], "%Y-%m-%d %H:%M:%S")
                                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
                        metrics.append([ts, float(r["sum_open_interest"]), float(r["sum_open_interest_value"]),
                                        float(r["count_toptrader_long_short_ratio"] or 0),
                                        float(r["sum_toptrader_long_short_ratio"] or 0),
                                        float(r["count_long_short_ratio"] or 0),
                                        float(r["sum_taker_long_short_vol_ratio"] or 0)])
                    except (KeyError, ValueError):
                        continue
        metrics.sort(key=lambda r: r[0])
        funding.sort(key=lambda r: r[0])
        return sym, funding, hourly_last_samples(metrics)
    tables, report = {}, {}
    for sym, funding, metrics in parallel(get, syms):
        if funding:
            tables[f"funding/{sym}.csv"] = funding
        if metrics:
            tables[f"metrics/{sym}.csv"] = metrics
        report[sym] = {"funding_rows": len(funding), "metrics_hours": len(metrics)}
    LAKE.mkdir(parents=True, exist_ok=True)
    def header(name):
        return ["funding_time", "funding_rate"] if name.startswith("funding/") else \
            ["sample_time", "sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
             "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
    write_tar(LAKE / f"futures-{year}.tar.gz", tables, header)
    (LAKE / f"futures-{year}.report.json").write_text(json.dumps(report), encoding="utf-8")
    print(f"futures {year}: {len(syms)} perps, {len(tables)} tables")


def stage_manifest():
    """sha256 of every lake file -> data/lab/universe/lake-manifest.json; its own
    hash is the dataset_version (RC section 14)."""
    files = {}
    for p in sorted(LAKE.glob("*")):
        if p.is_file():
            files[p.name] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
    body = json.dumps(files, sort_keys=True)
    manifest = {"contract": CONTRACT, "built_at": datetime.now(timezone.utc).isoformat(),
                "dataset_version": hashlib.sha256(body.encode()).hexdigest()[:16], "files": files}
    UNI_DIR.mkdir(parents=True, exist_ok=True)
    (UNI_DIR / "lake-manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"dataset_version {manifest['dataset_version']} over {len(files)} files")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["universe", "spot", "futures", "manifest"])
    ap.add_argument("--year", type=int)
    a = ap.parse_args()
    t0 = time.time()
    if a.stage == "universe":
        stage_universe()
    elif a.stage == "spot":
        stage_spot(a.year)
    elif a.stage == "futures":
        stage_futures(a.year)
    else:
        stage_manifest()
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
