"""
engine/data.py - load the frozen data lake (RC-1.2 sections 2 and 14).

Every file is checked against lake-manifest.json before use; a mismatch stops
the run (the dataset_version a result is tied to must be the data it used).
"""
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
LAKE_DIR = ROOT / "lake"
UNI_DIR = ROOT / "data" / "lab" / "universe"
RELEASE_URL = "https://github.com/drazezsebaey-art/investment-radar/releases/download/data-lake-v1"
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
              "trades", "taker_buy_base", "taker_buy_quote"]
H1, H4, D1 = 3_600_000, 14_400_000, 86_400_000


class DataIntegrityError(RuntimeError):
    pass


class Lake:
    def __init__(self, lake_dir: Path = LAKE_DIR, uni_dir: Path = UNI_DIR, download: bool = True):
        self.lake_dir, self.uni_dir, self.download = Path(lake_dir), Path(uni_dir), download
        self.manifest = json.loads((self.uni_dir / "lake-manifest.json").read_text())
        self.dataset_version = self.manifest["dataset_version"]
        self.config = json.loads((self.uni_dir / "lake-config.json").read_text())
        self._verified = set()

    # ---- integrity -------------------------------------------------------
    def path(self, name: str) -> Path:
        p = self.lake_dir / name
        if not p.exists():
            if not self.download:
                raise DataIntegrityError(f"missing lake file {name}")
            self.lake_dir.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(f"{RELEASE_URL}/{name}", p)
        if name not in self._verified:
            want = self.manifest["files"].get(name, {}).get("sha256")
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            if want != got:
                raise DataIntegrityError(f"{name}: sha256 {got[:12]} != manifest {str(want)[:12]} - stop")
            self._verified.add(name)
        return p

    # ---- readers ---------------------------------------------------------
    @staticmethod
    def _read_tar(path: Path, prefix: str, symbols=None) -> dict:
        out = {}
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if not m.name.startswith(prefix):
                    continue
                sym = m.name[len(prefix):-4]
                if symbols is not None and sym not in symbols:
                    continue
                out[sym] = pd.read_csv(io.BytesIO(tar.extractfile(m).read()))
        return out

    @staticmethod
    def _index_klines(df: pd.DataFrame) -> pd.DataFrame:
        df = df.drop_duplicates("open_time").sort_values("open_time")
        df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df

    def daily(self, symbols=None) -> dict:
        raw = self._read_tar(self.path("spot-1d-all.tar.gz"), "1d/", symbols)
        return {s: self._index_klines(d) for s, d in raw.items() if len(d)}

    def hourly(self, years, symbols=None) -> dict:
        parts = {}
        for y in years:
            name = f"spot-1h-{y}.tar.gz"
            if name not in self.manifest["files"]:
                continue
            for s, d in self._read_tar(self.path(name), "1h/", symbols).items():
                parts.setdefault(s, []).append(d)
        return {s: self._index_klines(pd.concat(v)) for s, v in parts.items()}


def resample_4h(h1: pd.DataFrame) -> pd.DataFrame:
    """4H candles from 1H, aligned to 00/04/08/12/16/20 UTC (Binance alignment).
    `complete` is False when any of the 4 hours is missing -> DATA_ERROR upstream."""
    key = (h1["open_time"] // H4) * H4
    g = h1.groupby(key)
    out = pd.DataFrame({
        "open_time": g["open_time"].first().index.values,
        "open": g["open"].first().values, "high": g["high"].max().values,
        "low": g["low"].min().values, "close": g["close"].last().values,
        "volume": g["volume"].sum().values, "quote_volume": g["quote_volume"].sum().values,
        "taker_buy_quote": g["taker_buy_quote"].sum().values, "n_hours": g.size().values,
    })
    out["close_time"] = out["open_time"] + H4 - 1
    out["complete"] = out["n_hours"] == 4
    out.index = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    return out
