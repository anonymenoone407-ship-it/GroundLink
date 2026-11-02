"""Disabled-by-default TLS 1.3 command server for the VM3 Channel agent."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import ssl
import struct
from threading import Event, Thread
import time
from typing import Optional

from groundlink_command_transport.agent_contract import ValidationError
from groundlink_command_transport.command_protocol import (
    MAX_COMMAND_WIRE_BYTES, CommandRequest, command_request_from_dict,
)

from .core import CommandRejected
from .persistence import DurableCommandProcessor


@dataclass(frozen=True)
class ServerConfig:
    certificate: Path
    private_key: Path
    ca_certificate: Path
    crl: Path
    database: Path
    audit_log: Path
    host: str = "10.10.3.30"
    port: int = 7443
    enabled: bool = False
    io_timeout_seconds: float = 3.0
    accept_timeout_seconds: float = 0.2
    requests_per_minute: int = 30


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._seen: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, peer: str) -> bool:
        now = time.monotonic()
        entries = self._seen[peer]
        while entries and entries[0] <= now - self.window:
            entries.popleft()
        if len(entries) >= self.limit:
            return False
        entries.append(now)
        return True


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, *, peer: str, request_id: Optional[str] = None,
               command_id: Optional[str] = None, reason: Optional[str] = None) -> None:
        safe_reason = reason[:160].replace("\n", " ") if reason else None
        value = {"time": datetime.now(timezone.utc).isoformat(), "event": event,
                 "peer": peer, "request_id": request_id, "command_id": command_id,
                 "reason": safe_reason}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _certificate_dns_sans(cert: dict) -> list[str]:
    return [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]


def _recv_exact(stream: ssl.SSLSocket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.recv(size - len(chunks))
        if not part:
            raise ConnectionError("connection closed before complete frame")
        chunks.extend(part)
    return bytes(chunks)


def _read_request(stream: ssl.SSLSocket) -> dict:
    length = struct.unpack("!I", _recv_exact(stream, 4))[0]
    if not 1 <= length <= MAX_COMMAND_WIRE_BYTES:
        raise ValueError("request length is outside 1..16384")
    payload = _recv_exact(stream, length)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


def _write_response(stream: ssl.SSLSocket, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(payload) <= MAX_COMMAND_WIRE_BYTES:
        raise ValueError("response exceeds protocol limit")
    stream.sendall(struct.pack("!I", len(payload)) + payload)


class ChannelCommandServer:
    """Explicitly enabled server; production address cannot be activated accidentally."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.processor = DurableCommandProcessor(config.database)
        self.audit = AuditLog(config.audit_log)
        self.rate_limiter = RateLimiter(config.requests_per_minute)
        self._stop = Event()
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[Thread] = None
        self.bound_address: Optional[tuple[str, int]] = None

    def _context(self) -> ssl.SSLContext:
        decoded = ssl._ssl._test_decode_cert(str(self.config.certificate))
        if _certificate_dns_sans(decoded) != ["vm3-channel"]:
            raise ValueError("server certificate must have sole DNS SAN vm3-channel")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(self.config.certificate, self.config.private_key)
        context.load_verify_locations(cafile=self.config.ca_certificate)
        context.load_verify_locations(cafile=self.config.crl)
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        return context

    def start_background(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("production listener is disabled")
        if self.config.host != "127.0.0.1":
            raise RuntimeError("this phase permits only 127.0.0.1 test listeners")
        context = self._context()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.config.host, self.config.port))
        listener.listen(8)
        listener.settimeout(self.config.accept_timeout_seconds)
        self._listener = listener
        self.bound_address = listener.getsockname()
        self._thread = Thread(target=self._serve, args=(context,), daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _serve(self, context: ssl.SSLContext) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                raw, address = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            peer = address[0]
            try:
                raw.settimeout(self.config.io_timeout_seconds)
                if not self.rate_limiter.allow(peer):
                    self.audit.record("rate_limited", peer=peer)
                    raw.close()
                    continue
                with context.wrap_socket(raw, server_side=True) as stream:
                    cert = stream.getpeercert()
                    if _certificate_dns_sans(cert) != ["vm2-station"]:
                        self.audit.record("identity_rejected", peer=peer)
                        continue
                    fingerprint = hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()[:16]
                    self._handle(stream, f"{peer}:{fingerprint}")
            except (ssl.SSLError, socket.timeout, ConnectionError, ValueError) as exc:
                self.audit.record("connection_rejected", peer=peer, reason=str(exc))
                raw.close()

    def _handle(self, stream: ssl.SSLSocket, peer: str) -> None:
        data = _read_request(stream)
        request: CommandRequest = command_request_from_dict(data)
        now = datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(request.deadline_at.replace("Z", "+00:00"))
        if deadline <= now:
            raise ValueError("request deadline has expired")
        try:
            ack = self.processor.process(request)
        except CommandRejected as exc:
            self.audit.record("command_collision", peer=peer, request_id=request.request_id,
                              command_id=request.command.command_id, reason=str(exc))
            raise
        _write_response(stream, ack.to_dict())
        self.audit.record("command_processed", peer=peer, request_id=request.request_id,
                          command_id=request.command.command_id,
                          reason="accepted" if ack.accepted else ack.reason)
