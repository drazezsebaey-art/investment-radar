"""
forward_t4.py - forward (out-of-time) paper test of T4-B, RC-1.3 cycle-1 decision
of 4 Oct 2026. Exact backtest definitions are kept by replaying each NEW month
from the Binance public archive once its monthly files exist (funding is only
published monthly), i.e. results arrive early the following month. Lagged, so
not a live-trading signal - but data nobody saw at design time.

Each run re-processes month M-1 and M (so trades opened late in M-1 are closed
with M's candles) and rewrites the ledger entries of both months.

Usage:  python scripts/lab/forward_t4.py 2026-10          (archive provider, on GitHub)
"""
import io
import json
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import t4 as T4  # noqa: E402
from engine.data import resample_4h  # noqa: E402
from engine.macro import MacroCalendar  # noqa: E402
from engine.regime import compute_regimes  # noqa: E402
from engine.universe import Universe  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "lab" / "forward"
FIRST_FORWARD_MONTH = "2026-10"                   # the sealed out-of-sample ends 2026-09-30
VARIANT = "B"


def month_bounds(ym: str):
    y, m = map(int, ym.split("-"))
    a = date(y, m, 1)
    b = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return a, b


def prev_month(ym: str) -> str:
    a, _ = month_bounds(ym)
    p = a - timedelta(days=1)
    return f"{p.year:04d}-{p.month:02d}"


def months_back(ym: str, n: int) -> list:
    out = [ym]
    for _ in range(n):
        out.append(prev_month(out[-1]))
    return sorted(out)


class ArchiveProvider:
    """Reads Binance's public archive (used on GitHub runners)."""
    def __init__(self):
        import build_data_lake as L
        self.L = L

    def usdt_pairs(self):
        import probe_data as pd_
        prefixes, _ = pd_.s3_list("data/spot/monthly/klines/")
        return sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes if p.rstrip("/").endswith("USDT"))

    def daily(self, symbols, months):
        def get(s):
            return s, self.L.klines_for(s, "1d", months, set(months))
        with ThreadPoolExecutor(24) as ex:
            rows = dict(ex.map(get, symbols))
        return {s: _frame(r) for s, r in rows.items() if r}

    def hourly(self, symbols, months):
        def get(s):
            return s, self.L.klines_for(s, "1h", months, set(months))
        with ThreadPoolExecutor(24) as ex:
            rows = dict(ex.map(get, symbols))
        return {s: _frame(r) for s, r in rows.items() if r}

    def futures(self, symbols, months, days):
        fund, met = {}, {}

        def get(s):
            f, m = [], []
            for ym in months:
                blob = self.L.fetch(f"data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip")
                if blob:
                    f += [[self.L.to_ms(r[0]), float(r[2])] for r in self.L.read_zip_rows(blob)]
            for d in days:
                blob = self.L.fetch(f"data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip")
                if not blob:
                    continue
                with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(z.namelist()[0]) as fh:
                    for r in pd.read_csv(fh).itertuples():
                        ts = int(datetime.strptime(r.create_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                        m.append([ts, r.sum_open_interest, r.count_long_short_ratio])
            return s, f, m
        with ThreadPoolExecutor(24) as ex:
            for s, f, m in ex.map(get, symbols):
                if f:
                    fund[s] = pd.DataFrame(sorted(f), columns=["funding_time", "funding_rate"])
                if m:
                    met[s] = pd.DataFrame(sorted(m), columns=["sample_time", "sum_open_interest", "count_long_short_ratio"])
        return {"funding": fund, "metrics": met}


def _frame(rows):
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote"]
    d = pd.DataFrame(rows, columns=cols).drop_duplicates("open_time").sort_values("open_time")
    d.index = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    return d


def universe_from_daily(daily: dict, start: str, end: str):
    import build_data_lake as L
    raw = {s: d[["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]].values.tolist()
           for s, d in daily.items()}
    by_day = L.eligibility_by_day(raw)
    u = Universe.__new__(Universe)
    u.by_day = by_day
    u.broad = None
    return u.attach_broad(daily, start, end)


def run_month(ym: str, provider) -> dict:
    if ym < FIRST_FORWARD_MONTH:
        raise SystemExit(f"{ym} is inside the sealed out-of-sample - forward test starts {FIRST_FORWARD_MONTH}")
    pm = prev_month(ym)
    a, _ = month_bounds(pm)
    _, b = month_bounds(ym)
    hist = months_back(ym, 13)                                      # 1d history for EMA200 / regime / volumes
    pairs = provider.usdt_pairs()
    daily = provider.daily(pairs, hist)
    uni = universe_from_daily(daily, a.isoformat(), b.isoformat())
    syms = sorted({s for d in pd.date_range(a, b, freq="D") for s in uni.top(d.date().isoformat(), 150)})
    hourly = provider.hourly(syms, months_back(ym, 2))
    c4 = {s: resample_4h(h) for s, h in hourly.items()}
    days = [d.date().isoformat() for d in pd.date_range(a - timedelta(days=2), b, freq="D")]
    fut = provider.futures(syms, [prev_month(pm), pm, ym], days)
    reg = compute_regimes(daily, uni, a.isoformat(), b.isoformat())
    macro = MacroCalendar(ROOT / "data" / "lab" / "macro-calendar.json")
    res = T4.run(c4, daily, fut, uni, reg, macro, a.isoformat(), b.isoformat(), variant=VARIANT)
    return {"months": [pm, ym], "trades": res["trades"], "signals": res["signals"], "regimes": reg["regime"].value_counts().to_dict()}


def update_ledger(result: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "t4b-ledger.json"
    led = json.loads(path.read_text()) if path.exists() else {"trader": "T4", "variant": VARIANT, "version": T4.VERSION,
                                                              "started": FIRST_FORWARD_MONTH, "months": {}}
    for ym in result["months"]:
        if ym < FIRST_FORWARD_MONTH:
            continue
        a, b = month_bounds(ym)
        lo = int(datetime(a.year, a.month, a.day, tzinfo=timezone.utc).timestamp() * 1000)
        hi = int((datetime(b.year, b.month, b.day, tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000)
        pick = lambda xs: [x for x in xs if lo <= x["signal_time"] < hi]
        led["months"][ym] = {"trades": pick(result["trades"]), "signals": pick(result["signals"]),
                             "processed_at": datetime.now(timezone.utc).isoformat()}
    from engine import metrics
    allt = [t for m in led["months"].values() for t in m["trades"] if t.get("status") == "CLOSED"]
    led["summary"] = metrics.summarize(allt) if allt else {"n": 0}
    path.write_text(json.dumps(led, indent=1, default=str), encoding="utf-8")
    return led


if __name__ == "__main__":
    ym = sys.argv[1] if len(sys.argv) > 1 else None
    if not ym:
        today = datetime.now(timezone.utc).date()
        last = today.replace(day=1) - timedelta(days=1)
        ym = f"{last.year:04d}-{last.month:02d}"
    led = update_ledger(run_month(ym, ArchiveProvider()))
    print(json.dumps({"month": ym, "summary": led["summary"]}, default=str, indent=1))
