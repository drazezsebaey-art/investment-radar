"""Lab probe: offline tests of the parsers and the venue decision."""
import io, sys, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lab"))
import probe_data as p  # noqa: E402

XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>true</IsTruncated><NextMarker>data/spot/monthly/klines/BTCUSDT/</NextMarker>
<Contents><Key>data/spot/monthly/klines/X/1d/X-1d-2024-01.zip</Key></Contents>
<Contents><Key>data/spot/monthly/klines/X/1d/X-1d-2024-01.zip.CHECKSUM</Key></Contents>
<CommonPrefixes><Prefix>data/spot/monthly/klines/ADAUSDT/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>data/spot/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes>
</ListBucketResult>"""


class T(unittest.TestCase):
    def test_listing(self):
        pre, keys, nxt = p.parse_s3_listing(XML)
        self.assertEqual(len(pre), 2)
        self.assertEqual(nxt, "data/spot/monthly/klines/BTCUSDT/")
        self.assertEqual([p.month_of(k) for k in keys], ["2024-01", None])

    def test_day(self):
        self.assertEqual(p.day_of("a/BTCUSDT-metrics-2024-01-01.zip"), "2024-01-01")

    def test_ts_unit(self):
        self.assertEqual(p.ts_unit("1704067200000"), "milliseconds")
        self.assertEqual(p.ts_unit("1735689600000000"), "microseconds")

    def test_zip(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("x.csv", "1704067200000,42000,42100,41900,42050,10\n1704070800000,1,2,0.5,1.5,3\n")
        head, n = p.read_zip_csv_head(b.getvalue())
        self.assertEqual(n, 2)

    def test_decide(self):
        rep = {"archive": {"spot_1h_2024_01": {"status": 200}}, "coverage": {"usdt_pairs": 600, "delisted_usdt_pairs": 150},
               "okx": {"rows": 5}}
        self.assertEqual(p.decide(rep)["venue"], "BINANCE")
        self.assertEqual(p.decide(rep)["universe_tier"], "A")
        rep["archive"]["spot_1h_2024_01"]["status"] = 403
        self.assertEqual(p.decide(rep)["venue"], "OKX_FALLBACK")


if __name__ == "__main__":
    unittest.main()
