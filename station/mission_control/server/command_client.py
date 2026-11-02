"""Disabled-by-default Station mTLS client for fixed GroundLink commands."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import ssl
import struct
from typing import Awaitable, Callable
import uuid

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID

from mission_control.agent_contract import AgentId, Command, PrepareCommand, StartCommand, StopCommand
from mission_control.command_protocol import (
    MAX_COMMAND_WIRE_BYTES, CommandAcknowledgement, CommandRequest,
    command_ack_from_dict, correlate_ack,
)
from .coordination import CommandAck, CoordinationError


class CommandTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandTLSConfig:
    ca_certificate: Path
    certificate_revocation_list: Path
    station_certificate: Path
    station_private_key: Path
    connect_timeout: float = 3.0
    io_timeout: float = 3.0
    retries: int = 1


def create_command_client_context(config: CommandTLSConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(config.ca_certificate))
    # OpenSSL treats a missing CRL as acceptable unless CRL checking is explicit.
    # Loading or parsing failure raises here, before any connection is attempted.
    context.load_verify_locations(cafile=str(config.certificate_revocation_list))
    context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(str(config.station_certificate), str(config.station_private_key))
    return context


class StationCommandClient:
    def __init__(self, host: str, port: int, expected_agent_id: str, config: CommandTLSConfig):
        if expected_agent_id not in {AgentId.SATELLITE.value, AgentId.CHANNEL.value}:
            raise ValueError("invalid remote command agent")
        if config.retries < 0 or config.retries > 2:
            raise ValueError("command retries must be between zero and two")
        self.host, self.port, self.expected_agent_id, self.config = host, port, expected_agent_id, config
        self.ssl_context = create_command_client_context(config)

    async def exchange(self, request: CommandRequest) -> CommandAcknowledgement:
        if request.target_agent_id != self.expected_agent_id:
            raise CommandTransportError("command target does not match TLS endpoint")
        encoded = json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAX_COMMAND_WIRE_BYTES:
            raise CommandTransportError("command request exceeds size limit")
        last: BaseException | None = None
        for attempt in range(self.config.retries + 1):
            try:
                return await self._exchange_once(encoded, request)
            except (OSError, ssl.SSLError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                last = exc
                if attempt == self.config.retries:
                    break
        raise CommandTransportError("command transport failed") from last

    async def _exchange_once(self, encoded: bytes, request: CommandRequest) -> CommandAcknowledgement:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.host, self.port, ssl=self.ssl_context,
                server_hostname=self.expected_agent_id, ssl_handshake_timeout=self.config.connect_timeout,
            ), self.config.connect_timeout,
        )
        try:
            _verify_exact_server_identity(writer, self.expected_agent_id)
            writer.write(struct.pack("!I", len(encoded)) + encoded)
            await asyncio.wait_for(writer.drain(), self.config.io_timeout)
            header = await asyncio.wait_for(reader.readexactly(4), self.config.io_timeout)
            size = struct.unpack("!I", header)[0]
            if size <= 0 or size > MAX_COMMAND_WIRE_BYTES:
                raise CommandTransportError("command response exceeds size limit")
            raw = await asyncio.wait_for(reader.readexactly(size), self.config.io_timeout)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CommandTransportError("malformed command response") from exc
            ack = command_ack_from_dict(value)
            correlate_ack(request, ack)
            return ack
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass


def _verify_exact_server_identity(writer: asyncio.StreamWriter, expected_agent_id: str) -> None:
    """Apply the GroundLink leaf profile after OpenSSL chain/CRL verification."""
    ssl_object = writer.get_extra_info("ssl_object")
    der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not der:
        raise CommandTransportError("verified server certificate is unavailable")
    try:
        certificate = x509.load_der_x509_certificate(der)
        san = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        dns_names = san.get_values_for_type(x509.DNSName)
        eku = certificate.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
        usage = certificate.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value
    except (ValueError, x509.ExtensionNotFound) as exc:
        raise CommandTransportError("server certificate profile is incomplete") from exc
    if dns_names != [expected_agent_id]:
        raise CommandTransportError("server certificate must contain exactly one expected DNS SAN")
    if set(eku) != {ExtendedKeyUsageOID.SERVER_AUTH}:
        raise CommandTransportError("server certificate EKU is not serverAuth-only")
    if not usage.digital_signature or usage.key_cert_sign or usage.crl_sign:
        raise CommandTransportError("server certificate key usage is invalid")
    public_key = certificate.public_key()
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if public_key.curve.key_size < 256:
            raise CommandTransportError("server certificate EC key is too small")
    elif isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < 3072:
            raise CommandTransportError("server certificate RSA key is too small")
    else:
        raise CommandTransportError("server certificate key algorithm is not approved")


ReadinessWaiter = Callable[[str, str, float], Awaitable[None]]


class SecureAgentControl:
    """AgentControl adapter; process readiness must come from an external observer."""

    def __init__(self, client: StationCommandClient, readiness_waiter: ReadinessWaiter | None = None):
        self.client = client
        self.readiness_waiter = readiness_waiter

    async def send(self, command: Command) -> CommandAck:
        now = datetime.now(timezone.utc)
        request = CommandRequest(
            uuid.uuid4().hex, now.isoformat(),
            (now + timedelta(seconds=self.client.config.io_timeout)).isoformat(),
            self.client.expected_agent_id, command,
        )
        ack = await self.client.exchange(request)
        return CommandAck(ack.command_id, ack.accepted, ack.state, ack.preparation_id, ack.reason)

    async def prepare(self, command: PrepareCommand) -> CommandAck:
        return await self.send(command)

    async def start(self, command: StartCommand) -> CommandAck:
        return await self.send(command)

    async def stop(self, command: StopCommand) -> CommandAck:
        return await self.send(command)

    async def wait_for_state(self, state: str, run_id: str, timeout: float) -> None:
        if self.readiness_waiter is None:
            raise CoordinationError("authenticated process readiness observer is unavailable")
        await self.readiness_waiter(state, run_id, timeout)
