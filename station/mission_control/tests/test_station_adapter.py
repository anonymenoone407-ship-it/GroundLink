import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from mission_control.agent_contract import AgentIdentity, IngestedTelemetry
from mission_control.models import SatelliteEvent, ValidationError
from mission_control.server.broker import EventBroker
from mission_control.server.database import MissionControlDB
from mission_control.server.station_adapter import LocalStationAdapter


RUN = "9" * 32
NOW = datetime.now(timezone.utc).isoformat()


class LocalStationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.mc_path = root / "mc.sqlite3"
        self.monitor_path = root / "monitoring.sqlite3"
        self.db = MissionControlDB(self.mc_path)
        with sqlite3.connect(self.mc_path) as db:
            db.execute("INSERT INTO runs VALUES (?, 'baseline', 'LIVE', 'ACTIVE', ?, NULL, ?)",
                       (RUN, NOW, "local receiver test"))
        with sqlite3.connect(self.monitor_path) as db:
            db.execute("""CREATE TABLE rx_events (
                id INTEGER PRIMARY KEY, event_time TEXT, run_id TEXT, scenario TEXT, iq_bytes INTEGER,
                power_db_rel REAL, evm_percent REAL, estimated_gain REAL,
                residual_spectral_peak_db REAL, status TEXT, reason TEXT, satellite_id TEXT,
                session_id TEXT, sequence_number INTEGER, processing_ms REAL, rf_level TEXT, rf_reasons TEXT)""")
            db.execute("INSERT INTO rx_events VALUES (1, ?, 'receiver-run', 'baseline', 20, NULL, NULL, NULL, NULL, 'ACCEPT', 'OK', 'SAT-001', 'sess', 1, 2.5, 'NORMAL', 'NONE')", (NOW,))
            db.execute("INSERT INTO rx_events VALUES (2, ?, 'receiver-run', 'baseline', 20, NULL, NULL, NULL, NULL, 'REJECT', 'CRC_INVALID', NULL, NULL, NULL, 2.0, 'UNKNOWN', 'RF_METRICS_UNAVAILABLE')", (NOW,))
        self.broker = EventBroker(self.db)
        self.adapter = LocalStationAdapter(self.broker, self.monitor_path)
        identity = AgentIdentity("vm1-satellite", "SATELLITE", "1")
        for number in (1, 2):
            event = SatelliteEvent(RUN, NOW, "baseline", f"sat-{number}", satellite_id="SAT-001", session_id="sess", sequence_number=number)
            await self.broker.publish_ingested(IngestedTelemetry(identity, f"frame-{number}", event))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_accept_and_reject_are_copied_from_read_only_receiver_rows(self):
        before = hashlib.sha256(self.monitor_path.read_bytes()).digest()
        accepted, _ = await self.adapter.publish_row(1, mission_run_id=RUN, monitoring_run_id="receiver-run", scenario="baseline", frame_id="frame-1")
        rejected, _ = await self.adapter.publish_row(2, mission_run_id=RUN, monitoring_run_id="receiver-run", scenario="baseline", frame_id="frame-2")
        self.assertEqual((accepted["decision"], rejected["decision"]), ("ACCEPT", "REJECT"))
        self.assertEqual(accepted["source_agent_id"], "vm2-station")
        self.assertIsNone(rejected["satellite_id"])
        self.assertEqual(before, hashlib.sha256(self.monitor_path.read_bytes()).digest())

    async def test_provenance_and_frame_binding_fail_closed(self):
        with self.assertRaises(ValidationError):
            await self.adapter.publish_row(1, mission_run_id=RUN, monitoring_run_id="wrong", scenario="baseline", frame_id="frame-1")
        with self.assertRaises(ValidationError):
            await self.adapter.publish_row(1, mission_run_id=RUN, monitoring_run_id="receiver-run", scenario="baseline", frame_id="missing")

    async def test_read_only_adapter_sees_recent_committed_wal_rows(self):
        writer = sqlite3.connect(self.monitor_path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO rx_events VALUES (3, ?, 'receiver-run', 'baseline', 8, NULL, NULL, NULL, NULL, 'REJECT', 'NEW_ROW', NULL, NULL, NULL, 1.0, 'UNKNOWN', 'NONE')", (NOW,))
            writer.commit()
            row = self.adapter._read_row(3)
            self.assertEqual((row["id"], row["reason"]), (3, "NEW_ROW"))
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
