"""Read-only provenance bridge from the real Station receiver database."""

from contextlib import closing
from pathlib import Path
import sqlite3

from mission_control.agent_contract import AgentId, AgentRole, AgentIdentity, IngestedTelemetry
from mission_control.models import StationEvent, ValidationError

from .broker import EventBroker, EventSource


class LocalStationAdapter:
    """Publishes only rows re-read from one explicitly configured local DB."""

    def __init__(self, broker: EventBroker, monitoring_db: Path):
        self.broker = broker
        self.monitoring_db = Path(monitoring_db).resolve(strict=True)

    def _read_row(self, row_id: int) -> sqlite3.Row:
        # Do not use immutable=1: the receiver writes in WAL mode and immutable
        # readers are allowed to ignore the WAL and therefore miss committed rows.
        uri = f"file:{self.monitoring_db}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM rx_events WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise LookupError("Station monitoring row not found")
        return row

    async def publish_row(
        self, row_id: int, *, mission_run_id: str, monitoring_run_id: str,
        scenario: str, frame_id: str,
    ) -> tuple[dict, bool]:
        row = self._read_row(row_id)
        if row["run_id"] != monitoring_run_id or row["scenario"] != scenario:
            raise ValidationError("Station row provenance does not match the local run binding")
        event = StationEvent(
            run_id=mission_run_id, event_time=row["event_time"], scenario=scenario,
            event_id=f"station-db:{monitoring_run_id}:{row_id}", station_event_id=row_id,
            decision=row["status"], reason=row["reason"],
            rf_level=row["rf_level"] or "UNKNOWN",
            rf_reasons=row["rf_reasons"] or "RF_METRICS_UNAVAILABLE",
            iq_bytes=row["iq_bytes"], processing_ms=row["processing_ms"],
            satellite_id=row["satellite_id"], session_id=row["session_id"],
            sequence_number=row["sequence_number"],
        )
        # The broker's Station source capability is created here, after a read-only
        # DB lookup; callers cannot turn arbitrary JSON into this event.
        run = self.broker.database.get_run(mission_run_id)
        if run["mode"] != "LIVE":
            raise PermissionError("Station decisions are accepted only for LIVE runs")
        if run["scenario"] != scenario:
            raise ValidationError("Station event scenario does not match its run")
        if not self.broker.database.has_frame_event(
            mission_run_id, frame_id, "satellite.telemetry_transmitted"
        ):
            raise ValidationError("Station decision has no correlated Satellite frame")
        async with self.broker._locks[mission_run_id]:
            stored, duplicate = self.broker.database.store_event(
                event.to_dict(), frame_id=frame_id,
                source_agent_id=AgentId.STATION.value, source_role=AgentRole.STATION.value,
            )
            if not duplicate:
                self.broker._fanout(mission_run_id, stored)
        return stored, duplicate

