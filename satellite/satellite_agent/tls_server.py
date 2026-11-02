"""Disabled-by-default bounded TLS 1.3 Satellite command server."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import ssl
import struct
import time

from .agent import AGENT_ID, StateError
from .command_protocol import MAX_COMMAND_WIRE_BYTES, strict_request_from_dict
from .persistence import CommandExpired, DurableCommandProcessor

STATION_ID = "vm2-station"


@dataclass(frozen=True)
class ServerConfig:
    enabled: bool = False
    host: str = "10.10.3.10"
    port: int = 7443
    max_connections: int = 16
    max_connections_per_minute: int = 60
    max_requests_per_minute: int = 120
    handshake_timeout: float = 5.0
    request_timeout: float = 3.0
    write_timeout: float = 3.0


class AuditLogger:
    """Emits bounded metadata only; never commands, credentials, or certificate bodies."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("satellite_agent.audit")

    def emit(self, event: str, **fields) -> None:
        safe = {key: str(value)[:128] for key, value in fields.items() if value is not None}
        self.logger.info(json.dumps({"event": event, **safe}, sort_keys=True))


def _exact_dns_identity(cert: dict, expected: str) -> None:
    sans = cert.get("subjectAltName", ())
    if tuple(sans) != (("DNS", expected),):
        raise ssl.SSLCertVerificationError("certificate must contain exactly the expected DNS SAN")


def build_server_ssl_context(
    *, certificate: Path, private_key: Path, trust_ca: Path, crl: Path
) -> ssl.SSLContext:
    """Build strict production context. Paths are consumed by OpenSSL, not application code."""
    decoded = ssl._ssl._test_decode_cert(str(certificate))  # type: ignore[attr-defined]
    _exact_dns_identity(decoded, AGENT_ID)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
    context.load_verify_locations(cafile=str(trust_ca))
    context.load_verify_locations(cafile=str(crl))
    context.verify_flags |= ssl.VERIFY_CRL_CHECK_CHAIN
    return context


def _json_no_duplicates(raw: bytes) -> dict:
    def hook(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=hook)
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


class SatelliteCommandServer:
    def __init__(
        self,
        config: ServerConfig,
        ssl_context: ssl.SSLContext,
        processor: DurableCommandProcessor,
        *,
        audit: AuditLogger | None = None,
    ) -> None:
        self.config = config
        self.ssl_context = ssl_context
        self.processor = processor
        self.audit = audit or AuditLogger()
        self._server: asyncio.Server | None = None
        self._slots = asyncio.Semaphore(config.max_connections)
        self._connection_times: dict[str, deque[float]] = defaultdict(deque)
        self._request_times: dict[str, deque[float]] = defaultdict(deque)

    async def start(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("production command listener is disabled")
        if self.config.host != "127.0.0.1" and self.config.host != "10.10.3.10":
            raise ValueError("listener host is not an approved Satellite address")
        self._server = await asyncio.start_server(
            self._handle,
            self.config.host,
            self.config.port,
            ssl=self.ssl_context,
            ssl_handshake_timeout=self.config.handshake_timeout,
            limit=MAX_COMMAND_WIRE_BYTES + 4,
            start_serving=True,
        )

    @property
    def sockets(self):
        return () if self._server is None else tuple(self._server.sockets or ())

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _allow(bucket: deque[float], limit: int, now: float) -> bool:
        while bucket and bucket[0] <= now - 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) and peer else "unknown"
        if self._slots.locked():
            self.audit.emit("connection_rejected", peer=peer_ip, reason="connection_limit")
            writer.close()
            return
        async with self._slots:
            now = time.monotonic()
            if not self._allow(self._connection_times[peer_ip], self.config.max_connections_per_minute, now):
                self.audit.emit("connection_rejected", peer=peer_ip, reason="connection_rate")
                writer.close()
                return
            ssl_object = writer.get_extra_info("ssl_object")
            try:
                if ssl_object is None or ssl_object.version() != "TLSv1.3":
                    raise ssl.SSLError("TLS 1.3 required")
                cert = ssl_object.getpeercert()
                _exact_dns_identity(cert, STATION_ID)
                if not self._allow(self._request_times[peer_ip], self.config.max_requests_per_minute, now):
                    raise PermissionError("request rate limit")
                ack = await asyncio.wait_for(
                    self._read_and_process(reader), timeout=self.config.request_timeout
                )
                encoded = json.dumps(ack.to_dict(), separators=(",", ":")).encode("utf-8")
                writer.write(struct.pack("!I", len(encoded)) + encoded)
                await asyncio.wait_for(writer.drain(), timeout=self.config.write_timeout)
                self.audit.emit(
                    "command_acknowledged",
                    peer=peer_ip,
                    request_id=ack.request_id,
                    command_id=ack.command_id,
                    command_type=ack.command_type,
                    state=ack.state,
                )
            except Exception as exc:
                self.audit.emit("request_rejected", peer=peer_ip, reason=type(exc).__name__)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _read_and_process(self, reader: asyncio.StreamReader):
        prefix = await reader.readexactly(4)
        (length,) = struct.unpack("!I", prefix)
        if length < 1 or length > MAX_COMMAND_WIRE_BYTES:
            raise ValueError("invalid request size")
        body = await reader.readexactly(length)
        # Reject already-buffered second/trailing frames without waiting for EOF.
        if reader._buffer:  # bounded private buffer; no bytes are consumed or logged
            raise ValueError("trailing data is forbidden")
        request = strict_request_from_dict(_json_no_duplicates(body))
        return await asyncio.to_thread(self.processor.process, request)
