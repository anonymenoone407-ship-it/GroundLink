import json
from pathlib import Path
import unittest

from groundlink_command_transport.agent_contract import ValidationError
from groundlink_command_transport.command_protocol import (
    COMMAND_PROTOCOL_VERSION, MAX_COMMAND_WIRE_BYTES,
    command_ack_from_dict, command_request_from_dict, correlate_ack,
)


ROOT = Path(__file__).parents[1]
VECTORS = ROOT / "vectors"


def vector(name):
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


class PortableCompatibilityTests(unittest.TestCase):
    def test_golden_vectors_round_trip_and_correlate(self):
        for command in ("prepare", "start", "stop"):
            request = command_request_from_dict(vector(f"{command}-request.json"))
            ack = command_ack_from_dict(vector(f"{command}-ack.json"))
            self.assertEqual(request.to_dict(), vector(f"{command}-request.json"))
            self.assertEqual(ack.to_dict(), vector(f"{command}-ack.json"))
            correlate_ack(request, ack)

    def test_versions_and_allowlist(self):
        self.assertEqual(COMMAND_PROTOCOL_VERSION, "1.0")
        for name in ("prepare", "start", "stop"):
            self.assertEqual(
                command_request_from_dict(vector(f"{name}-request.json")).command.to_dict()["contract_version"],
                "1.0.0",
            )
        bad = vector("prepare-request.json"); bad["protocol_version"] = "2.0"
        with self.assertRaises(ValidationError): command_request_from_dict(bad)
        bad = vector("prepare-request.json"); bad["command"]["contract_version"] = "2.0.0"
        with self.assertRaises(ValidationError): command_request_from_dict(bad)
        bad = vector("prepare-request.json"); bad["command"]["command_type"] = "EXEC"
        with self.assertRaises(ValidationError): command_request_from_dict(bad)

    def test_unknown_and_unsafe_fields_rejected(self):
        for field, value in (("argv", ["/bin/sh"]), ("script", "run"), ("environment", {"X": "Y"})):
            bad = vector("prepare-request.json"); bad["command"][field] = value
            with self.assertRaises(ValidationError): command_request_from_dict(bad)
        bad = vector("prepare-ack.json"); bad["certificate_identity"] = "vm1-satellite"
        with self.assertRaises(ValidationError): command_ack_from_dict(bad)

    def test_request_command_and_preparation_correlation(self):
        request = command_request_from_dict(vector("start-request.json"))
        for field, value in (
            ("request_id", "different-request"), ("agent_id", "vm3-channel"),
            ("command_id", "different-command"), ("run_id", "f" * 32),
            ("scenario", "jamming"), ("preparation_id", "different-preparation"),
        ):
            bad = vector("start-ack.json"); bad[field] = value
            ack = command_ack_from_dict(bad)
            with self.assertRaises(ValidationError): correlate_ack(request, ack)

    def test_identifiers_and_size_limit(self):
        self.assertEqual(MAX_COMMAND_WIRE_BYTES, 16384)
        for field, value in (("request_id", "bad id"),):
            bad = vector("prepare-request.json"); bad[field] = value
            with self.assertRaises(ValidationError): command_request_from_dict(bad)
        bad = vector("prepare-request.json"); bad["command"]["command_id"] = ""
        with self.assertRaises(ValidationError): command_request_from_dict(bad)
        bad = vector("prepare-request.json"); bad["command"]["run_id"] = "ABC"
        with self.assertRaises(ValidationError): command_request_from_dict(bad)

    def test_satellite_and_channel_identity_targets(self):
        satellite_request = command_request_from_dict(vector("prepare-request.json"))
        satellite_ack = command_ack_from_dict(vector("prepare-ack.json"))
        correlate_ack(satellite_request, satellite_ack)
        channel_request_data = vector("prepare-request.json")
        channel_request_data["target_agent_id"] = "vm3-channel"
        channel_ack_data = vector("prepare-ack.json")
        channel_ack_data["agent_id"] = "vm3-channel"
        correlate_ack(
            command_request_from_dict(channel_request_data),
            command_ack_from_dict(channel_ack_data),
        )


if __name__ == "__main__":
    unittest.main()
