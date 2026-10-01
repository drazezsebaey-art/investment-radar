"""
digest.py (v66) - ONE small file (data/digest.md, target < 4 KB) that tells the
analyst everything the radar knows, so a chat session reads 1 file instead of
10+ large JSON files. Pure reader: no network. Missing inputs are listed, never
guessed. Runs last in the workflow (if: always()).
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "data"
OUT = D / "digest.md"
MAX_LINES_PER_SECTION = 8


def load(name):
    try:
        return json.loads((D / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def is_tradeable_key(key: str) -> bool:
    """v67: a fundamentals.json 'flagged' key is a CoinGecko id when the protocol
    maps to a token (lower-case, no spaces, no 'protocol:' prefix), otherwise the
    raw protocol name. Pegged assets (e.g. ripple-usd) are not tradeable either."""
    if not key or " " in key or key.startswith("protocol:") or key != key.lower():
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import asset_filters
        return asset_filters.exclusion_reason(key, None) is None
    except Exception:  # noqa: BLE001
        return True


def fmt(x, nd=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}" if abs(x) < 1e6 else f"{x/1e6:.1f}M"
    return str(x)


def age(ts, now):
    try:
        h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
        return f"{h:.0f}h" if h >= 1 else f"{h*60:.0f}m"
    except Exception:
        return "?"


def build(now=None):
    now = now or datetime.now(timezone.utc)
    L, missing = [f"# Radar digest - {now.strftime('%Y-%m-%d %H:%M')} UTC", ""], []

    # --- market regime -------------------------------------------------------
    cm, al, mg = load("correction-monitor.json"), load("altseason.json"), load("macro-gold.json")
    L.append("## Market")
    if cm:
        b = cm.get("btc_q4_scenario") or {}
        L.append(f"- BTC scenario **{b.get('scenario')}** | price {fmt(b.get('price'),0)} | wk close "
                 f"{fmt(b.get('last_weekly_close'),0)} vs {fmt(b.get('key_level'),0)} | 50W {fmt(b.get('sma_50w'),0)} "
                 f"({fmt(b.get('dist_to_50w_pct'))}%) | {age(cm.get('updated_at'), now)} old")
    else:
        missing.append("correction-monitor")
    if al:
        m = al.get("metrics", {})
        L.append(f"- Alts: **{al.get('regime')}** [{', '.join(al.get('signals') or []) or '-'}] | BTC.D "
                 f"{fmt(m.get('btc_dominance_pct'))}% | ETH/BTC {fmt(m.get('eth_btc'),5)} | breadth7d "
                 f"{fmt(m.get('breadth_7d_pct'),0)}% | stables 30d {fmt(m.get('stablecoin_30d_pct'))}%")
        ar = al.get("alt_risk") or {}  # v68
        if ar:
            L.append(f"- Alt risk (BTC.D): **{ar.get('level')}** {ar.get('flags') or ''} | 3d {fmt(ar.get('dom_3d_change_pt'))}pt "
                     f"| 7d {fmt(ar.get('dom_7d_change_pt'))}pt | n={ar.get('n_history')}")
    else:
        missing.append("altseason")
    if mg:
        ry = (mg.get("macro") or {}).get("real_yield_10y_pct", {})
        usd = (mg.get("macro") or {}).get("usd_broad_index", {})
        cot = mg.get("cot_gold_managed_money") or {}
        p6 = mg.get("pillar6_check") or {}
        L.append(f"- Gold: PAXG 1m {fmt((mg.get('gold_paxg') or {}).get('chg_1m'))}% | real10y {fmt(ry.get('value'))} "
                 f"({fmt(ry.get('chg_1m'),0)}bp 1m) | USD 1m {fmt(usd.get('chg_1m'))}% | pillar6 **{p6.get('status')}** "
                 f"{p6.get('flags') or ''} | COT pctl {fmt(cot.get('mm_net_percentile_3y'),0)}")
        yd = (mg.get("yield_decomposition") or {})  # v68
        for w, lab in (("chg_1w", "1w"), ("chg_1m", "1m")):
            d = yd.get(w) or {}
            if d.get("driver") and d["driver"] != "insufficient_data":
                L.append(f"- 10y {lab}: {fmt(d.get('nominal_bp'),0)}bp = real {fmt(d.get('real_bp'),0)} + breakeven "
                         f"{fmt(d.get('breakeven_bp'),0)} -> **{d['driver']}**")
    else:
        missing.append("macro-gold")
    L.append("")

    # --- actionable ----------------------------------------------------------
    L.append("## Coins in correction (entry_ready first)")
    if cm and cm.get("coins_in_correction"):
        rows = sorted(cm["coins_in_correction"], key=lambda r: (not r.get("entry_ready"), r["status"]))
        for r in rows[:MAX_LINES_PER_SECTION]:
            lv = r.get("levels") or {}
            ob = r.get("nearest_bull_ob_below") or {}
            L.append(f"- {r['symbol']}: **{r['status']}**{' ENTRY_READY' if r.get('entry_ready') else ''} | "
                     f"+{fmt(r.get('impulse_gain_pct'),0)}% impulse, retr {fmt(r.get('retracement_now'))} | OI dd "
                     f"{fmt(r.get('oi_drawdown_pct'),0)}% | fund {fmt(r.get('funding_now_pct'),4)} | hold "
                     f"{lv.get('bullish_hold_signal')} | inval {fmt(lv.get('long_invalidation_level'),5)} | OB "
                     f"{fmt(ob.get('bottom'),5)}-{fmt(ob.get('top'),5)}")
    else:
        L.append("- none")
    L.append("")

    pp = load("prepump-candidates.json")
    L.append("## Pre-pump candidates")
    if pp and pp.get("candidates"):
        for c in pp["candidates"][:MAX_LINES_PER_SECTION]:
            L.append(f"- {c['symbol']}: [{'+'.join(c['categories'])}] 7d {fmt(c.get('change_7d_pct'),1)}%")
    else:
        L.append("- none" if pp else "- (file missing)")
    L.append("")

    et = load("etf-news.json")
    L.append("## ETF pipeline (new this run)")
    if et and et.get("alerts_this_run"):
        for i in et["alerts_this_run"][:MAX_LINES_PER_SECTION]:
            L.append(f"- {i['coin']}{' NEW' if i.get('new_coin_discovered') else ''}: {i['event']} - {i['title'][:90]}")
    else:
        L.append("- none")
    L.append("")

    dv = load("derivatives.json")
    L.append("## Derivatives flags (OKX)")
    if dv and (dv.get("summary") or {}).get("flagged"):
        for k, v in list(dv["summary"]["flagged"].items())[:MAX_LINES_PER_SECTION]:
            c = dv["coins"].get(k, {})
            L.append(f"- {c.get('symbol', k)}: {', '.join(v)} | OI/mc {fmt(c.get('oi_to_mcap_pct'))}% | "
                     f"topPos {fmt(c.get('top_trader_position_ratio'))} | taker {fmt(c.get('taker_buy_sell_24h'))}")
    else:
        L.append("- none" if dv else "- (file missing)")
    L.append("")

    fu = load("fundamentals.json")
    L.append("## Revenue / buyback flags")
    if fu and fu.get("flagged"):
        # v67: only protocols that map to a real, non-pegged token are actionable;
        # entities without a token (wallets, builders) are counted, not listed
        tradeable = {k: v for k, v in fu["flagged"].items() if is_tradeable_key(k)}
        for k, v in list(tradeable.items())[:6]:
            L.append(f"- {k}: {', '.join(v)}")
        hidden = len(fu["flagged"]) - len(tradeable)
        if hidden:
            L.append(f"- ({hidden} flagged protocol(s) without a tradeable token hidden)")
        for g in [g for g in (fu.get("governance_catalysts") or []) if g.get("coin")][:3]:
            L.append(f"- vote: {g.get('coin')} ({g.get('space')}) until {g.get('ends')} - {g.get('title', '')[:70]}")
    else:
        L.append("- none" if fu else "- (file missing)")
    L.append("")

    # --- v68: paper book mark-to-market (observability only) -------------------
    L.append("## Paper book (open trades, marked to last scan)")
    try:
        trades = json.loads((ROOT / "config" / "trades.json").read_text(encoding="utf-8")).get("trades", [])
        ms = load("market-scan.json") or {}
        px = {c.get("id"): c.get("price_usd") for c in ms.get("coins", [])}
        rows = []
        for t in trades:
            if t.get("status") != "open":
                continue
            e, p, st = t.get("actual_entry") or t.get("entry"), px.get(t.get("asset_id")), t.get("stop")
            if e and p and st and e > st:
                rows.append(((p / e - 1) * 100, (p - e) / (e - st), t.get("date_opened") or "", t.get("symbol") or t.get("asset_id")))
        if rows:
            rows.sort(key=lambda r: r[1])  # by R multiple
            old_n = sum(1 for r in rows if r[2] and (now.date() - datetime.fromisoformat(r[2]).date()).days > 7)
            L.append(f"- open {len(rows)} | mean {sum(r[0] for r in rows)/len(rows):.1f}% ({sum(r[1] for r in rows)/len(rows):.2f}R) "
                     f"| in profit {sum(1 for r in rows if r[0] > 0)} | older than 7d {old_n}")
            L.append(f"- worst: {', '.join(f'{r[3]} {r[1]:.2f}R' for r in rows[:3])} | best: "
                     f"{', '.join(f'{r[3]} {r[1]:.2f}R' for r in rows[-3:])}")
        else:
            L.append("- none")
    except Exception as e:  # noqa: BLE001
        L.append(f"- unavailable ({e})")
    L.append("")

    # --- system health -------------------------------------------------------
    L.append("## System")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cg_budget
        s = cg_budget.status(now)
        L.append(f"- CoinGecko {s['used']}/{s['limit']} used, month-end projection {s['projection']} "
                 f"({fmt(s['projection_pct'],0)}%), throttle level {s['throttle_level']} | by script {s['by_script']}")
    except Exception as e:  # noqa: BLE001
        L.append(f"- CoinGecko budget: unavailable ({e})")
    tr, hy = load("trials-report.json"), load("hypotheses-report.json")
    if tr:
        L.append(f"- Trials guard: {tr.get('status')} ({len(tr.get('untracked_parameter_changes') or [])} untracked)")
    if hy:
        L.append("- Hypotheses: " + ", ".join(f"{k} {v['verdict']} (n={v['forward_test_arm'].get('n', 0)})"
                                           for k, v in (hy.get("hypotheses") or {}).items()))
    rf = load("radar-flags.json")
    if rf:
        L.append(f"- radar-flags.json scan age: {age(rf.get('updated_at'), now)}")
        wu = rf.get("warmup") or {}
        if wu.get("active"):
            L.append(f"- WARM-UP active until {wu.get('warmup_until')} after a {wu.get('gap_hours')}h gap: "
                     f"Layer-2 early signals recorded, not flagged")
        if rf.get("score_streak_reset"):
            r = rf["score_streak_reset"]
            L.append(f"- score streaks reset {r.get('reset_at')} after {r.get('gap_hours')}h gap "
                     f"({r.get('streaks_cleared')} cleared)")
        if rf.get("excluded_count"):
            L.append(f"- excluded from radar (pegged/tokenized equity): {rf['excluded_count']}")
    if missing:
        L.append(f"- missing inputs: {', '.join(missing)}")
    return "\n".join(L) + "\n"


def run(now=None, out=OUT):
    text = build(now)
    Path(out).write_text(text, encoding="utf-8")
    print(f"Digest written ({len(text.encode('utf-8'))} bytes).")
    return text


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"digest failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
