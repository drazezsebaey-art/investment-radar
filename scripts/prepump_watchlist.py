"""
prepump_watchlist.py (v64) - candidates BEFORE the move, then an honest scorecard.

Combines independent evidence categories per coin (no weights - weights would
be a fitting trial; the forward test below is what earns weights later):

  A  dated catalyst      ETF pipeline news (data/etf-news.json, 14d) and active
                         buyback/burn/fee-switch governance votes (fundamentals.json)
  B  squeeze fuel        SHORT_SQUEEZE_FUEL from data/derivatives.json
  C  hidden strength     the radar's hidden relative-strength flag (radar-flags.json)
  D  revenue leading     REVENUE_ACCELERATING / HIGH_HOLDER_YIELD (fundamentals.json)
  E  sector rotation     a same-category peer already ran >= LEADER_7D_PCT while this
                         coin is still quiet (categories.json) - lowest confidence

Candidate = >= MIN_CATEGORIES distinct categories, not already up >= MAX_7D_PCT
this week, market cap >= MIN_MCAP_USD.

Forward test (the point of the whole layer): every new candidate is logged with
its price at signal time; after 7 and 14 days the result is recorded next to
the universe median over the same window. data/prepump-stats.json reports hit
rate / average and EXCESS return per category. Below 30 evaluated candidates
nothing is "meaningful" (standing rule, config/trials-log.json).
Limitation: max-gain is sampled at run times, not tick-level.
"""
import json
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # v67
import asset_filters  # noqa: E402  v67

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "data"
FLAGS, ETF, DERIV, FUND, CATS = (D / "radar-flags.json", D / "etf-news.json", D / "derivatives.json",
                                 D / "fundamentals.json", D / "categories.json")
OUT, LOG, STATS = D / "prepump-candidates.json", D / "prepump-log.json", D / "prepump-stats.json"
ENGINE_VERSION = "prepump-v67"

# thresholds (tracked in config/trials-log.json)
PREPUMP_MIN_CATEGORIES = 2
PREPUMP_MAX_7D_PCT = 30.0
PREPUMP_MIN_MCAP_USD = 50_000_000
PREPUMP_LEADER_7D_PCT = 40.0
PREPUMP_PEER_MAX_7D_PCT = 10.0
PREPUMP_HIT_PCT = 20.0            # a "hit" = max gain >= 20% within the window
ETF_LOOKBACK_DAYS = 14
RELOG_AFTER_DAYS = 14
EXCLUDE = {"bitcoin", "tether", "usd-coin", "dai", "first-digital-usd", "ethena-usde"}
RS_FLAG_MARKER = "قوة نسبية خفية"


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def okx_price(symbol):
    try:
        req = urllib.request.Request(f"https://www.okx.com/api/v5/market/ticker?instId={symbol.upper()}-USDT",
                                     headers={"User-Agent": "investment-radar/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode()).get("data") or []
        return float(data[0]["last"]) if data else None
    except Exception:  # noqa: BLE001
        return None


def collect_signals(flags, etf, deriv, fund, cats, now):
    coins = {c["id"]: c for c in flags.get("coins", []) if c.get("id")}
    sig = {}

    def add(cid, cat, text):
        if cid in coins and cid not in EXCLUDE:
            sig.setdefault(cid, {}).setdefault(cat, []).append(text)

    cutoff = now - timedelta(days=ETF_LOOKBACK_DAYS)
    for it in etf.get("items", []):
        try:
            if datetime.fromisoformat(it["detected_at"]) < cutoff:
                continue
        except (KeyError, ValueError):
            continue
        if it.get("event") not in ("other", "delay_or_rejection"):
            add(it.get("coin"), "A", f"ETF {it.get('event')}: {it.get('title', '')[:90]}")
    for g in fund.get("governance_catalysts", []):
        if g.get("coin"):
            add(g["coin"], "A", f"governance vote until {g.get('ends')}: {g.get('title', '')[:90]}")
    for cid, d in (deriv.get("coins") or {}).items():
        if "SHORT_SQUEEZE_FUEL" in (d.get("flags") or []):
            add(cid, "B", f"funding {d.get('funding_now_pct')}% with OI {d.get('oi_change_24h_pct')}% 24h")
    for cid, c in coins.items():
        if any(RS_FLAG_MARKER in f for f in c.get("flags") or []):
            add(cid, "C", "hidden relative strength during consolidation")
    for key, fl in (fund.get("flagged") or {}).items():
        for f in fl:
            if f in ("REVENUE_ACCELERATING", "HIGH_HOLDER_YIELD"):
                add(key, "D", f)
    for cat, members in (cats.get("categories") or {}).items():
        leaders = [m for m in members if (coins.get(m, {}).get("change_7d_pct") or 0) >= PREPUMP_LEADER_7D_PCT]
        if not leaders:
            continue
        for m in members:
            c7 = coins.get(m, {}).get("change_7d_pct")
            if m not in leaders and c7 is not None and c7 <= PREPUMP_PEER_MAX_7D_PCT:
                add(m, "E", f"{cat}: peer {leaders[0]} ran {coins[leaders[0]]['change_7d_pct']:.0f}% in 7d")
    return coins, sig


def candidates(coins, sig):
    out = []
    for cid, cats in sig.items():
        c = coins[cid]
        if asset_filters.exclusion_reason_for({**c, "id": cid}):  # v67/v68: no pegged / tokenized equities
            continue
        if len(cats) < PREPUMP_MIN_CATEGORIES:
            continue
        if (c.get("change_7d_pct") or 0) >= PREPUMP_MAX_7D_PCT:
            continue
        if (c.get("market_cap_usd") or 0) < PREPUMP_MIN_MCAP_USD:
            continue
        out.append({"coin": cid, "symbol": c.get("symbol"), "price_usd": c.get("price_usd"),
                    "change_7d_pct": c.get("change_7d_pct"), "market_cap_usd": c.get("market_cap_usd"),
                    "categories": sorted(cats), "n_categories": len(cats), "evidence": cats,
                    "confidence": "LOW (thesis-based, not price-confirmed)"})
    return sorted(out, key=lambda x: (-x["n_categories"], x["change_7d_pct"] or 0))


def universe_median_7d(coins):
    vals = [c.get("change_7d_pct") for c in coins.values() if c.get("change_7d_pct") is not None]
    return round(statistics.median(vals), 2) if vals else None


def update_log(log, cands, coins, now, price_fn=okx_price):
    entries = log.get("entries", [])
    recent = {e["coin"] for e in entries
              if now - datetime.fromisoformat(e["first_seen"]) < timedelta(days=RELOG_AFTER_DAYS)}
    for c in cands:
        if c["coin"] not in recent and c.get("price_usd"):
            entries.append({"coin": c["coin"], "symbol": c["symbol"], "first_seen": now.isoformat(),
                            "price_at_signal": c["price_usd"], "categories": c["categories"],
                            "max_price_seen": c["price_usd"], "checkpoints": {}})
    lookups = 0
    for e in entries:
        age = now - datetime.fromisoformat(e["first_seen"])
        if age > timedelta(days=15) and len(e["checkpoints"]) == 2:
            continue
        price = coins.get(e["coin"], {}).get("price_usd")
        if price is None and lookups < 10:
            price, lookups = price_fn(e.get("symbol") or ""), lookups + 1
            e["out_of_universe"] = True
        if price is None:
            continue
        e["max_price_seen"] = max(e.get("max_price_seen") or price, price)
        for label, days in (("d7", 7), ("d14", 14)):
            if label not in e["checkpoints"] and age >= timedelta(days=days):
                p0 = e["price_at_signal"]
                e["checkpoints"][label] = {
                    "at": now.isoformat(), "price": price,
                    "return_pct": round((price / p0 - 1) * 100, 2),
                    "max_gain_pct": round((e["max_price_seen"] / p0 - 1) * 100, 2),
                    "universe_median_7d_pct": universe_median_7d(coins)}
    log["entries"] = entries
    return log


def stats(log):
    out = {}
    for label in ("d7", "d14"):
        done = [e for e in log.get("entries", []) if label in e["checkpoints"]]
        groups = {"ALL": done}
        for e in done:
            for c in e["categories"]:
                groups.setdefault(f"cat_{c}", []).append(e)
        res = {}
        for g, es in groups.items():
            r = [e["checkpoints"][label]["return_pct"] for e in es]
            hits = [e for e in es if e["checkpoints"][label]["max_gain_pct"] >= PREPUMP_HIT_PCT]
            excess = [e["checkpoints"][label]["return_pct"] - (e["checkpoints"][label]["universe_median_7d_pct"] or 0)
                      for e in es]
            res[g] = {"n": len(es), "hit_rate_pct": round(len(hits) / len(es) * 100, 1) if es else None,
                      "avg_return_pct": round(sum(r) / len(r), 2) if r else None,
                      "avg_excess_vs_universe_pct": round(sum(excess) / len(excess), 2) if excess else None,
                      "meaningful": len(es) >= 30}
        out[label] = res
    return out


def run(now=None, price_fn=okx_price):
    now = now or now_utc()
    coins, sig = collect_signals(load(FLAGS, {}), load(ETF, {}), load(DERIV, {}), load(FUND, {}), load(CATS, {}), now)
    cands = candidates(coins, sig)
    OUT.write_text(json.dumps({"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION,
                               "candidates": cands,
                               "signals_by_coin": {k: sorted(v) for k, v in sig.items()}},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    log = update_log(load(LOG, {"entries": []}), cands, coins, now, price_fn)
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    st = {"updated_at": now.isoformat(), "hit_definition": f"max gain >= {PREPUMP_HIT_PCT}% inside the window",
          **stats(log)}
    STATS.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Pre-pump: {len(cands)} candidate(s); log {len(log['entries'])} entries; "
          f"d7 evaluated {st['d7'].get('ALL', {}).get('n', 0)}")
    for c in cands[:10]:
        print(f"  - {c['coin']} [{'+'.join(c['categories'])}] 7d={c['change_7d_pct']}%")
    return cands


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"prepump_watchlist failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
