"""TLS 1.3-only, mutually authenticated transport for agent ingestion.

This module is deliberately not started by the FastAPI application.  Deployment
must explicitly supply CA/server certificate paths and an approved bind address.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import ssl
import struct
from typing import Any, Mapping

from mission_control.agent_contract import AuthenticatedAgent

from .agent_ingestion import AgentIngestionAdapter, MAX_AGENT_REQUEST_BYTES, RequestContext


PROTOCOL_VERSION = "1.0"
MAX_WIRE_BYTES = MAX_AGENT_REQUEST_BYTES
IDENTITY_BINDINGS = {
    "vm1-satellite": "SATELLITE",
    "vm3-channel": "CHANNEL",
}
logger = logging.getLogger("groundlink.agent_transport")


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class TLSFiles:
    ca_certificate: Path
    server_certificate: Path
    server_private_key: Path


class CertificatePolicy:
    """Maps a verified certificate SAN to a fixed agent role."""

    def __init__(self, *, revoked_sha256: frozenset[str] = frozenset()):
        self.revoked_sha256 = frozenset(value.lower() for value in revoked_sha256)

    def authenticate(self, ssl_object: ssl.SSLObject) -> AuthenticatedAgent:
        der = ssl_object.getpeercert(binary_form=True)
        decoded = ssl_object.getpeercert()
        if not der or not decoded:
            raise TransportError("client certificate required")
        fingerprint = hashlib.sha256(der).hexdigest()
        if fingerprint in self.revoked_sha256:
            raise TransportError("client certificate revoked")
        dns_names = [value for kind, value in decoded.get("subjectAltName", ()) if kind == "DNS"]
        authorized = [name for name in dns_names if name in IDENTITY_BINDINGS]
        if len(authorized) != 1 or len(dns_names) != 1:
            raise TransportError("certificate identity is not authorized")
        agent_id = authorized[0]
        not_after = decoded.get("notAfter")
        if not not_after:
            raise TransportError("certificate expiry is unavailable")
        remaining = ssl.cert_time_to_seconds(not_after) - datetime.now(timezone.utc).timestamp()
        if remaining <= 0:
            raise TransportError("client certificate expired")
        if remaining <= 7 * 86400:
            logger.warning("agent_certificate_expiring agent_id=%s days=%d", agent_id, int(remaining // 86400))
        return AuthenticatedAgent(agent_id, IDENTITY_BINDINGS[agent_id], "mTLS", True)


class PersistentCertificatePolicy(CertificatePolicy):
    """Fail-closed operator policy reloaded for every authenticated connection."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._load()

    def _load(self) -> frozenset[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransportError("certificate revocation policy unavailable") from exc
        if not isinstance(data, dict) or set(data) != {"version", "revoked_sha256"} or data["version"] != 1:
            raise TransportError("certificate revocation policy invalid")
        values = data["revoked_sha256"]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value)
            for value in values
        ):
            raise TransportError("certificate revocation policy invalid")
        return frozenset(value.lower() for value in values)

    def authenticate(self, ssl_object: ssl.SSLObject) -> AuthenticatedAgent:
        self.revoked_sha256 = self._load()
        return super().authenticate(ssl_object)


def create_server_ssl_context(files: TLSFiles) -> ssl.SSLContext:
    """Build a fail-closed TLS context; private keys are read only by ssl."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(files.ca_certificate))
    context.load_cert_chain(str(files.server_certificate), str(files.server_private_key))
    return context


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise TransportError("invalid sent_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransportError("invalid sent_at") from exc
    if parsed.tzinfo is None:
        raise TransportError("invalid sent_at")
    return parsed


class AgentTLSServer:
    """Length-prefixed JSON protocol directly bound to the verified TLS socket."""

    def __init__(
        self, adapter: AgentIngestionAdapter, ssl_context: ssl.SSLContext,
        *, policy: CertificatePolicy | None = None, max_connections_per_agent: int = 2,
        requests_per_minute: int = 60,
    ):
        self.adapter = adapter
        self.ssl_context = ssl_context
        if policy is None:
            raise ValueError("an explicit certificate policy is required")
        self.policy = policy
        if max_connections_per_agent < 1 or requests_per_minute < 1:
            raise ValueError("transport limits must be positive")
        self.max_connections_per_agent = max_connections_per_agent
        self.requests_per_minute = requests_per_minute
        self._active: defaultdict[str, int] = defaultdict(int)
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> tuple[str, int]:
        if self._server is not None:
            raise RuntimeError("transport already started")
        self._server = await asyncio.start_server(
            self._handle, host, port, ssl=self.ssl_context, ssl_handshake_timeout=5
        )
        address = self._server.sockets[0].getsockname()
        return str(address[0]), int(address[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer: AuthenticatedAgent | None = None
        acquired = False
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            if ssl_object is None:
                raise TransportError("TLS is required")
            peer = self.policy.authenticate(ssl_object)
            now = asyncio.get_running_loop().time()
            recent = self._requests[peer.agent_id]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if self._active[peer.agent_id] >= self.max_connections_per_agent:
                raise TransportError("agent connection limit exceeded")
            if len(recent) >= self.requests_per_minute:
                raise TransportError("agent request rate exceeded")
            self._active[peer.agent_id] += 1
            acquired = True
            recent.append(now)
            header = await asyncio.wait_for(reader.readexactly(4), timeout=5)
            size = struct.unpack("!I", header)[0]
            if size <= 0 or size > MAX_WIRE_BYTES:
                raise TransportError("request size rejected")
            encoded = await asyncio.wait_for(reader.readexactly(size), timeout=5)
            try:
                request = json.loads(encoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransportError("malformed JSON") from exc
            await self._dispatch(request, peer, size)
            await self._reply(writer, {"status": "accepted"})
            logger.info("agent_request_accepted agent_id=%s", peer.agent_id)
            self.adapter.broker.database.record_security_audit(peer.agent_id, "ACCEPTED", "request")
        except (Exception, asyncio.IncompleteReadError) as exc:
            logger.warning(
                "agent_request_rejected agent_id=%s reason=%s",
                peer.agent_id if peer else "unverified", type(exc).__name__,
            )
            try:
                self.adapter.broker.database.record_security_audit(
                    peer.agent_id if peer else "unverified", "REJECTED", type(exc).__name__
                )
            except Exception:
                pass
            try:
                await self._reply(writer, {"status": "rejected"})
            except Exception:
                pass
        finally:
            if acquired and peer is not None:
                self._active[peer.agent_id] -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (Exception, asyncio.CancelledError):
                pass

    async def _dispatch(
        self, request: object, peer: AuthenticatedAgent, encoded_size: int
    ) -> None:
        if not isinstance(request, Mapping) or set(request) != {
            "protocol_version", "request_id", "sent_at", "kind", "payload"
        }:
            raise TransportError("invalid request envelope")
        if request["protocol_version"] != PROTOCOL_VERSION:
            raise TransportError("unsupported protocol version")
        if request["kind"] not in {"telemetry", "channel_action"}:
            raise TransportError("unsupported request kind")
        context = RequestContext(
            peer=peer, request_id=request["request_id"], sent_at=_timestamp(request["sent_at"])
        )
        if request["kind"] == "telemetry":
            await self.adapter.ingest_telemetry(
                request["payload"], context=context, encoded_size=encoded_size
            )
        else:
            await self.adapter.ingest_channel_action(
                request["payload"], context=context, encoded_size=encoded_size
            )

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, separators=(",", ":")).encode()
        writer.write(struct.pack("!I", len(encoded)) + encoded)
        await writer.drain()
