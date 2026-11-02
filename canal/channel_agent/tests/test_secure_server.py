from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from groundlink_command_transport.command_protocol import (
    CommandRequest, command_ack_from_dict, command_request_from_dict, correlate_ack,
)
from channel_agent import ChannelCommandServer, DurableCommandProcessor, ServerConfig
from channel_agent.core import CommandRejected

NOW = datetime.now(timezone.utc)
RUN = "1" * 32


def request(kind="PREPARE", command_id="cmd-1", scenario="baseline", run_id=RUN,
            preparation_id=None, request_id="req-1"):
    command = {"contract_version": "1.0.0", "command_type": kind,
               "command_id": command_id, "run_id": run_id, "scenario": scenario,
               "issued_at": NOW.isoformat()}
    if kind == "START": command["preparation_id"] = preparation_id
    if kind == "STOP": command["reason"] = "operator_requested"
    return {"protocol_version": "1.0", "request_id": request_id,
            "sent_at": NOW.isoformat(), "deadline_at": (NOW + timedelta(minutes=5)).isoformat(),
            "target_agent_id": "vm3-channel", "command": command}


class PKI:
    def __init__(self, root: Path):
        self.root = root
        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GroundLink Test CA")])
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(True, False, True, False, False, True, True, False, False), critical=True)
                   .sign(self.ca_key, hashes.SHA256()))
        self._write("ca.pem", self.ca.public_bytes(serialization.Encoding.PEM))
        self.server = self.leaf("server", "vm3-channel", ExtendedKeyUsageOID.SERVER_AUTH)
        self.station = self.leaf("station", "vm2-station", ExtendedKeyUsageOID.CLIENT_AUTH)
        self.wrong = self.leaf("wrong", "vm1-satellite", ExtendedKeyUsageOID.CLIENT_AUTH)
        self.expired = self.leaf("expired", "vm2-station", ExtendedKeyUsageOID.CLIENT_AUTH,
                                 before=NOW - timedelta(days=3), after=NOW - timedelta(days=2))
        self.revoked = self.leaf("revoked", "vm2-station", ExtendedKeyUsageOID.CLIENT_AUTH)
        revoked_cert = (x509.RevokedCertificateBuilder().serial_number(self.revoked.serial_number)
                        .revocation_date(NOW - timedelta(hours=1)).build())
        crl = (x509.CertificateRevocationListBuilder().issuer_name(self.ca.subject)
               .last_update(NOW - timedelta(hours=1)).next_update(NOW + timedelta(days=1))
               .add_revoked_certificate(revoked_cert).sign(self.ca_key, hashes.SHA256()))
        self._write("crl.pem", crl.public_bytes(serialization.Encoding.PEM))

    def _write(self, name, data):
        (self.root / name).write_bytes(data)

    def leaf(self, prefix, san, eku, before=NOW - timedelta(hours=1), after=NOW + timedelta(days=1)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
                .issuer_name(self.ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(before).not_valid_after(after)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
                .sign(self.ca_key, hashes.SHA256()))
        self._write(prefix + ".pem", cert.public_bytes(serialization.Encoding.PEM))
        self._write(prefix + ".key", key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return cert


class SecureServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pki = PKI(self.root)

    def tearDown(self):
        if hasattr(self, "server"): self.server.close()
        self.temp.cleanup()

    def config(self, **changes):
        values = dict(certificate=self.root / "server.pem", private_key=self.root / "server.key",
                      ca_certificate=self.root / "ca.pem", crl=self.root / "crl.pem",
                      database=self.root / "commands.sqlite3", audit_log=self.root / "audit.jsonl",
                      host="127.0.0.1", port=0, enabled=True, io_timeout_seconds=0.3)
        values.update(changes)
        return ServerConfig(**values)

    def start(self, **changes):
        self.server = ChannelCommandServer(self.config(**changes))
        self.server.start_background()

    def exchange(self, value, cert="station", *, raw_length=None):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.root / "ca.pem"))
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        if cert:
            context.load_cert_chain(self.root / f"{cert}.pem", self.root / f"{cert}.key")
        with socket.create_connection(self.server.bound_address, timeout=1) as raw:
            with context.wrap_socket(raw, server_hostname="vm3-channel") as stream:
                payload = json.dumps(value, separators=(",", ":")).encode()
                stream.sendall(struct.pack("!I", raw_length if raw_length is not None else len(payload)) + payload)
                header = stream.recv(4)
                if not header: return None
                size = struct.unpack("!I", header)[0]
                body = bytearray()
                while len(body) < size: body.extend(stream.recv(size - len(body)))
                return json.loads(body)

    def test_valid_station_certificate_and_duplicate(self):
        self.start()
        first = self.exchange(request())
        self.assertTrue(first["accepted"])
        duplicate = self.exchange(request(request_id="req-2"))
        self.assertEqual(duplicate["request_id"], "req-2")
        self.assertEqual(first["preparation_id"], duplicate["preparation_id"])

    def test_missing_wrong_revoked_and_expired_certificates(self):
        self.start()
        for cert in (None, "wrong", "revoked", "expired"):
            with self.subTest(cert=cert), self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
                result = self.exchange(request(request_id=f"req-{cert}"), cert=cert)
                if result is None: raise ConnectionError("rejected")

    def test_invalid_version_unknown_command_and_limits(self):
        self.start()
        for field, value in (("protocol_version", "2.0"),):
            bad = request(); bad[field] = value
            self.assertIsNone(self.exchange(bad))
        bad = request(); bad["command"]["command_type"] = "EXEC"
        self.assertIsNone(self.exchange(bad))
        self.assertIsNone(self.exchange({}, raw_length=16385))

    def test_read_timeout(self):
        self.start()
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.root / "ca.pem"))
        context.load_cert_chain(self.root / "station.pem", self.root / "station.key")
        with socket.create_connection(self.server.bound_address, timeout=1) as raw:
            with context.wrap_socket(raw, server_hostname="vm3-channel") as stream:
                stream.sendall(struct.pack("!I", 100) + b"{")
                time.sleep(0.5)
                self.assertEqual(stream.recv(1), b"")

    def test_rate_limit_and_expired_deadline(self):
        self.start(requests_per_minute=1)
        self.assertTrue(self.exchange(request())["accepted"])
        with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
            result = self.exchange(request(request_id="rate-limited"))
            if result is None: raise ConnectionError("rate limited")
        self.server.close()
        del self.server
        self.start()
        expired = request(request_id="expired")
        expired["sent_at"] = (NOW - timedelta(minutes=2)).isoformat()
        expired["deadline_at"] = (NOW - timedelta(minutes=1)).isoformat()
        self.assertIsNone(self.exchange(expired))

    def test_listener_disabled_and_non_loopback_refused(self):
        with self.assertRaises(RuntimeError):
            ChannelCommandServer(self.config(enabled=False)).start_background()
        with self.assertRaises(RuntimeError):
            ChannelCommandServer(self.config(host="10.10.3.30")).start_background()


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.sqlite3"

    def tearDown(self): self.temp.cleanup()

    def parse(self, value): return command_request_from_dict(value)

    def test_prepare_start_recovery_collision_and_safe_stop(self):
        processor = DurableCommandProcessor(self.database)
        prepare = self.parse(request())
        ack = processor.process(prepare)
        self.assertEqual(ack.state, "PREPARED")
        recovered = DurableCommandProcessor(self.database)
        self.assertEqual(recovered.agent.state, "PREPARED")
        start = self.parse(request("START", "cmd-2", preparation_id="cmd-1", request_id="req-2"))
        self.assertEqual(recovered.process(start).state, "RUNNING")
        conservative = DurableCommandProcessor(self.database)
        self.assertEqual(conservative.agent.state, "ERROR")
        with self.assertRaises(CommandRejected):
            conservative.process(self.parse(request(scenario="replay", request_id="collision")))
        stop = self.parse(request("STOP", "cmd-3", request_id="req-3"))
        self.assertEqual(conservative.process(stop).state, "IDLE")

    def test_wrong_preparation_concurrent_invalid_ids_and_scenarios(self):
        processor = DurableCommandProcessor(self.database)
        processor.process(self.parse(request()))
        wrong = processor.process(self.parse(request("START", "cmd-2", preparation_id="wrong", request_id="r2")))
        self.assertFalse(wrong.accepted)
        concurrent = processor.process(self.parse(request("PREPARE", "cmd-3", run_id="2" * 32, request_id="r3")))
        self.assertFalse(concurrent.accepted)
        for mutation in (lambda x: x["command"].update(run_id="BAD"),
                         lambda x: x["command"].update(scenario="fading")):
            bad = request(); mutation(bad)
            with self.assertRaises(Exception): self.parse(bad)

    def test_channelized_station_golden_vector_compatibility(self):
        root = Path(__file__).parents[2] / "dist" / "groundlink-command-transport-1.0.0" / "vectors"
        data = json.loads((root / "prepare-request.json").read_text())
        data["target_agent_id"] = "vm3-channel"
        request_model = self.parse(data)
        processor = DurableCommandProcessor(self.database, clock=lambda: "2026-11-01T12:00:01+00:00")
        ack = processor.process(request_model)
        correlate_ack(request_model, command_ack_from_dict(ack.to_dict()))
        self.assertEqual(ack.agent_id, "vm3-channel")

    def test_no_station_decisions_or_process_execution_interface(self):
        processor = DurableCommandProcessor(self.database)
        ack = processor.process(self.parse(request())).to_dict()
        rendered = json.dumps(ack)
        for forbidden in ("ACCEPT", "REJECT", "station.frame_decided", "executable", "argv", "shell"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
