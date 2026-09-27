"""
etf_news_check.py (v61) - ETF pipeline monitoring layer.

Standing rule (27/9/2026): tracking coins that may get an ETF is a core,
always-on part of crypto analysis. Any coin showing new ETF-preparation news
(filing, S-1/amendment, exchange listing application, approval, launch date,
trust conversion) or an institution publicly backing one must be flagged.

Sources (both keyless, NOT CoinGecko - zero impact on the CoinGecko quota):
  1. SEC EDGAR full-text search (efts.sec.gov) - primary legal filings:
     S-1, S-1/A, 8-A12B (exchange listing registration), 424B3/424B4
     (final prospectus = product ready to trade).
  2. Google News RSS - coverage of approvals/launches/institutional backing
     that do not show up as an EDGAR form (e.g. exchange 19b-4 decisions).

Discovery: generic ETF-filing queries are matched against the whole scanned
universe (data/radar-flags.json coins), so a coin NOT yet in
config/etf-watch.json still gets flagged as `new_coin_discovered`.

Outputs:
  data/etf-news.json        rolling 30-day log + alerts_this_run + CME clocks
  data/etf-news-state.json  seen ids + last_run (gate state)

Gate: runs at most once every ETF_GATE_HOURS (state-based, not hour%N,
because the external cron is irregular). Never fails the workflow.
"""
import json
import hashlib
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH_FILE = ROOT / "config" / "etf-watch.json"
FLAGS_FILE = ROOT / "data" / "radar-flags.json"
OUT_FILE = ROOT / "data" / "etf-news.json"
STATE_FILE = ROOT / "data" / "etf-news-state.json"

ENGINE_VERSION = "etf_news-v61"
ETF_GATE_HOURS = 2
NEWS_LOOKBACK_HOURS = 72
EDGAR_LOOKBACK_DAYS = 10
ROLLING_DAYS = 30
MAX_SEEN_IDS = 3000
REQUEST_PAUSE_SEC = 0.6
# SEC requires a descriptive User-Agent with contact info (fair-access policy).
UA = "investment-radar etf-monitor (drazezsebaey-art@users.noreply.github.com)"

EDGAR_FORMS = "S-1,S-1/A,8-A12B,424B3,424B4"

GENERIC_QUERIES = [
    "spot crypto ETF filing",
    "crypto ETF S-1 filed",
    "Grayscale files ETF",
    "Bitwise files ETF",
    "VanEck crypto ETF filing",
    "21Shares ETF filing",
    "Canary Capital ETF filing",
    "crypto ETF approval NYSE Arca OR Nasdaq OR Cboe",
    "crypto trust conversion ETF",
]

ETF_WORDS = re.compile(r"\b(ETF|ETP|exchange[- ]traded|trust)\b", re.I)
EVENT_PATTERNS = [
    ("launch", re.compile(r"\b(launch|launches|debut|begins trading|starts trading|first day of trading|goes live)\b", re.I)),
    ("approval", re.compile(r"\b(approv\w*|clear(s|ed)? to list|greenlight\w*|effective)\b", re.I)),
    ("conversion", re.compile(r"\b(convert\w*|conversion|uplist\w*)\b", re.I)),
    ("amendment", re.compile(r"\b(amend\w*|S-1/A|revised)\b", re.I)),
    ("filing", re.compile(r"\b(file[sd]?|filing|S-1|19b-4|8-A|registration|applies|application|submit\w*)\b", re.I)),
    ("delay_or_rejection", re.compile(r"\b(delay\w*|postpone\w*|extend\w* (the )?(review|deadline)|reject\w*|den(y|ies|ied)|withdraw\w*)\b", re.I)),
    ("institutional_backing", re.compile(r"\b(backs|backing|seed|stake in|partners? with|custod\w*|sponsor)\b", re.I)),
]
PRIORITY = {"launch": 1, "approval": 2, "conversion": 3, "delay_or_rejection": 4,
            "amendment": 5, "filing": 6, "institutional_backing": 7, "other": 9}
# Too-generic coin names/symbols that would false-match ordinary English words.
AMBIGUOUS_TOKENS = {"ONE", "NEAR", "SUN", "GAS", "KEY", "HOT", "BOND", "PEOPLE", "TRUST",
                    "OPEN", "SAFE", "REAL", "GOLD", "PRIME", "WIN", "ACT", "AI", "ZK", "CAT",
                    "ETF", "USD", "FUN", "JUST", "MOVE", "SIGN", "BANK", "MEME", "ANIME"}


def now_utc():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def http_get(url, accept="*/*", timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def item_id(*parts):
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:16]


def classify(text):
    hits = [name for name, pat in EVENT_PATTERNS if pat.search(text or "")]
    if not hits:
        return "other"
    return sorted(hits, key=lambda h: PRIORITY[h])[0]


# ---------------------------------------------------------------- gate ----
def gate_open(state, now=None):
    now = now or now_utc()
    last = state.get("last_run")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt) >= timedelta(hours=ETF_GATE_HOURS) - timedelta(minutes=5)


# ------------------------------------------------------------ parsing ----
def parse_news_rss(xml_bytes, lookback_hours=NEWS_LOOKBACK_HOURS, now=None):
    now = now or now_utc()
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src_el = it.find("source")
        source = src_el.text.strip() if src_el is not None and src_el.text else ""
        try:
            pub_dt = parsedate_to_datetime(pub) if pub else None
            if pub_dt and pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pub_dt = None
        if pub_dt and (now - pub_dt) > timedelta(hours=lookback_hours):
            continue
        out.append({"title": title, "link": link, "source": source,
                    "published": pub_dt.isoformat() if pub_dt else None})
    return out


def parse_edgar_json(raw):
    out = []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return out
    for h in (data.get("hits", {}) or {}).get("hits", []) or []:
        src = h.get("_source", {}) or {}
        adsh = src.get("adsh") or (h.get("_id", "").split(":")[0])
        names = src.get("display_names") or []
        ciks = src.get("ciks") or []
        cik = (ciks[0].lstrip("0") if ciks else "")
        link = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/"
                if cik and adsh else "https://efts.sec.gov/LATEST/search-index")
        out.append({"adsh": adsh, "form": src.get("form") or src.get("file_type") or "",
                    "file_date": src.get("file_date"), "filer": "; ".join(names), "link": link})
    return out


# ------------------------------------------------------------ fetching ----
def fetch_news(query):
    q = urllib.parse.quote(f"{query} when:3d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return parse_news_rss(http_get(url, accept="application/rss+xml"))


def fetch_edgar(phrase, now=None):
    now = now or now_utc()
    params = urllib.parse.urlencode({
        "q": f'"{phrase}"',
        "forms": EDGAR_FORMS,
        "dateRange": "custom",
        "startdt": (now - timedelta(days=EDGAR_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
        "enddt": now.strftime("%Y-%m-%d"),
    })
    return parse_edgar_json(http_get(f"https://efts.sec.gov/LATEST/search-index?{params}",
                                     accept="application/json"))


# ------------------------------------------------------------ matching ----
def build_universe(flags, watch):
    """name/symbol -> coin id, from the scanned universe + the watch config."""
    idx = {}
    for c in (flags.get("coins") or []):
        for tok in (c.get("name"), c.get("symbol")):
            if tok and len(tok) >= 3 and tok.upper() not in AMBIGUOUS_TOKENS:
                idx[tok.lower()] = c.get("id")
    for cid, w in watch.get("coins", {}).items():
        for tok in w.get("match_terms", []):
            idx[tok.lower()] = cid
    return idx


def match_coins(title, universe):
    found = set()
    for tok, cid in universe.items():
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", title, re.I):
            found.add(cid)
    return found


def cme_clocks(watch, now=None):
    """Generic listing standards: >=6 months of CFTC-regulated futures -> fast-track eligible."""
    now = now or now_utc()
    rows = []
    for cid, w in watch.get("coins", {}).items():
        since = w.get("cme_futures_since")
        if not since:
            continue
        try:
            start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        eligible = start + timedelta(days=183)
        rows.append({"coin": cid, "cme_futures_since": since,
                     "fast_track_eligible_from": eligible.date().isoformat(),
                     "eligible_now": now >= eligible,
                     "days_left": max(0, (eligible - now).days)})
    return sorted(rows, key=lambda r: r["days_left"])


# ---------------------------------------------------------------- main ----
def run(now=None, fetch_news_fn=fetch_news, fetch_edgar_fn=fetch_edgar, sleep=time.sleep):
    now = now or now_utc()
    watch = load_json(WATCH_FILE, {"coins": {}})
    flags = load_json(FLAGS_FILE, {})
    state = load_json(STATE_FILE, {"seen": [], "last_run": None})
    prev = load_json(OUT_FILE, {"items": []})

    if not gate_open(state, now):
        print(f"ETF gate closed (runs every {ETF_GATE_HOURS}h). Last run: {state.get('last_run')}")
        return None

    seen = set(state.get("seen", []))
    universe = build_universe(flags, watch)
    watched = set(watch.get("coins", {}).keys())
    new_items, errors = [], []

    def add(item):
        if item["id"] in seen:
            return
        seen.add(item["id"])
        new_items.append(item)

    # 1) EDGAR: primary legal filings per watched coin
    for cid, w in watch.get("coins", {}).items():
        for phrase in w.get("edgar_phrases", []):
            try:
                for f in fetch_edgar_fn(phrase, now):
                    add({"id": item_id("edgar", f["adsh"]), "coin": cid, "source_type": "sec_edgar",
                         "event": "amendment" if "/A" in f["form"] else ("launch" if f["form"].startswith("424B")
                                  else ("approval" if f["form"].startswith("8-A") else "filing")),
                         "title": f"{f['form']} - {f['filer']}", "source": "SEC EDGAR",
                         "link": f["link"], "published": f["file_date"], "new_coin_discovered": False})
            except Exception as e:  # noqa: BLE001 - observability only
                errors.append(f"edgar[{phrase}]: {e}")
            sleep(REQUEST_PAUSE_SEC)

    # 2) News per watched coin
    for cid, w in watch.get("coins", {}).items():
        for q in w.get("news_queries", []):
            try:
                for n in fetch_news_fn(q):
                    if not ETF_WORDS.search(n["title"]):
                        continue
                    # search engines return loosely related stories: attribute to this coin only
                    # if the headline actually names it; otherwise let universe matching decide.
                    terms = {t.lower(): cid for t in w.get("match_terms", [])}
                    if not match_coins(n["title"], terms):
                        for other in match_coins(n["title"], universe) - {"bitcoin", "ethereum", cid}:
                            add({"id": item_id("news", n["link"] or n["title"], other), "coin": other,
                                 "source_type": "news_discovery", "event": classify(n["title"]),
                                 "title": n["title"], "source": n["source"], "link": n["link"],
                                 "published": n["published"], "new_coin_discovered": other not in watched})
                        continue
                    add({"id": item_id("news", n["link"] or n["title"]), "coin": cid, "source_type": "news",
                         "event": classify(n["title"]), "title": n["title"], "source": n["source"],
                         "link": n["link"], "published": n["published"], "new_coin_discovered": False})
            except Exception as e:  # noqa: BLE001
                errors.append(f"news[{q}]: {e}")
            sleep(REQUEST_PAUSE_SEC)

    # 3) Discovery: generic queries matched against the whole universe
    for q in GENERIC_QUERIES:
        try:
            for n in fetch_news_fn(q):
                if not ETF_WORDS.search(n["title"]):
                    continue
                coins = match_coins(n["title"], universe) - {"bitcoin", "ethereum"}
                for cid in coins:
                    add({"id": item_id("news", n["link"] or n["title"], cid), "coin": cid,
                         "source_type": "news_discovery", "event": classify(n["title"]),
                         "title": n["title"], "source": n["source"], "link": n["link"],
                         "published": n["published"], "new_coin_discovered": cid not in watched})
        except Exception as e:  # noqa: BLE001
            errors.append(f"discovery[{q}]: {e}")
        sleep(REQUEST_PAUSE_SEC)

    for it in new_items:
        it["detected_at"] = now.isoformat()
        it["engine_version"] = ENGINE_VERSION
    new_items.sort(key=lambda x: (not x["new_coin_discovered"], PRIORITY.get(x["event"], 9)))

    cutoff = now - timedelta(days=ROLLING_DAYS)
    kept = [i for i in prev.get("items", [])
            if i.get("detected_at") and datetime.fromisoformat(i["detected_at"]) >= cutoff]
    items = new_items + kept

    by_coin = {}
    for i in items:
        b = by_coin.setdefault(i["coin"], {"count_30d": 0, "latest_event": None, "latest_title": None})
        b["count_30d"] += 1
        if b["latest_event"] is None:
            b["latest_event"], b["latest_title"] = i["event"], i["title"]

    out = {
        "updated_at": now.isoformat(),
        "engine_version": ENGINE_VERSION,
        "alert": bool(new_items),
        "alerts_this_run": new_items,
        "new_coins_discovered": sorted({i["coin"] for i in new_items if i["new_coin_discovered"]}),
        "by_coin_30d": by_coin,
        "cme_eligibility_clock": cme_clocks(watch, now),
        "manual_status": {cid: w.get("status_note") for cid, w in watch.get("coins", {}).items()},
        "errors": errors,
        "items": items,
    }
    save_json(OUT_FILE, out)
    save_json(STATE_FILE, {"seen": list(seen)[-MAX_SEEN_IDS:], "last_run": now.isoformat()})

    print(f"ETF check: {len(new_items)} new item(s), {len(out['new_coins_discovered'])} new coin(s), "
          f"{len(errors)} error(s)")
    for i in new_items[:25]:
        tag = " [NEW COIN]" if i["new_coin_discovered"] else ""
        print(f"  - {i['coin']}{tag} | {i['event']} | {i['title'][:120]}")
    for e in errors[:10]:
        print(f"  ! {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"etf_news_check failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
