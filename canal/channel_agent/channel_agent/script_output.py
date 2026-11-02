"""Strict adapters for action-bearing lines printed by the existing scripts.

Frame IDs are supplied by the future supervised receive-side correlator because the legacy
scripts do not print them. Lines that do not prove an action return None.
"""

import re
from typing import Optional

from .registry import SCENARIO_REGISTRY
from .telemetry import ExecutionObservation

_RF_TX = re.compile(r"TX #\d+ accepté par ZeroMQ \| (?P<bytes>\d+) octets \| mode=(?P<mode>\w+)")
_RF_DROP = re.compile(r"DROP #\d+ : bloc volontairement supprimé")
_REPLAY_TX = re.compile(r"\[TX\] Bloc #\d+ \| status=(?P<status>INTACT|REPLAY) \| (?P<bytes>\d+) octets")
_SPOOF_TX = re.compile(r"\[TX\] bloc=\d+ \| seq=.*? \| (?P<status>INTACT|SPOOFED) \| (?P<bytes>\d+) octets")
_TAMPER_TX = re.compile(r"\[TX\] seq=.*? \| (?P<status>INTACTE|MODIFIÉE) \| (?P<bytes>\d+) octets")

_RF_MODE = {
    "unlabeled": "ideal", "baseline": "ideal", "awgn_20db": "awgn",
    "awgn_0db": "awgn", "jamming": "interference",
}


class ScriptOutputAdapter:
    """Convert only a successful TX or explicit DROP line into confirmed evidence."""

    def adapt_line(self, *, line: str, run_id: str, frame_id: str, event_id: str,
                   event_time: str, scenario: str, received_iq_bytes: Optional[int] = None
                   ) -> Optional[ExecutionObservation]:
        if scenario not in SCENARIO_REGISTRY:
            raise ValueError("scenario is not in the Channel registry")
        common = dict(run_id=run_id, frame_id=frame_id, event_id=event_id,
                      event_time=event_time, scenario=scenario, confirmed=True)

        match = _RF_TX.search(line)
        if match and scenario in _RF_MODE:
            if match.group("mode") != _RF_MODE[scenario]:
                raise ValueError("script mode does not match prepared scenario")
            attack = SCENARIO_REGISTRY[scenario].attack
            disposition = "ATTACK" if attack else "FORWARD"
            return ExecutionObservation(**common, iq_bytes=int(match.group("bytes")),
                                        disposition=disposition, attack=attack,
                                        detail="successful ZeroMQ TX observed")
        if _RF_DROP.search(line) and scenario in _RF_MODE:
            if received_iq_bytes is None:
                raise ValueError("DROP observation requires correlated received IQ size")
            return ExecutionObservation(**common, iq_bytes=received_iq_bytes,
                                        disposition="DROP", detail="explicit script DROP observed")

        match = _REPLAY_TX.search(line)
        if match and scenario == "replay":
            attacked = match.group("status") == "REPLAY"
            return ExecutionObservation(**common, iq_bytes=int(match.group("bytes")),
                                        disposition="ATTACK" if attacked else "FORWARD",
                                        attack="REPLAY" if attacked else None,
                                        detail="replay script TX observed")
        match = _SPOOF_TX.search(line)
        if match and scenario == "spoofing":
            attacked = match.group("status") == "SPOOFED"
            return ExecutionObservation(**common, iq_bytes=int(match.group("bytes")),
                                        disposition="ATTACK" if attacked else "FORWARD",
                                        attack="SPOOFING" if attacked else None,
                                        detail="spoof script TX observed")
        match = _TAMPER_TX.search(line)
        if match and scenario == "malformed":
            attacked = match.group("status") == "MODIFIÉE"
            return ExecutionObservation(**common, iq_bytes=int(match.group("bytes")),
                                        disposition="ATTACK" if attacked else "FORWARD",
                                        attack="MALFORMED" if attacked else None,
                                        detail="tamper script TX observed")
        return None
