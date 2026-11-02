import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import ssl
import struct
import tempfile
import unittest
import jsonschema
from cryptography import x509

from mission_control.agent_contract import PrepareCommand, StartCommand, StopCommand, ValidationError
from mission_control.command_protocol import (
    MAX_COMMAND_WIRE_BYTES, CommandAcknowledgement, CommandRequest,
    command_ack_from_dict, command_request_from_dict,
)
from mission_control.server.agent_tls import TLSFiles, create_server_ssl_context
from mission_control.server.command_client import (
    CommandTLSConfig, CommandTransportError, SecureAgentControl, StationCommandClient,
)
from mission_control.server.coordination import LiveCoordinator
from mission_control.tests.test_agent_tls import TestPKI
from mission_control.tests.test_coordination import FakeControl, fill_registry
from mission_control.server.coordination import ReadinessRegistry
from mission_control.tests.tls_command_server import TestCommandAgentServer


RUN = "6" * 32


def now():
    return datetime.now(timezone.utc).isoformat()


class CommandProtocolTests(unittest.TestCase):
    def test_strict_request_and_ack_round_trip(self):
        command = PrepareCommand("cmd-1", RUN, "baseline", now())
        request = CommandRequest("req-1", now(), (datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat(), "vm1-satellite", command)
        self.assertEqual(command_request_from_dict(request.to_dict()), request)
        ack = CommandAcknowledgement("req-1", "vm1-satellite", "cmd-1", "PREPARE", RUN,
                                     "baseline", True, "PREPARED", "prep-1", None, now())
        self.assertEqual(command_ack_from_dict(ack.to_dict()), ack)
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "command-transport-v1.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(request.to_dict())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(ack.to_dict())
        bad = request.to_dict(); bad["argv"] = ["unsafe"]
        with self.assertRaises(ValidationError):
            command_request_from_dict(bad)
        bad = request.to_dict(); bad["protocol_version"] = "2.0"
        with self.assertRaises(ValidationError):
            command_request_from_dict(bad)
        bad = request.to_dict(); bad["command"]["command_type"] = "EXEC"
        with self.assertRaises(ValidationError):
            command_request_from_dict(bad)


class CommandTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.pki = TestPKI(root)
        self.crl = self.pki.crl()
        self.station_cert, self.station_key = self.pki.leaf("vm2-station")
        self.sat_cert, self.sat_key = self.pki.leaf("vm1-satellite", server=True)
        self.channel_cert, self.channel_key = self.pki.leaf("vm3-channel", server=True)
        self.wrong_cert, self.wrong_key = self.pki.leaf("wrong-station")
        self.expired_cert, self.expired_key = self.pki.leaf("expired-station", expired=True)
        self.sat_server = TestCommandAgentServer(
            "vm1-satellite", create_server_ssl_context(TLSFiles(self.pki.ca_path, self.sat_cert, self.sat_key)))
        self.channel_server = TestCommandAgentServer(
            "vm3-channel", create_server_ssl_context(TLSFiles(self.pki.ca_path, self.channel_cert, self.channel_key)))
        self.sat_port = await self.sat_server.start()
        self.channel_port = await self.channel_server.start()
        self.config = CommandTLSConfig(self.pki.ca_path, self.crl, self.station_cert, self.station_key,
                                       connect_timeout=.3, io_timeout=.2, retries=0)
        self.sat_client = StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", self.config)
        self.channel_client = StationCommandClient("127.0.0.1", self.channel_port, "vm3-channel", self.config)

    async def asyncTearDown(self):
        await self.sat_server.close()
        await self.channel_server.close()
        self.temp.cleanup()

    def request_for(self, command, target="vm1-satellite", request_id="req-1"):
        current = datetime.now(timezone.utc)
        return CommandRequest(request_id, current.isoformat(), (current+timedelta(seconds=1)).isoformat(), target, command)

    async def test_valid_satellite_channel_and_duplicate_idempotency(self):
        sat = PrepareCommand("sat-prepare", RUN, "baseline", now())
        request = self.request_for(sat)
        first = await self.sat_client.exchange(request)
        second = await self.sat_client.exchange(request)
        self.assertEqual(first, second)
        third = await self.sat_client.exchange(self.request_for(sat, request_id="req-2"))
        self.assertEqual((third.command_id, third.state), (first.command_id, first.state))
        self.assertEqual(self.sat_server.requests, ["PREPARE"])
        channel = PrepareCommand("ch-prepare", RUN, "baseline", now())
        ack = await self.channel_client.exchange(self.request_for(channel, "vm3-channel", "req-ch"))
        self.assertEqual((ack.agent_id, ack.state), ("vm3-channel", "PREPARED"))

    async def test_start_preparation_run_scenario_and_command_collision(self):
        prepare = PrepareCommand("prepare", RUN, "baseline", now())
        ack = await self.sat_client.exchange(self.request_for(prepare))
        wrong = StartCommand("wrong-prep", RUN, "baseline", now(), "not-the-id")
        rejected = await self.sat_client.exchange(self.request_for(wrong, request_id="wrong-prep-req"))
        self.assertFalse(rejected.accepted)
        wrong_scenario = StartCommand("wrong-scenario", RUN, "jamming", now(), ack.preparation_id)
        self.assertFalse((await self.sat_client.exchange(
            self.request_for(wrong_scenario, request_id="wrong-scenario-req")
        )).accepted)
        wrong_run = StopCommand("wrong-run", "5"*32, "baseline", now())
        self.assertFalse((await self.sat_client.exchange(self.request_for(wrong_run, request_id="wrong-run-req"))).accepted)
        collision = PrepareCommand("prepare", RUN, "jamming", now())
        with self.assertRaises(CommandTransportError):
            await self.sat_client.exchange(self.request_for(collision, request_id="collision"))
        start = StartCommand("start", RUN, "baseline", now(), ack.preparation_id)
        accepted = await self.sat_client.exchange(self.request_for(start, request_id="start-req"))
        self.assertTrue(accepted.accepted)

    async def test_wrong_server_identity_wrong_client_and_untrusted_expired(self):
        command = PrepareCommand("identity", RUN, "baseline", now())
        wrong_server = StationCommandClient("127.0.0.1", self.sat_port, "vm3-channel", self.config)
        with self.assertRaises(CommandTransportError):
            await wrong_server.exchange(self.request_for(command, "vm3-channel"))
        for cert, key in ((self.wrong_cert, self.wrong_key), (self.expired_cert, self.expired_key)):
            config = CommandTLSConfig(self.pki.ca_path, self.crl, cert, key, connect_timeout=.2, io_timeout=.2, retries=0)
            client = StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", config)
            with self.assertRaises(CommandTransportError):
                await client.exchange(self.request_for(command))
        other_root = Path(self.temp.name) / "other"; other_root.mkdir()
        other = TestPKI(other_root)
        config = CommandTLSConfig(other.ca_path, other.crl(), self.station_cert, self.station_key, connect_timeout=.2, io_timeout=.2, retries=0)
        client = StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", config)
        with self.assertRaises(CommandTransportError):
            await client.exchange(self.request_for(command))

    async def test_revoked_server_and_missing_invalid_expired_crl_fail_closed(self):
        command = PrepareCommand("revocation", RUN, "baseline", now())
        satellite = x509.load_pem_x509_certificate(self.sat_cert.read_bytes())
        revoked_crl = self.pki.crl("revoked", revoked=(satellite,))
        config = CommandTLSConfig(self.pki.ca_path, revoked_crl, self.station_cert, self.station_key,
                                  connect_timeout=.2, io_timeout=.2, retries=0)
        with self.assertRaises(CommandTransportError):
            await StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", config).exchange(
                self.request_for(command)
            )
        expired_crl = self.pki.crl("expired", expired=True)
        config = CommandTLSConfig(self.pki.ca_path, expired_crl, self.station_cert, self.station_key,
                                  connect_timeout=.2, io_timeout=.2, retries=0)
        with self.assertRaises(CommandTransportError):
            await StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", config).exchange(
                self.request_for(command)
            )
        with self.assertRaises((OSError, ssl.SSLError)):
            StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", CommandTLSConfig(
                self.pki.ca_path, Path(self.temp.name) / "missing.crl", self.station_cert, self.station_key
            ))
        invalid = Path(self.temp.name) / "invalid.crl"; invalid.write_text("not a CRL")
        with self.assertRaises((OSError, ssl.SSLError)):
            StationCommandClient("127.0.0.1", self.sat_port, "vm1-satellite", CommandTLSConfig(
                self.pki.ca_path, invalid, self.station_cert, self.station_key
            ))

    async def test_incorrect_eku_and_ambiguous_san_are_rejected(self):
        command = PrepareCommand("profile", RUN, "baseline", now())
        bad_eku_cert, bad_eku_key = self.pki.leaf(
            "vm1-satellite", server=False, file_tag="bad-eku-server"
        )
        ambiguous_cert, ambiguous_key = self.pki.leaf(
            "vm1-satellite", server=True,
            dns_names=["vm1-satellite", "extra.invalid"], file_tag="ambiguous-server",
        )
        for certificate, key in ((bad_eku_cert, bad_eku_key), (ambiguous_cert, ambiguous_key)):
            server = TestCommandAgentServer(
                "vm1-satellite",
                create_server_ssl_context(TLSFiles(self.pki.ca_path, certificate, key)),
            )
            port = await server.start()
            try:
                client = StationCommandClient("127.0.0.1", port, "vm1-satellite", self.config)
                with self.assertRaises(CommandTransportError):
                    await client.exchange(self.request_for(command))
            finally:
                await server.close()

    async def test_plaintext_server_cannot_trigger_downgrade(self):
        async def plaintext(reader, writer):
            writer.write(b"plaintext-not-tls")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(plaintext, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = StationCommandClient("127.0.0.1", port, "vm1-satellite", self.config)
            with self.assertRaises(CommandTransportError):
                await client.exchange(self.request_for(PrepareCommand("plain", RUN, "baseline", now())))
        finally:
            server.close(); await server.wait_closed()

    async def test_invalid_ack_timeout_oversized_response_and_reconnect(self):
        command = PrepareCommand("reliability", RUN, "baseline", now())
        self.sat_server.invalid_ack = True
        with self.assertRaises(ValidationError):
            await self.sat_client.exchange(self.request_for(command))
        self.sat_server.invalid_ack = False
        self.sat_server.delay = .4
        with self.assertRaises(CommandTransportError):
            await self.sat_client.exchange(self.request_for(PrepareCommand("timeout", RUN, "baseline", now()), request_id="timeout"))
        self.sat_server.delay = 0
        await asyncio.sleep(.25)  # let the injected slow command leave the server lock
        self.sat_server.oversized_response = True
        with self.assertRaises(CommandTransportError):
            await self.sat_client.exchange(self.request_for(PrepareCommand("oversize", RUN, "baseline", now()), request_id="oversize"))
        self.sat_server.oversized_response = False
        # A fresh TLS connection still works after failures/disconnects.
        stop = StopCommand("reconnect", RUN, "baseline", now())
        self.assertTrue((await self.sat_client.exchange(self.request_for(stop, request_id="reconnect"))).accepted)

    async def test_secure_controls_integrate_with_coordinator_order(self):
        registry = ReadinessRegistry(); fill_registry(registry)
        observed = []
        async def readiness(state, run_id, timeout):
            observed.append((state, run_id))
        sat = SecureAgentControl(self.sat_client, readiness)
        channel = SecureAgentControl(self.channel_client, readiness)
        local_log = []
        local_station = FakeControl("vm2-station", local_log)
        coordinator = LiveCoordinator(registry, {
            "vm2-station": local_station, "vm3-channel": channel, "vm1-satellite": sat
        }, timeout=.5)
        run = await coordinator.prepare("baseline")
        await coordinator.start(run.run_id)
        await coordinator.stop(run.run_id)
        self.assertEqual(self.channel_server.requests, ["PREPARE", "START", "STOP"])
        self.assertEqual(self.sat_server.requests, ["PREPARE", "START", "STOP"])
        self.assertEqual(local_log, [("PREPARE", "vm2-station"), ("START", "vm2-station"), ("STOP", "vm2-station")])
        self.assertEqual(len(observed), 2)

    async def test_missing_client_certificate_and_oversized_request_rejected(self):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.pki.ca_path))
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        with self.assertRaises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError)):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.sat_port, ssl=context, server_hostname="vm1-satellite")
            writer.write(struct.pack("!I", 1)); await writer.drain()
            await reader.readexactly(1)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.sat_port, ssl=self.sat_client.ssl_context, server_hostname="vm1-satellite")
        writer.write(struct.pack("!I", MAX_COMMAND_WIRE_BYTES + 1)); await writer.drain()
        self.assertEqual(await reader.read(), b"")
        writer.close(); await writer.wait_closed()

    async def test_concurrent_command_is_rejected(self):
        self.sat_server.delay = .12
        first_command = PrepareCommand("concurrent-1", RUN, "baseline", now())
        first = asyncio.create_task(self.sat_client.exchange(self.request_for(first_command, request_id="concurrent-1")))
        await asyncio.sleep(.03)
        second_command = PrepareCommand("concurrent-2", RUN, "baseline", now())
        with self.assertRaises(CommandTransportError):
            await self.sat_client.exchange(self.request_for(second_command, request_id="concurrent-2"))
        self.assertTrue((await first).accepted)


if __name__ == "__main__":
    unittest.main()
