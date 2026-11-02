"""Clearly synthetic animation events for MOCK runs; no security decisions."""

from datetime import datetime, timezone

from mission_control.models import ChannelEvent, SatelliteEvent


def mock_events(run_id: str, scenario: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        SatelliteEvent(
            run_id=run_id,
            event_time=now,
            scenario=scenario,
            event_id=f"mock:{run_id}:satellite:1",
            satellite_id="MOCK-SATELLITE",
            session_id=f"mock-{run_id}",
            sequence_number=1,
        ).to_dict(),
        ChannelEvent(
            run_id=run_id,
            event_time=now,
            scenario=scenario,
            event_id=f"mock:{run_id}:channel:1",
            iq_bytes=0,
        ).to_dict(),
    ]
