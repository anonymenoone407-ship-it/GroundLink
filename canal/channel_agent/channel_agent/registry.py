"""Closed v1 scenario-to-process mapping; contains no user-controlled argv."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional


@dataclass(frozen=True)
class ScenarioBinding:
    script: Optional[str]
    arguments: tuple[str, ...]
    attack: Optional[str]
    termination: str
    five_frame_behavior: str


_REGISTRY = {
    "unlabeled": ScenarioBinding("channel_rf.py", ("--mode", "ideal"), None,
                                  "runs until SIGINT/error", "unbounded"),
    "baseline": ScenarioBinding("channel_rf.py", ("--mode", "ideal"), None,
                                 "runs until SIGINT/error", "unbounded"),
    "awgn_20db": ScenarioBinding("channel_rf.py", ("--mode", "awgn", "--snr-db", "20"), "AWGN",
                                  "runs until SIGINT/error", "unbounded"),
    "awgn_0db": ScenarioBinding("channel_rf.py", ("--mode", "awgn", "--snr-db", "0"), "AWGN",
                                 "runs until SIGINT/error", "unbounded"),
    "jamming": ScenarioBinding("channel_rf.py", ("--mode", "interference"), "JAMMING",
                                "runs until SIGINT/error", "unbounded"),
    "replay": ScenarioBinding("channel_replay.py", (), "REPLAY",
                               "exits after five received blocks or timeout/error", "block 1 replaces block 3"),
    "spoofing": ScenarioBinding("channel_spoof.py", (), "SPOOFING",
                                 "exits after five received blocks or uncaught error", "block 3 is replaced"),
    "malformed": ScenarioBinding("channel_tamper.py", (), "MALFORMED",
                                  "runs until SIGINT; per-frame validation/send errors are logged", "sequence 3 is rewritten"),
}

SCENARIO_REGISTRY = MappingProxyType(_REGISTRY)
