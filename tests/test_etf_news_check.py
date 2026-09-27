import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import etf_news_check as m

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Grayscale files S-1 for Cardano ETF GADA</title><link>https://x/1</link>
<pubDate>Sun, 27 Sep 2026 08:00:00 GMT</pubDate><source url="https://c">CoinDesk</source></item>
<item><title>Old story ETF approval</title><link>https://x/2</link>
<pubDate>Mon, 01 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Grayscale converts Zcash trust into ETF</title><link>https://x/4</link>
<pubDate>Sun, 27 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Bitwise NEAR ETF launches on NYSE Arca</title><link>https://x/3</link>
<pubDate>Sat, 26 Sep 2026 20:00:00 GMT</pubDate></item>
</channel></rss>"""
EDGAR = json.dumps({"hits": {"hits": [{"_id": "0001193125-26-000001:a.htm", "_source": {
    "adsh": "0001193125-26-000001", "form": "S-1/A", "file_date": "2026-09-25",
    "display_names": ["Grayscale Cardano Trust (CIK 0001234567)"], "ciks": ["0001234567"]}}]}})


class T(unittest.TestCase):
    def test_rss_lookback(self):
        items = m.parse_news_rss(RSS, now=NOW)
        self.assertEqual([i["link"] for i in items], ["https://x/1", "https://x/4", "https://x/3"])

    def test_classify(self):
        self.assertEqual(m.classify("Bitwise NEAR ETF launches on NYSE Arca"), "launch")
        self.assertEqual(m.classify("SEC delays decision on TAO ETF"), "delay_or_rejection")
        self.assertEqual(m.classify("Grayscale files S-1 for Cardano ETF"), "filing")
        self.assertEqual(m.classify("Grayscale amends S-1/A for Cardano ETF"), "amendment")

    def test_edgar(self):
        f = m.parse_edgar_json(EDGAR)[0]
        self.assertEqual(f["form"], "S-1/A")
        self.assertIn("/1234567/000119312526000001/", f["link"])

    def test_gate(self):
        self.assertTrue(m.gate_open({}, NOW))
        self.assertFalse(m.gate_open({"last_run": (NOW - timedelta(minutes=30)).isoformat()}, NOW))
        self.assertTrue(m.gate_open({"last_run": (NOW - timedelta(hours=2)).isoformat()}, NOW))

    def test_match_no_ambiguous(self):
        uni = m.build_universe({"coins": [{"id": "near", "name": "NEAR Protocol", "symbol": "NEAR"},
                                          {"id": "cardano", "name": "Cardano", "symbol": "ADA"}]}, {"coins": {}})
        self.assertEqual(m.match_coins("ETF filings near record as Cardano rises", uni), {"cardano"})

    def test_cme_clock(self):
        rows = m.cme_clocks({"coins": {"cardano": {"cme_futures_since": "2026-02-09"},
                                       "uniswap": {"cme_futures_since": "2026-10-19"}}}, NOW)
        d = {r["coin"]: r for r in rows}
        self.assertTrue(d["cardano"]["eligible_now"])
        self.assertFalse(d["uniswap"]["eligible_now"])

    def test_run_end_to_end_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            m.WATCH_FILE, m.FLAGS_FILE = td / "w.json", td / "f.json"
            m.OUT_FILE, m.STATE_FILE = td / "o.json", td / "s.json"
            m.WATCH_FILE.write_text(json.dumps({"coins": {"cardano": {
                "match_terms": ["Cardano"], "edgar_phrases": ["Cardano ETF"], "news_queries": ["Cardano ETF"]}}}))
            m.FLAGS_FILE.write_text(json.dumps({"coins": [{"id": "near", "name": "NEAR Protocol", "symbol": "NEAR"},
                                                     {"id": "zcash", "name": "Zcash", "symbol": "ZEC"}]}))
            news = lambda q: m.parse_news_rss(RSS, now=NOW)
            edgar = lambda p, n: m.parse_edgar_json(EDGAR)
            out = m.run(now=NOW, fetch_news_fn=news, fetch_edgar_fn=edgar, sleep=lambda s: None)
            coins = {(i["coin"], i["new_coin_discovered"]) for i in out["alerts_this_run"]}
            self.assertIn(("cardano", False), coins)
            self.assertIn(("zcash", True), coins)         # discovered via universe, not in watch
            cardano_titles = [i["title"] for i in out["alerts_this_run"] if i["coin"] == "cardano"]
            self.assertTrue(all("Cardano" in t for t in cardano_titles))  # no misattribution
            self.assertTrue(out["alert"])
            out2 = m.run(now=NOW + timedelta(hours=3), fetch_news_fn=news, fetch_edgar_fn=edgar, sleep=lambda s: None)
            self.assertEqual(out2["alerts_this_run"], [])  # dedupe
            self.assertFalse(out2["alert"])
            self.assertGreater(len(out2["items"]), 0)      # rolling log kept

    def test_fetch_errors_non_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            m.WATCH_FILE, m.FLAGS_FILE = td / "w.json", td / "f.json"
            m.OUT_FILE, m.STATE_FILE = td / "o.json", td / "s.json"
            m.WATCH_FILE.write_text(json.dumps({"coins": {"x": {"edgar_phrases": ["a"], "news_queries": ["b"]}}}))
            def boom(*a): raise OSError("403")
            out = m.run(now=NOW, fetch_news_fn=boom, fetch_edgar_fn=boom, sleep=lambda s: None)
            self.assertGreater(len(out["errors"]), 0)


if __name__ == "__main__":
    unittest.main()
