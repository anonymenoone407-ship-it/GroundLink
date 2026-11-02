import asyncio
from datetime import datetime, timedelta, timezone
import unittest
import uuid

from mission_control.agent_contract import (
    AgentCapabilities, AgentIdentity, AgentState, AuthenticatedAgent, Heartbeat,
    ReadinessReport, StartCommand,
)
from mission_control.scenarios import SCENARIO_ALLOWLIST
from mission_control.server.coordination import (
    CommandAck, CoordinationError, LiveCoordinator, ReadinessRegistry,
)


ROLES = {"vm1-satellite": "SATELLITE", "vm3-channel": "CHANNEL", "vm2-station": "STATION"}


def readiness(agent_id, *, when=None, state="IDLE", ready=True, run_id=None):
    identity = AgentIdentity(agent_id, ROLES[agent_id], "test")
    observed = (when or datetime.now(timezone.utc)).isoformat()
    report = ReadinessReport(identity, observed, ready, state, AgentCapabilities(identity, tuple(SCENARIO_ALLOWLIST)))
    heartbeat = Heartbeat(identity, uuid.uuid4().hex, observed, state, run_id)
    return report, heartbeat


def fill_registry(registry, *, when=None):
    for agent_id in ROLES:
        report, heartbeat = readiness(agent_id, when=when)
        if agent_id == "vm2-station":
            registry.update(report, heartbeat, protocol_version="1.0", process_ready=True, local_station=True)
        else:
            peer = AuthenticatedAgent(agent_id, ROLES[agent_id], "mTLS", True)
            registry.update(report, heartbeat, protocol_version="1.0", process_ready=True, peer=peer)


class FakeControl:
    def __init__(self, agent_id, log, *, fail=None, hang_start=False):
        self.agent_id = agent_id
        self.log = log
        self.fail = fail
        self.hang_start = hang_start
        self.preparation_id = None

    async def send(self, command):
        kind = command.command_type.value
        self.log.append((kind, self.agent_id))
        if self.fail == kind:
            return CommandAck(command.command_id, False, "ERROR", reason="test")
        if kind == "PREPARE":
            self.preparation_id = f"prep-{self.agent_id}"
            return CommandAck(command.command_id, True, "PREPARED", self.preparation_id)
        if kind == "START":
            if command.preparation_id != self.preparation_id:
                return CommandAck(command.command_id, False, "ERROR", reason="wrong preparation")
            return CommandAck(command.command_id, True, "RUNNING")
        return CommandAck(command.command_id, True, "IDLE")

    async def wait_for_state(self, state, run_id, timeout):
        if self.hang_start:
            await asyncio.sleep(10)


class ReadinessTests(unittest.TestCase):
    def test_valid_authenticated_readiness_and_missing_process(self):
        registry = ReadinessRegistry()
        fill_registry(registry)
        registry.require_idle_ready()
        missing = ReadinessRegistry()
        report, heartbeat = readiness("vm1-satellite")
        missing.update(report, heartbeat, protocol_version="1.0", process_ready=False,
                       peer=AuthenticatedAgent("vm1-satellite", "SATELLITE", "mTLS", True))
        with self.assertRaises(CoordinationError):
            missing.require_idle_ready()

    def test_invalid_identity_protocol_stale_and_replayed_heartbeat(self):
        registry = ReadinessRegistry(freshness_seconds=30)
        report, heartbeat = readiness("vm1-satellite")
        with self.assertRaises(PermissionError):
            registry.update(report, heartbeat, protocol_version="1.0", process_ready=True,
                            peer=AuthenticatedAgent("vm3-channel", "CHANNEL", "mTLS", True))
        with self.assertRaises(CoordinationError):
            registry.update(report, heartbeat, protocol_version="2.0", process_ready=True,
                            peer=AuthenticatedAgent("vm1-satellite", "SATELLITE", "mTLS", True))
        old_report, old_heartbeat = readiness("vm1-satellite", when=datetime.now(timezone.utc)-timedelta(minutes=2))
        with self.assertRaises(CoordinationError):
            registry.update(old_report, old_heartbeat, protocol_version="1.0", process_ready=True,
                            peer=AuthenticatedAgent("vm1-satellite", "SATELLITE", "mTLS", True))
        peer = AuthenticatedAgent("vm1-satellite", "SATELLITE", "mTLS", True)
        registry.update(report, heartbeat, protocol_version="1.0", process_ready=True, peer=peer)
        with self.assertRaises(CoordinationError):
            registry.update(report, heartbeat, protocol_version="1.0", process_ready=True, peer=peer)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **options):
        registry = ReadinessRegistry()
        fill_registry(registry)
        log = []
        clients = {agent: FakeControl(agent, log, **options.get(agent, {})) for agent in ROLES}
        return LiveCoordinator(registry, clients, timeout=0.03), clients, log

    async def test_order_prepare_start_reverse_stop_and_idempotency(self):
        coordinator, clients, log = self.make()
        run = await coordinator.prepare("baseline")
        self.assertEqual(log, [("PREPARE", "vm2-station"), ("PREPARE", "vm3-channel"), ("PREPARE", "vm1-satellite")])
        await coordinator.start(run.run_id)
        self.assertEqual(log[3:6], [("START", "vm2-station"), ("START", "vm3-channel"), ("START", "vm1-satellite")])
        await coordinator.stop(run.run_id)
        self.assertEqual(log[6:9], [("STOP", "vm1-satellite"), ("STOP", "vm3-channel"), ("STOP", "vm2-station")])
        count = len(log)
        await coordinator.stop(run.run_id)
        self.assertEqual(len(log), count)

    async def test_prepare_required_wrong_run_and_concurrent_run_rejected(self):
        coordinator, _, _ = self.make()
        with self.assertRaises(CoordinationError):
            await coordinator.start("0" * 32)
        run = await coordinator.prepare("baseline")
        with self.assertRaises(CoordinationError):
            await coordinator.prepare("jamming")
        with self.assertRaises(CoordinationError):
            await coordinator.start("0" * 32)
        run.preparation_ids["vm3-channel"] = "wrong"
        with self.assertRaises(CoordinationError):
            await coordinator.start(run.run_id)

    async def test_prepare_failure_rolls_back_and_start_timeout_rolls_back(self):
        coordinator, _, log = self.make(**{"vm3-channel": {"fail": "PREPARE"}})
        with self.assertRaises(CoordinationError):
            await coordinator.prepare("baseline")
        self.assertEqual(log[-1], ("STOP", "vm2-station"))
        coordinator, _, log = self.make(**{"vm2-station": {"hang_start": True}})
        run = await coordinator.prepare("baseline")
        with self.assertRaises(CoordinationError):
            await coordinator.start(run.run_id)
        self.assertEqual(log[-3:], [("STOP", "vm1-satellite"), ("STOP", "vm3-channel"), ("STOP", "vm2-station")])


if __name__ == "__main__":
    unittest.main()
