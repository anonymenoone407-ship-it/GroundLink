import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from mission_control.agent_contract import AuthenticatedAgent
from mission_control.models import ValidationError
from mission_control.server.agent_ingestion import AgentIngestionAdapter, RequestContext
from mission_control.server.broker import EventBroker
from mission_control.server.database import ConflictError, MissionControlDB


RUN = "a" * 32
NOW = datetime.now(timezone.utc).isoformat()


def peer(agent="vm1-satellite", role="SATELLITE", authenticated=True):
    return AuthenticatedAgent(agent, role, "mTLS", authenticated)


def context(identity, request_id):
    return RequestContext(identity, request_id, datetime.now(timezone.utc))


def satellite(event_id="sat-1", frame="frame-1", run=RUN, scenario="baseline"):
    return {
        "contract_version": "1.0.0",
        "identity": {"agent_id": "vm1-satellite", "role": "SATELLITE", "software_version": "1.0", "contract_version": "1.0.0"},
        "frame_id": frame,
        "event": {"schema_version": "1.0.0", "run_id": run, "event_time": NOW,
                  "scenario": scenario, "event_id": event_id,
                  "event_type": "satellite.telemetry_transmitted", "satellite_id": "SAT-001",
                  "session_id": "session-1", "sequence_number": 1},
    }


def channel(event_id="rf-1", frame="frame-1", run=RUN, scenario="baseline"):
    value = satellite(event_id, frame, run, scenario)
    value["identity"] = {"agent_id": "vm3-channel", "role": "CHANNEL", "software_version": "1.0", "contract_version": "1.0.0"}
    value["event"] = {"schema_version": "1.0.0", "run_id": run, "event_time": NOW,
                      "scenario": scenario, "event_id": event_id, "event_type": "channel.rf_observed",
                      "iq_bytes": 16, "power_db_rel": None, "evm_percent": None,
                      "estimated_gain": None, "residual_spectral_peak_db": None}
    return value


def action(disposition, event_id, frame="frame-1"):
    value = {"message_type": "channel.action", "contract_version": "1.0.0", "run_id": RUN,
             "frame_id": frame, "event_id": event_id, "event_time": NOW, "scenario": "baseline",
             "disposition": disposition}
    if disposition == "ATTACK":
        value["attack"] = "JAMMING"
    return value


class AgentIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mc.sqlite3"
        self.db = MissionControlDB(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO runs VALUES (?, ?, 'LIVE', 'ACTIVE', ?, NULL, ?)",
                       (RUN, "baseline", NOW, "test integration binding"))
        self.broker = EventBroker(self.db)
        self.adapter = AgentIngestionAdapter(self.broker)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def put_sat(self, event_id="sat-1", frame="frame-1", request_id="req-sat"):
        return await self.adapter.ingest_telemetry(
            satellite(event_id, frame), context=context(peer(), request_id), encoded_size=500)

    async def test_valid_satellite_and_channel_telemetry_persist_and_deliver(self):
        queue = self.broker.subscribe(RUN)
        sat, duplicate = await self.put_sat()
        self.assertFalse(duplicate)
        self.assertEqual((await queue.get())["frame_id"], "frame-1")
        ch, _ = await self.adapter.ingest_telemetry(
            channel(), context=context(peer("vm3-channel", "CHANNEL"), "req-ch"), encoded_size=500)
        self.assertEqual(ch["source_agent_id"], "vm3-channel")
        self.assertEqual((await queue.get())["event_type"], "channel.rf_observed")
        self.assertEqual(len(self.db.list_events(RUN)), 2)

    async def test_forward_drop_attack_are_distinct_actions(self):
        await self.put_sat()
        for index, disposition in enumerate(("FORWARD", "DROP", "ATTACK"), 1):
            frame = f"frame-{index}"
            if index > 1:
                await self.put_sat(f"sat-{index}", frame, f"req-sat-{index}")
            stored, _ = await self.adapter.ingest_channel_action(
                action(disposition, f"act-{index}", frame),
                context=context(peer("vm3-channel", "CHANNEL"), f"req-act-{index}"), encoded_size=300)
            self.assertEqual(stored["event_type"], "channel.action")
            self.assertEqual(stored["disposition"], disposition)

    async def test_unknown_type_version_run_scenario_and_missing_frame_rejected(self):
        bad = satellite()
        bad["event"]["event_type"] = "station.magic"
        with self.assertRaises(ValidationError):
            await self.adapter.ingest_telemetry(bad, context=context(peer(), "bad-type"), encoded_size=100)
        bad = satellite(); bad["contract_version"] = "2.0.0"
        with self.assertRaises(ValidationError):
            await self.adapter.ingest_telemetry(bad, context=context(peer(), "bad-version"), encoded_size=100)
        for key, value in (("run_id", "b" * 32), ("scenario", "jamming")):
            bad = satellite(); bad["event"][key] = value
            with self.assertRaises((ValidationError, LookupError)):
                await self.adapter.ingest_telemetry(bad, context=context(peer(), f"bad-{key}"), encoded_size=100)
        with self.assertRaises(ValidationError):
            await self.adapter.ingest_telemetry(channel(frame="absent"), context=context(peer("vm3-channel", "CHANNEL"), "missing"), encoded_size=100)

    async def test_auth_identity_staleness_request_replay_and_size_fail_closed(self):
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(satellite(), context=context(peer(authenticated=False), "unauth"), encoded_size=100)
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(satellite(), context=context(peer("vm3-channel", "CHANNEL"), "forged"), encoded_size=100)
        stale = RequestContext(peer(), "stale", datetime.now(timezone.utc) - timedelta(hours=1))
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(satellite(), context=stale, encoded_size=100)
        await self.put_sat()
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(satellite(), context=context(peer(), "req-sat"), encoded_size=100)
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(satellite(), context=context(peer(), "huge"), encoded_size=100_000)

    async def test_event_id_dedup_and_duplicate_frame_rejected(self):
        first, _ = await self.put_sat()
        same, duplicate = await self.adapter.ingest_telemetry(
            satellite(), context=context(peer(), "req-duplicate"), encoded_size=100)
        self.assertTrue(duplicate)
        self.assertEqual(first["run_sequence"], same["run_sequence"])
        with self.assertRaises(ConflictError):
            await self.put_sat("different-event", "frame-1", "different-request")

    async def test_forged_remote_station_decision_rejected(self):
        raw = satellite()
        raw["identity"] = {"agent_id": "vm2-station", "role": "STATION", "software_version": "1", "contract_version": "1.0.0"}
        raw["event"] = {"schema_version": "1.0.0", "run_id": RUN, "event_time": NOW,
                        "scenario": "baseline", "event_id": "station-forged", "event_type": "station.frame_decided",
                        "station_event_id": 1, "decision": "REJECT", "reason": "X", "rf_level": "UNKNOWN",
                        "rf_reasons": "NONE", "iq_bytes": 1, "processing_ms": None, "satellite_id": None,
                        "session_id": None, "sequence_number": None, "decision_source": "station_receiver"}
        with self.assertRaises(PermissionError):
            await self.adapter.ingest_telemetry(raw, context=context(peer("vm2-station", "STATION"), "station"), encoded_size=500)


if __name__ == "__main__":
    unittest.main()
