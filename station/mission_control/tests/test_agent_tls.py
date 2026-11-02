import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import sqlite3
import ssl
import struct
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mission_control.server.agent_ingestion import AgentIngestionAdapter
from mission_control.server.agent_tls import (
    AgentTLSServer, CertificatePolicy, PersistentCertificatePolicy, TLSFiles,
    TransportError, create_server_ssl_context,
)
from mission_control.server.broker import EventBroker
from mission_control.server.database import MissionControlDB


RUN = "7" * 32


class TestPKI:
    def __init__(self, root: Path):
        self.root = root
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GroundLink Test CA")])
        now = datetime.now(timezone.utc)
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .sign(self.ca_key, hashes.SHA256()))
        self.ca_path = self._write_cert("ca", self.ca)

    def _write_cert(self, name, cert):
        path = self.root / f"{name}.pem"
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return path

    def leaf(self, name, *, server=False, expired=False, dns_names=None, file_tag=None):
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        before = now - timedelta(days=3 if expired else 1)
        after = now - timedelta(days=2) if expired else now + timedelta(days=1)
        san = [x509.DNSName(value) for value in (dns_names or [name])]
        if server:
            san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(self.ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(before).not_valid_after(after)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName(san), critical=False)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=None, decipher_only=None,
                ), critical=True)
                .add_extension(x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
                ]), critical=False).sign(self.ca_key, hashes.SHA256()))
        output_name = (file_tag or name).replace(".", "-")
        cert_path = self._write_cert(output_name, cert)
        key_path = self.root / f"{output_name}.key"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        key_path.chmod(0o600)
        return cert_path, key_path

    def crl(self, name="ca", *, revoked=(), expired=False):
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateRevocationListBuilder().issuer_name(self.ca.subject)
                   .last_update(now - timedelta(days=2 if expired else 1))
                   .next_update(now - timedelta(days=1) if expired else now + timedelta(days=1)))
        for certificate in revoked:
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder().serial_number(certificate.serial_number)
                .revocation_date(now - timedelta(minutes=1)).build()
            )
        crl = builder.sign(self.ca_key, hashes.SHA256())
        path = self.root / f"{name}.crl.pem"
        path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))
        return path


def telemetry(agent="vm1-satellite", role="SATELLITE", event_id="sat-1", frame="frame-1", run=RUN):
    now = datetime.now(timezone.utc).isoformat()
    if role == "SATELLITE":
        event = {"schema_version": "1.0.0", "run_id": run, "event_time": now,
                 "scenario": "baseline", "event_id": event_id,
                 "event_type": "satellite.telemetry_transmitted", "satellite_id": "SAT-001",
                 "session_id": "s1", "sequence_number": 1}
    else:
        event = {"schema_version": "1.0.0", "run_id": run, "event_time": now,
                 "scenario": "baseline", "event_id": event_id, "event_type": "channel.rf_observed",
                 "iq_bytes": 4, "power_db_rel": None, "evm_percent": None,
                 "estimated_gain": None, "residual_spectral_peak_db": None}
    return {"contract_version": "1.0.0", "identity": {"agent_id": agent, "role": role,
            "software_version": "test", "contract_version": "1.0.0"}, "frame_id": frame, "event": event}


def request(payload, request_id, kind="telemetry", sent_at=None):
    return {"protocol_version": "1.0", "request_id": request_id,
            "sent_at": sent_at or datetime.now(timezone.utc).isoformat(), "kind": kind, "payload": payload}


class AgentTLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.pki = TestPKI(root)
        self.server_cert, self.server_key = self.pki.leaf("vm2-station", server=True)
        self.sat_cert, self.sat_key = self.pki.leaf("vm1-satellite")
        self.channel_cert, self.channel_key = self.pki.leaf("vm3-channel")
        self.unknown_cert, self.unknown_key = self.pki.leaf("unknown-agent")
        self.expired_cert, self.expired_key = self.pki.leaf("expired-agent", expired=True)
        self.db_path = root / "mission.sqlite3"
        self.db = MissionControlDB(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO runs VALUES (?, 'baseline', 'LIVE', 'ACTIVE', ?, NULL, ?)",
                       (RUN, datetime.now(timezone.utc).isoformat(), "TLS test"))
        self.broker = EventBroker(self.db)
        self.adapter = AgentIngestionAdapter(self.broker)
        files = TLSFiles(self.pki.ca_path, self.server_cert, self.server_key)
        self.server = AgentTLSServer(self.adapter, create_server_ssl_context(files), policy=CertificatePolicy())
        _, self.port = await self.server.start("127.0.0.1", 0)

    async def asyncTearDown(self):
        await self.server.close()
        self.temp.cleanup()

    def client_context(self, cert=None, key=None, ca=None):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca or self.pki.ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        if cert:
            context.load_cert_chain(str(cert), str(key))
        return context

    async def send(self, value, cert=None, key=None, *, raw_size=None):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.client_context(cert, key), server_hostname="vm2-station")
        encoded = json.dumps(value, separators=(",", ":")).encode()
        writer.write(struct.pack("!I", raw_size if raw_size is not None else len(encoded)))
        if raw_size is None:
            writer.write(encoded)
        await writer.drain()
        try:
            size = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 2))[0]
            response = json.loads(await reader.readexactly(size))
        finally:
            writer.close()
            await writer.wait_closed()
        return response

    async def test_valid_satellite_and_channel_certificates_and_actions(self):
        result = await self.send(request(telemetry(), "sat-request"), self.sat_cert, self.sat_key)
        self.assertEqual(result["status"], "accepted")
        channel = telemetry("vm3-channel", "CHANNEL", "rf-1")
        result = await self.send(request(channel, "channel-request"), self.channel_cert, self.channel_key)
        self.assertEqual(result["status"], "accepted")
        action = {"message_type": "channel.action", "contract_version": "1.0.0", "run_id": RUN,
                  "frame_id": "frame-1", "event_id": "action-1", "event_time": datetime.now(timezone.utc).isoformat(),
                  "scenario": "baseline", "disposition": "ATTACK", "attack": "JAMMING"}
        result = await self.send(request(action, "action-request", "channel_action"), self.channel_cert, self.channel_key)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.db.list_events(RUN)[-1]["event_type"], "channel.action")

    async def test_unknown_identity_and_forged_identity_field_fail_before_persistence(self):
        result = await self.send(request(telemetry(), "unknown"), self.unknown_cert, self.unknown_key)
        self.assertEqual(result["status"], "rejected")
        forged = request(telemetry(), "forged")
        forged["x-client-cert"] = "vm1-satellite"
        result = await self.send(forged, self.sat_cert, self.sat_key)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.db.list_events(RUN), [])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_requests").fetchone()[0], 0)

    async def test_incorrect_certificate_identity_binding_and_remote_station_fail(self):
        result = await self.send(request(telemetry(), "wrong-cert"), self.channel_cert, self.channel_key)
        self.assertEqual(result["status"], "rejected")
        station = telemetry()
        station["identity"] = {"agent_id": "vm2-station", "role": "STATION", "software_version": "x", "contract_version": "1.0.0"}
        result = await self.send(request(station, "remote-station"), self.sat_cert, self.sat_key)
        self.assertEqual(result["status"], "rejected")

    async def test_replay_stale_oversized_invalid_contract_and_cross_run(self):
        valid = request(telemetry(), "replay-id")
        self.assertEqual((await self.send(valid, self.sat_cert, self.sat_key))["status"], "accepted")
        self.assertEqual((await self.send(valid, self.sat_cert, self.sat_key))["status"], "rejected")
        stale = request(telemetry(event_id="old", frame="old"), "stale", sent_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        self.assertEqual((await self.send(stale, self.sat_cert, self.sat_key))["status"], "rejected")
        self.assertEqual((await self.send({}, self.sat_cert, self.sat_key, raw_size=65537))["status"], "rejected")
        invalid = request(telemetry(event_id="bad", frame="bad"), "invalid-contract")
        invalid["payload"]["contract_version"] = "2.0.0"
        self.assertEqual((await self.send(invalid, self.sat_cert, self.sat_key))["status"], "rejected")
        protocol = request(telemetry(event_id="proto", frame="proto"), "invalid-protocol")
        protocol["protocol_version"] = "2.0"
        self.assertEqual((await self.send(protocol, self.sat_cert, self.sat_key))["status"], "rejected")
        cross = request(telemetry(event_id="cross", frame="cross", run="8"*32), "cross-run")
        self.assertEqual((await self.send(cross, self.sat_cert, self.sat_key))["status"], "rejected")

    async def test_replay_survives_transport_restart_and_reconnect(self):
        value = request(telemetry(), "persistent-replay")
        self.assertEqual((await self.send(value, self.sat_cert, self.sat_key))["status"], "accepted")
        await self.server.close()
        files = TLSFiles(self.pki.ca_path, self.server_cert, self.server_key)
        self.server = AgentTLSServer(AgentIngestionAdapter(EventBroker(MissionControlDB(self.db_path))), create_server_ssl_context(files), policy=CertificatePolicy())
        _, self.port = await self.server.start("127.0.0.1", 0)
        self.assertEqual((await self.send(value, self.sat_cert, self.sat_key))["status"], "rejected")

    async def test_missing_expired_and_untrusted_client_certificates_fail_handshake(self):
        for cert, key in ((None, None), (self.expired_cert, self.expired_key)):
            with self.assertRaises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError)):
                await self.send(request(telemetry(), "handshake"), cert, key)
        other_root = Path(self.temp.name) / "other"
        other_root.mkdir()
        other = TestPKI(other_root)
        bad_cert, bad_key = other.leaf("vm1-satellite")
        with self.assertRaises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError)):
            await self.send(request(telemetry(), "untrusted"), bad_cert, bad_key)

    async def test_persistent_revocation_policy_and_rate_limit(self):
        await self.server.close()
        cert = x509.load_pem_x509_certificate(self.sat_cert.read_bytes())
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        policy_path = Path(self.temp.name) / "revocations.json"
        policy_path.write_text(json.dumps({"version": 1, "revoked_sha256": [fingerprint]}))
        files = TLSFiles(self.pki.ca_path, self.server_cert, self.server_key)
        self.server = AgentTLSServer(
            self.adapter, create_server_ssl_context(files),
            policy=PersistentCertificatePolicy(policy_path), requests_per_minute=1,
        )
        _, self.port = await self.server.start("127.0.0.1", 0)
        self.assertEqual((await self.send(request(telemetry(), "revoked"), self.sat_cert, self.sat_key))["status"], "rejected")
        policy_path.write_text(json.dumps({"version": 1, "revoked_sha256": []}))
        self.assertEqual((await self.send(request(telemetry(), "rate-1"), self.sat_cert, self.sat_key))["status"], "accepted")
        self.assertEqual((await self.send(request(telemetry(event_id="sat-2", frame="frame-2"), "rate-2"), self.sat_cert, self.sat_key))["status"], "rejected")
        policy_path.unlink()
        self.assertEqual((await self.send(request(telemetry(event_id="sat-3", frame="frame-3"), "missing-policy"), self.channel_cert, self.channel_key))["status"], "rejected")

    def test_missing_revocation_policy_fails_closed(self):
        with self.assertRaises(TransportError):
            PersistentCertificatePolicy(Path(self.temp.name) / "missing-policy.json")


if __name__ == "__main__":
    unittest.main()
