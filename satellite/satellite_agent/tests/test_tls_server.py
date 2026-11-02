import asyncio
import json
from pathlib import Path
import shutil
import ssl
import struct
import subprocess
import tempfile
import unittest

from satellite_agent.agent import SatelliteAgent
from satellite_agent.command_protocol import MAX_COMMAND_WIRE_BYTES
from satellite_agent.persistence import DurableCommandProcessor
from satellite_agent.tls_server import (
    ServerConfig, SatelliteCommandServer, build_server_ssl_context,
)
from satellite_agent.transmitter import OfflineTransmitterAdapter


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for temporary TLS fixtures")
class TLSCommandServerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls._make_pki()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_ssl(cls, *args):
        subprocess.run(["openssl", *args], cwd=cls.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def _make_pki(cls):
        cls.run_ssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=Test CA", "-keyout", "ca.key", "-out", "ca.crt")
        for name, san, ca_prefix in (
            ("server", "vm1-satellite", "ca"),
            ("station", "vm2-station", "ca"),
            ("wrong", "vm3-channel", "ca"),
        ):
            cls.run_ssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={san}",
                        "-addext", f"subjectAltName=DNS:{san}",
                        "-keyout", f"{name}.key", "-out", f"{name}.csr")
            cls.run_ssl("x509", "-req", "-days", "2", "-in", f"{name}.csr",
                        "-CA", f"{ca_prefix}.crt", "-CAkey", f"{ca_prefix}.key", "-CAcreateserial",
                        "-copy_extensions", "copy", "-out", f"{name}.crt")
        cls.run_ssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=Other CA", "-keyout", "otherca.key", "-out", "otherca.crt")
        cls.run_ssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=vm2-station",
                    "-addext", "subjectAltName=DNS:vm2-station", "-keyout", "untrusted.key", "-out", "untrusted.csr")
        cls.run_ssl("x509", "-req", "-days", "2", "-in", "untrusted.csr", "-CA", "otherca.crt",
                    "-CAkey", "otherca.key", "-CAcreateserial", "-copy_extensions", "copy", "-out", "untrusted.crt")
        # Empty CRL from the trusted CA.
        (cls.root / "index.txt").write_text("")
        (cls.root / "serial").write_text("1000\n")
        (cls.root / "crlnumber").write_text("1000\n")
        (cls.root / "openssl.cnf").write_text(
            "[ca]\ndefault_ca=x\n[x]\ndatabase=index.txt\nserial=serial\ncrlnumber=crlnumber\n"
            "private_key=ca.key\ncertificate=ca.crt\ndefault_md=sha256\ndefault_crl_days=2\n"
            "default_days=2\nnew_certs_dir=.\npolicy=policy\ncopy_extensions=copy\nunique_subject=no\n"
            "[policy]\ncommonName=supplied\n"
        )
        for name in ("expired", "revoked"):
            cls.run_ssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=vm2-station",
                        "-addext", "subjectAltName=DNS:vm2-station",
                        "-keyout", f"{name}.key", "-out", f"{name}.csr")
        cls.run_ssl("ca", "-batch", "-config", "openssl.cnf", "-in", "expired.csr",
                    "-startdate", "20200101000000Z", "-enddate", "20200102000000Z", "-out", "expired.crt")
        cls.run_ssl("ca", "-batch", "-config", "openssl.cnf", "-in", "revoked.csr", "-out", "revoked.crt")
        cls.run_ssl("ca", "-batch", "-config", "openssl.cnf", "-revoke", "revoked.crt")
        cls.run_ssl("ca", "-gencrl", "-config", "openssl.cnf", "-out", "ca.crl")

    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.old_exception_handler = self.loop.get_exception_handler()
        def expected_tls_handler(loop, context):
            if isinstance(context.get("exception"), ssl.SSLError):
                return
            loop.default_exception_handler(context)
        self.loop.set_exception_handler(expected_tls_handler)
        tx = OfflineTransmitterAdapter()
        self.processor = DurableCommandProcessor(
            self.root / f"state-{self._testMethodName}.sqlite3", SatelliteAgent(tx)
        )
        context = build_server_ssl_context(
            certificate=self.root / "server.crt", private_key=self.root / "server.key",
            trust_ca=self.root / "ca.crt", crl=self.root / "ca.crl",
        )
        self.server = SatelliteCommandServer(
            ServerConfig(enabled=True, host="127.0.0.1", port=0, request_timeout=0.3),
            context, self.processor,
        )
        await self.server.start()
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.close()
        self.processor.close()
        self.loop.set_exception_handler(self.old_exception_handler)

    def client_context(self, cert=None):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.root / "ca.crt"))
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        if cert:
            context.load_cert_chain(str(self.root / f"{cert}.crt"), str(self.root / f"{cert}.key"))
        return context

    def request(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        return {
            "protocol_version": "1.0", "request_id": "tls-prepare",
            "sent_at": stamp, "deadline_at": (now + timedelta(seconds=2)).isoformat(),
            "target_agent_id": "vm1-satellite",
            "command": {"contract_version": "1.0.0", "command_type": "PREPARE",
                        "command_id": "tls-prepare-command", "run_id": "2" * 32,
                        "scenario": "baseline", "issued_at": stamp},
        }

    async def exchange(self, context, payload):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=context, server_hostname="vm1-satellite"
        )
        try:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            writer.write(struct.pack("!I", len(raw)) + raw)
            await writer.drain()
            prefix = await reader.readexactly(4)
            body = await reader.readexactly(struct.unpack("!I", prefix)[0])
            return json.loads(body)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def test_correct_station_certificate(self):
        ack = await self.exchange(self.client_context("station"), self.request())
        self.assertTrue(ack["accepted"])
        self.assertEqual(ack["agent_id"], "vm1-satellite")

    async def test_wrong_identity_is_closed(self):
        with self.assertRaises((asyncio.IncompleteReadError, ConnectionResetError)):
            await self.exchange(self.client_context("wrong"), self.request())

    async def test_missing_expired_revoked_and_untrusted_certificates_fail_handshake(self):
        for cert in (None, "expired", "revoked", "untrusted"):
            with self.assertRaises((ssl.SSLError, ConnectionResetError, asyncio.IncompleteReadError)):
                await self.exchange(self.client_context(cert), self.request())

    async def test_invalid_version_is_closed(self):
        payload = self.request(); payload["protocol_version"] = "9.0"
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.exchange(self.client_context("station"), payload)

    async def test_forged_identity_field_is_closed(self):
        payload = self.request(); payload["station_identity"] = "vm2-station"
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.exchange(self.client_context("station"), payload)

    async def test_oversized_and_timeout_connections_are_closed(self):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.client_context("station"), server_hostname="vm1-satellite"
        )
        writer.write(struct.pack("!I", MAX_COMMAND_WIRE_BYTES + 1)); await writer.drain()
        self.assertEqual(await reader.read(), b"")
        writer.close(); await writer.wait_closed()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.client_context("station"), server_hostname="vm1-satellite"
        )
        await asyncio.sleep(0.5)
        self.assertEqual(await reader.read(), b"")
        writer.close(); await writer.wait_closed()

    async def test_disabled_by_default(self):
        disabled = SatelliteCommandServer(ServerConfig(), self.server.ssl_context, self.processor)
        with self.assertRaises(RuntimeError):
            await disabled.start()


if __name__ == "__main__":
    unittest.main()
