"""
cg_budget.py (v66) - CoinGecko credit ledger + automatic throttle.

The only hard monthly limit in the whole system is the CoinGecko Demo credit
allowance (GitHub Actions minutes are unlimited on this public repo). This
module counts every CoinGecko request made by any script, projects the month
end, and tells non-essential scripts to back off before the allowance runs out.

  record(script, n=1)   in-memory count, flushed to data/cg-usage.json at exit
  throttle_level()      0 = normal, 1 = projection > WARN, 2 = projection > LIMIT
                        (or 97% of the allowance already used)
  status()              dict for the digest

scan.py is ESSENTIAL and is never throttled; breakout_check, counterfactual_check
and check_liquidity consult throttle_level(). The allowance lives in
config/cg-budget.json (monthly_limit) - set it to the number on your CoinGecko
developer dashboard.
"""
import atexit
import calendar
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ROOT / "data" / "cg-usage.json"
CONFIG = ROOT / "config" / "cg-budget.json"
DEFAULT_LIMIT = 10000
WARN_FRACTION = 0.90

_pending = {}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def monthly_limit():
    return int(_load(CONFIG, {}).get("monthly_limit", DEFAULT_LIMIT))


def record(script: str, n: int = 1):
    _pending[script] = _pending.get(script, 0) + n


def _month_key(now):
    return now.strftime("%Y-%m")


def flush(now=None, usage_path=USAGE):
    if not _pending:
        return
    now = now or datetime.now(timezone.utc)
    u = _load(usage_path, {})
    mk = _month_key(now)
    if u.get("month") != mk:
        u = {"month": mk, "total": 0, "by_script": {}, "by_day": {}, "history": u.get("history", [])[-11:]
             + ([{"month": u["month"], "total": u.get("total", 0)}] if u.get("month") else [])}
    day = now.strftime("%Y-%m-%d")
    for s, n in _pending.items():
        u["total"] = u.get("total", 0) + n
        u["by_script"][s] = u["by_script"].get(s, 0) + n
        u["by_day"][day] = u["by_day"].get(day, 0) + n
    u["updated_at"] = now.isoformat()
    Path(usage_path).write_text(json.dumps(u, indent=2), encoding="utf-8")
    _pending.clear()


atexit.register(flush)


def status(now=None, usage_path=USAGE):
    now = now or datetime.now(timezone.utc)
    u = _load(usage_path, {})
    limit = monthly_limit()
    if u.get("month") != _month_key(now):
        used = 0
    else:
        used = u.get("total", 0)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed = max(now.day - 1 + now.hour / 24, 0.5)
    projection = round(used / elapsed * days_in_month)
    level = 0
    if projection > limit or used >= 0.97 * limit:
        level = 2
    elif projection > WARN_FRACTION * limit:
        level = 1
    return {"month": _month_key(now), "used": used, "limit": limit, "projection": projection,
            "projection_pct": round(projection / limit * 100, 1) if limit else None,
            "throttle_level": level, "by_script": (u.get("by_script") if u.get("month") == _month_key(now) else {})}


def throttle_level(now=None, usage_path=USAGE):
    return status(now, usage_path)["throttle_level"]
