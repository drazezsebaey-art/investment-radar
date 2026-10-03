"""engine/macro.py - blackout windows from the frozen macro calendar (RC-1.3 section 2)."""
import json
from datetime import datetime
from pathlib import Path

H = 3_600_000


class MacroCalendar:
    def __init__(self, path: Path):
        ev = json.loads(Path(path).read_text())["events"]
        self.times = sorted(int(datetime.fromisoformat(e["scheduled_utc"]).timestamp() * 1000) for e in ev)

    def windows(self, before_h: float, after_h: float) -> list:
        """[(start_ms, end_ms)] around every release - each playbook declares its own sizes."""
        return [(t - int(before_h * H), t + int(after_h * H)) for t in self.times]

    def any_between(self, a_ms: int, b_ms: int) -> bool:
        import bisect
        i = bisect.bisect_left(self.times, a_ms)
        return i < len(self.times) and self.times[i] <= b_ms
