"""Narrow integration boundary for the existing secure transmitter.

No implementation in this module launches a process or accesses key material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TransmissionConfirmation:
    """Fact reported only after the transmitter's ZeroMQ send returns."""

    satellite_id: str
    session_id: str
    sequence_number: int


class TransmitterAdapter(Protocol):
    def prepare(self, run_id: str, scenario: str) -> None: ...
    def start(self, run_id: str, scenario: str) -> None: ...
    def request_stop(self, run_id: str) -> None: ...


class OfflineTransmitterAdapter:
    """Safe test adapter: records lifecycle requests and never transmits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def prepare(self, run_id: str, scenario: str) -> None:
        self.calls.append(("prepare", run_id, scenario))

    def start(self, run_id: str, scenario: str) -> None:
        self.calls.append(("start", run_id, scenario))

    def request_stop(self, run_id: str) -> None:
        self.calls.append(("stop", run_id, None))


class SatelliteSecureIntegrationAdapter:
    """Design contract for a future, fixed satellite_secure.py runner.

    The runner must call ``confirm_handoff`` after each successful
    ``socket.send(iq_samples.tobytes())`` return. Starting the process,
    constructing/signing/modulating a frame, or calling ``socket.connect`` is
    insufficient evidence and must never call this method. Receiver delivery
    requires a separate authenticated Channel acknowledgement in a later phase.

    This adapter deliberately contains no subprocess launcher. A future LIVE
    implementation must use one fixed executable/script, a dedicated account,
    authenticated control, and explicit shutdown/reaping.
    """

    def __init__(self, confirmation_sink) -> None:
        self._confirmation_sink = confirmation_sink

    def confirm_handoff(self, satellite_id: str, session_id: str, sequence_number: int):
        return self._confirmation_sink(
            TransmissionConfirmation(satellite_id, session_id, sequence_number)
        )
