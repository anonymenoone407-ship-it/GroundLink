import tempfile
from pathlib import Path
import unittest

from mission_control.models import ValidationError
from mission_control.server.broker import EventAuthorizationError, EventBroker, EventSource
from mission_control.server.database import ConflictError, MissionControlDB


RUN_ID = "c" * 32
BASE = {
    "schema_version": "1.0.0",
    "run_id": RUN_ID,
    "event_time": "2026-10-31T12:00:00Z",
    "scenario": "baseline",
}


def channel(event_id: str) -> dict:
    return {
        **BASE,
        "event_type": "channel.rf_observed",
        "event_id": event_id,
        "iq_bytes": 16,
        "power_db_rel": None,
        "evm_percent": None,
        "estimated_gain": None,
        "residual_spectral_peak_db": None,
    }


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = MissionControlDB(Path(self.temp.name) / "mc.sqlite3")
        self.db.create_run(RUN_ID, "baseline", "MOCK")
        self.broker = EventBroker(self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_deduplicates_and_orders_per_run(self):
        first, duplicate = await self.broker.publish(channel("evt-1"))
        self.assertFalse(duplicate)
        same, duplicate = await self.broker.publish(channel("evt-1"))
        self.assertTrue(duplicate)
        second, _ = await self.broker.publish(channel("evt-2"))
        self.assertEqual((first["run_sequence"], same["run_sequence"], second["run_sequence"]), (1, 1, 2))
        self.assertEqual([event["event_id"] for event in self.db.list_events(RUN_ID)], ["evt-1", "evt-2"])

    async def test_broadcasts_new_event_to_subscriber(self):
        queue = self.broker.subscribe(RUN_ID)
        try:
            published, _ = await self.broker.publish(channel("broadcast-1"))
            delivered = await queue.get()
            self.assertEqual(delivered, published)
        finally:
            self.broker.unsubscribe(RUN_ID, queue)

    async def test_events_remain_isolated_between_runs(self):
        await self.broker.publish(channel("first-run"))
        self.db.stop_run(RUN_ID)
        other_run = "d" * 32
        self.db.create_run(other_run, "baseline", "MOCK")
        other = channel("second-run")
        other["run_id"] = other_run
        await self.broker.publish(other)
        self.assertEqual([event["event_id"] for event in self.db.list_events(RUN_ID)], ["first-run"])
        self.assertEqual([event["event_id"] for event in self.db.list_events(other_run)], ["second-run"])

    async def test_validates_run_and_scenario(self):
        invalid = channel("bad")
        invalid["scenario"] = "awgn_0db"
        with self.assertRaises(ValidationError):
            await self.broker.publish(invalid)

    async def test_station_decision_requires_authenticated_station_and_not_mock(self):
        station = {
            **BASE,
            "event_type": "station.frame_decided",
            "event_id": "station-1",
            "station_event_id": 1,
            "decision": "REJECT",
            "reason": "CRC_INVALID",
            "rf_level": "UNKNOWN",
            "rf_reasons": "RF_METRICS_UNAVAILABLE",
            "iq_bytes": 16,
            "processing_ms": None,
            "satellite_id": None,
            "session_id": None,
            "sequence_number": None,
            "decision_source": "station_receiver",
        }
        with self.assertRaises(EventAuthorizationError):
            await self.broker.publish(station)
        with self.assertRaises(EventAuthorizationError):
            await self.broker.publish(
                station, source=EventSource(role="station_receiver", authenticated=True)
            )

    async def test_stop_is_idempotent_and_blocks_new_events(self):
        stopped = self.db.stop_run(RUN_ID)
        stopped_again = self.db.stop_run(RUN_ID)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(stopped_again["status"], "STOPPED")
        self.assertEqual(stopped["stopped_at"], stopped_again["stopped_at"])
        with self.assertRaises(ConflictError):
            await self.broker.publish(channel("late"))


if __name__ == "__main__":
    unittest.main()
