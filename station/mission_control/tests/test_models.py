import unittest

from mission_control.models import ChannelEvent, SatelliteEvent, StationEvent, ValidationError, event_from_dict
from mission_control.run_id import new_run_id


BASE = {"run_id": "a" * 32, "event_time": "2026-10-31T12:00:00+00:00", "scenario": "baseline", "event_id": "evt-1"}


class ModelTests(unittest.TestCase):
    def test_common_run_id_matches_station_format(self):
        self.assertRegex(new_run_id(), r"^[0-9a-f]{32}$")

    def test_round_trip_all_event_types(self):
        events = [
            SatelliteEvent(**BASE, satellite_id="SAT-001", session_id="s1", sequence_number=1),
            ChannelEvent(**BASE, iq_bytes=96768, power_db_rel=0.1, evm_percent=2.0, estimated_gain=1.0),
            StationEvent(**BASE, station_event_id=7, decision="ACCEPT", reason="OK", rf_level="NORMAL", rf_reasons="RF_OK", iq_bytes=96768, satellite_id="SAT-001", session_id="s1", sequence_number=1),
        ]
        for event in events:
            self.assertEqual(event_from_dict(event.to_dict()), event)

    def test_scenario_allowlist_is_closed(self):
        with self.assertRaises(ValidationError):
            ChannelEvent(**{**BASE, "scenario": "invented_attack"}, iq_bytes=1)

    def test_only_station_event_can_have_decision(self):
        data = SatelliteEvent(**BASE, satellite_id="SAT-001", session_id="s1", sequence_number=1).to_dict()
        data["decision"] = "ACCEPT"
        with self.assertRaises(ValidationError):
            event_from_dict(data)

    def test_rejected_station_event_cannot_claim_identity(self):
        with self.assertRaises(ValidationError):
            StationEvent(**BASE, station_event_id=1, decision="REJECT", reason="CRC_INVALID", rf_level="UNKNOWN", rf_reasons="RF_METRICS_UNAVAILABLE", iq_bytes=7, satellite_id="SAT-001")

    def test_station_decision_source_is_fixed(self):
        with self.assertRaises(ValidationError):
            StationEvent(**BASE, station_event_id=1, decision="REJECT", reason="CRC_INVALID", rf_level="UNKNOWN", rf_reasons="RF_METRICS_UNAVAILABLE", iq_bytes=7, decision_source="dashboard")


if __name__ == "__main__":
    unittest.main()
