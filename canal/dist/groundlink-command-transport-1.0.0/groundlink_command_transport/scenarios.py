"""Closed scenario vocabulary used only for experiment correlation and display."""

from enum import Enum


class Scenario(str, Enum):
    UNLABELED = "unlabeled"
    BASELINE = "baseline"
    AWGN_20DB = "awgn_20db"
    AWGN_0DB = "awgn_0db"
    REPLAY = "replay"
    SPOOFING = "spoofing"
    JAMMING = "jamming"
    MALFORMED = "malformed"


SCENARIO_ALLOWLIST = frozenset(item.value for item in Scenario)


def validate_scenario(value: object) -> str:
    if not isinstance(value, str) or value not in SCENARIO_ALLOWLIST:
        raise ValueError("scenario must be in the fixed GroundLink allowlist")
    return value
