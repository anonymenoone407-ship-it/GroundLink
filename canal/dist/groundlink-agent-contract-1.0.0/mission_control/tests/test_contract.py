import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from mission_control.models import ChannelEvent, SatelliteEvent, StationEvent


BASE = {"run_id": "b" * 32, "event_time": "2026-10-31T12:00:00Z", "scenario": "awgn_20db", "event_id": "contract-1"}


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_path = Path(__file__).parents[1] / "schemas" / "telemetry-v1.schema.json"
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def test_runtime_models_conform_to_wire_schema(self):
        events = [
            SatelliteEvent(**BASE, satellite_id="SAT-001", session_id="s", sequence_number=1),
            ChannelEvent(**BASE, iq_bytes=16),
            StationEvent(**BASE, station_event_id=2, decision="REJECT", reason="CRC_INVALID", rf_level="UNKNOWN", rf_reasons="RF_METRICS_UNAVAILABLE", iq_bytes=16),
        ]
        for event in events:
            self.validator.validate(event.to_dict())

    def test_schema_rejects_decision_on_channel_event(self):
        event = ChannelEvent(**BASE, iq_bytes=16).to_dict()
        event["decision"] = "ACCEPT"
        self.assertTrue(list(self.validator.iter_errors(event)))

    def test_schema_rejects_fabricated_station_source(self):
        event = StationEvent(**BASE, station_event_id=2, decision="REJECT", reason="CRC_INVALID", rf_level="UNKNOWN", rf_reasons="RF_METRICS_UNAVAILABLE", iq_bytes=16).to_dict()
        event["decision_source"] = "simulation"
        self.assertTrue(list(self.validator.iter_errors(event)))


if __name__ == "__main__":
    unittest.main()
