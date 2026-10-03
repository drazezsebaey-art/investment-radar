"""
engine/universe.py - point-in-time universe and liquidity tiers (RC-1.2 sections 3 and 5).

eligible-by-day was built by the data lake from volumes of days D-30..D-1 only.
"""
import gzip
import json
from pathlib import Path

TIERS = (("L1", 100_000_000), ("L2", 20_000_000), ("L3", 5_000_000))
SLIPPAGE_PER_SIDE = {"L1": 0.0003, "L2": 0.0008, "L3": 0.0020}   # RC section 5 (cost proxy C1)
FEE_PER_SIDE = 0.0010


class Universe:
    def __init__(self, uni_dir: Path):
        with gzip.open(Path(uni_dir) / "eligible-by-day.json.gz", "rt", encoding="utf-8") as f:
            raw = json.load(f)
        self.by_day = {d: [(s, v) for s, v in rows] for d, rows in raw.items()}
        self.broad = None

    def eligible(self, day: str) -> list:
        return self.by_day.get(day, [])

    def top(self, day: str, n: int = 150) -> list:
        return [s for s, _ in self.eligible(day)[:n]]

    def regime_universe(self, day: str) -> list:
        """RC-1.3: top 100 by 30-day median volume WITHOUT the $5M floor (needs
        attach_broad()); falls back to the tradeable list only if not attached."""
        if self.broad is not None:
            return self.broad.get(day, [])[:100]
        return self.top(day, 100)

    def attach_broad(self, daily: dict, start: str, end: str):
        """Broad ranking per day D from volumes of D-30..D-1: every non-excluded
        USDT pair with >= 60 days of history, no volume floor."""
        import pandas as pd
        from build_data_lake import is_excluded, looks_pegged  # same exclusion rules as the lake
        days = pd.date_range(start, end, freq="D", tz="UTC")
        med, ok = {}, {}
        for s, d in daily.items():
            if is_excluded(s) or len(d) < 61:
                continue
            qv = d["quote_volume"].shift(1).rolling(30).median()          # D-30..D-1
            age = pd.Series(range(len(d)), index=d.index)
            closes = d["close"].shift(1)
            pegged = closes.rolling(30).apply(lambda w: looks_pegged(list(w)), raw=False)
            valid = (age >= 60) & qv.notna() & (pegged != 1)
            med[s] = qv.where(valid).reindex(days)
        table = pd.DataFrame(med)
        self.broad = {}
        for D, row in table.iterrows():
            r = row.dropna().sort_values(ascending=False)
            self.broad[D.date().isoformat()] = list(r.index)
        return self

    def median_volume(self, symbol: str, day: str):
        for s, v in self.eligible(day):
            if s == symbol:
                return v
        return None

    def tier(self, symbol: str, day: str):
        v = self.median_volume(symbol, day)
        if v is None:
            return None                       # NOT_TRADEABLE on that day
        for name, floor in TIERS:
            if v >= floor:
                return name
        return None


def round_trip_cost(tier: str) -> float:
    """c = (fee + slippage) x 2 sides (RC section 5)."""
    return 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE[tier])
