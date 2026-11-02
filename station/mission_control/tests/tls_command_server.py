"""Loopback-only command agent simulator. Never import from production code."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import ssl
import struct

from mission_control.agent_contract import CommandType, ValidationError
from mission_control.command_protocol import (
    MAX_COMMAND_WIRE_BYTES, CommandAcknowledgement, command_request_from_dict,
)


class TestCommandAgentServer:
    __test__ = False

    def __init__(self, agent_id: str, ssl_context: ssl.SSLContext):
        self.agent_id = agent_id
        self.ssl_context = ssl_context
        self.server = None
        self.state = "IDLE"
        self.run_id = None
        self.scenario = None
        self.preparation_id = None
        self.cache = {}
        self.requests = []
        self.delay = 0.0
        self.fail_command = None
        self.invalid_ack = False
        self.oversized_response = False
        self._command_lock = asyncio.Lock()

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self.ssl_context, ssl_handshake_timeout=2
        )
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader, writer):
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            cert = ssl_object.getpeercert() if ssl_object else {}
            dns = [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]
            if dns != ["vm2-station"]:
                raise PermissionError("Station certificate required")
            if self._command_lock.locked():
                raise RuntimeError("concurrent command rejected")
            async with self._command_lock:
                size = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 2))[0]
                if size <= 0 or size > MAX_COMMAND_WIRE_BYTES:
                    raise ValidationError("request size rejected")
                raw = await asyncio.wait_for(reader.readexactly(size), 2)
                request = command_request_from_dict(json.loads(raw))
                if request.target_agent_id != self.agent_id:
                    raise PermissionError("wrong command target")
                if datetime.now(timezone.utc) >= datetime.fromisoformat(request.deadline_at.replace("Z", "+00:00")):
                    raise TimeoutError("command deadline expired")
                canonical = json.dumps(request.command.to_dict(), sort_keys=True, separators=(",", ":"))
                cached = self.cache.get(request.command.command_id)
                if cached:
                    if cached[0] != canonical:
                        raise ValidationError("command_id collision")
                    ack = replace(cached[1], request_id=request.request_id)
                else:
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    ack = self._execute(request)
                    self.cache[request.command.command_id] = (canonical, ack)
                    self.requests.append(request.command.command_type.value)
                value = ack.to_dict()
                if self.invalid_ack:
                    value["run_id"] = "0" * 32
                if self.oversized_response:
                    writer.write(struct.pack("!I", MAX_COMMAND_WIRE_BYTES + 1))
                else:
                    encoded = json.dumps(value, separators=(",", ":")).encode()
                    writer.write(struct.pack("!I", len(encoded)) + encoded)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _execute(self, request):
        command = request.command
        accepted, reason = True, None
        if self.fail_command == command.command_type.value:
            accepted, reason = False, "injected_failure"
        elif command.command_type == CommandType.PREPARE:
            if self.state != "IDLE":
                accepted, reason = False, "not_idle"
            else:
                self.run_id, self.scenario = str(command.run_id), command.scenario
                self.preparation_id = f"prep-{command.command_id}"
                self.state = "PREPARED"
        elif command.command_type == CommandType.START:
            if (str(command.run_id), command.scenario, command.preparation_id) != (
                self.run_id, self.scenario, self.preparation_id
            ):
                accepted, reason = False, "preparation_mismatch"
            else:
                self.state = "RUNNING"
        else:
            if self.run_id is not None and (str(command.run_id), command.scenario) != (self.run_id, self.scenario):
                accepted, reason = False, "run_mismatch"
            else:
                self.state = "IDLE"
                self.run_id = self.scenario = self.preparation_id = None
        state = self.state if accepted else "ERROR"
        prep = (
            self.preparation_id if command.command_type == CommandType.PREPARE and accepted
            else command.preparation_id if command.command_type == CommandType.START
            else None
        )
        return CommandAcknowledgement(
            request.request_id, self.agent_id, command.command_id, command.command_type.value,
            str(command.run_id), command.scenario, accepted, state, prep, reason,
            datetime.now(timezone.utc).isoformat(),
        )
