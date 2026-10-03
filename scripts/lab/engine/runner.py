"""
engine/runner.py - experiment runner skeleton and the out-of-sample seal (RC-1.3
sections 8 and 14). Playbooks plug in through `Playbook` (step 5).
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UNSEAL = ROOT / "data" / "lab" / "oos-unseal.json"
FROZEN = {"T1": "T1-v1.2", "T2": "T2-v1.1", "T3": "T3-v1.1", "T4": "T4-v1.1"}


class SealedSegment(PermissionError):
    pass


def segment_bounds(config: dict, segment: str, trader: str = None) -> tuple:
    s = config["splits"]
    if segment == "development":
        a, b = s["development"]
        if trader == "T4":
            a = "2021-12-01"                       # RC-1.3: metrics coverage starts Dec 2021
        return a, b
    if segment == "validation":
        return tuple(s["validation"])
    if segment == "out_of_sample":
        assert_unsealed()
        return tuple(s["out_of_sample"])
    raise ValueError(segment)


def assert_unsealed():
    """OOS opens once, for all four together, only after every playbook is frozen
    and validated, with an explicit approval file AND an explicit env switch."""
    if os.environ.get("LAB_UNSEAL_OOS") != "yes" or not UNSEAL.exists():
        raise SealedSegment("out-of-sample is sealed (RC section 8)")
    u = json.loads(UNSEAL.read_text())
    if u.get("playbooks") != FROZEN or not u.get("approved_by"):
        raise SealedSegment("unseal file does not match the frozen playbook versions")


class Playbook:
    """Interface every trader implements (step 5)."""
    trader_id = "T?"
    version = "?"
    signal_tf_ms = 14_400_000

    def evaluate(self, t_ms: int, ctx: dict) -> list:
        """-> list of signal dicts in the RC section-10 schema (decision LONG/WAIT/
        ABSTAIN/INVALID/DATA_ERROR + an execution Spec for LONG)."""
        raise NotImplementedError
