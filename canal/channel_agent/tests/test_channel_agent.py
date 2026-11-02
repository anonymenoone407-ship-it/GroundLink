import unittest

from mission_control.agent_contract import PrepareCommand, StartCommand, StopCommand
from mission_control.models import ValidationError
from mission_control.scenarios import SCENARIO_ALLOWLIST

from channel_agent import (
    ChannelAgent, CommandRejected, ExecutionObservation, SCENARIO_REGISTRY,
    ScriptOutputAdapter, TelemetryAdapter,
)

NOW = "2026-11-01T10:00:00Z"
RUN = "a" * 32


def prepare(run=RUN, scenario="baseline", command_id="prepare-1"):
    return PrepareCommand(command_id, run, scenario, NOW)


class ChannelAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = ChannelAgent()

    def test_identity_and_exact_allowlist(self):
        self.assertEqual((self.agent.identity.agent_id, self.agent.identity.role), ("vm3-channel", "CHANNEL"))
        self.assertEqual(frozenset(SCENARIO_REGISTRY), SCENARIO_ALLOWLIST)
        self.assertNotIn("awgn_10db", SCENARIO_REGISTRY)
        self.assertNotIn("fading", SCENARIO_REGISTRY)

    def test_prepare_start_stop_and_idempotency(self):
        p = prepare()
        result = self.agent.handle(p)
        self.assertEqual((result.state, result.preparation_id), ("PREPARED", "prepare-1"))
        self.assertTrue(self.agent.handle(p).idempotent)
        self.agent.handle(StartCommand("start-1", RUN, "baseline", NOW, "prepare-1"))
        self.assertEqual(self.agent.state, "RUNNING")
        stop = StopCommand("stop-1", RUN, "baseline", NOW)
        self.agent.handle(stop)
        self.assertEqual(self.agent.state, "IDLE")
        self.assertTrue(self.agent.handle(StopCommand("stop-2", RUN, "baseline", NOW)).idempotent)

    def test_rejects_concurrent_and_mismatched_context(self):
        self.agent.handle(prepare())
        with self.assertRaises(CommandRejected):
            self.agent.handle(prepare("b" * 32, "replay", "prepare-2"))
        with self.assertRaises(CommandRejected):
            self.agent.handle(StartCommand("start-x", RUN, "baseline", NOW, "wrong"))
        with self.assertRaises(CommandRejected):
            self.agent.handle(StartCommand("start-y", "b" * 32, "baseline", NOW, "prepare-1"))
        with self.assertRaises(CommandRejected):
            self.agent.handle(StopCommand("stop-x", RUN, "replay", NOW))

    def test_command_id_cannot_be_reused_with_different_content(self):
        self.agent.handle(prepare())
        with self.assertRaises(CommandRejected):
            self.agent.handle(prepare(RUN, "replay"))

    def test_confirmed_telemetry_and_action_are_separate_and_correlated(self):
        self.agent.handle(prepare(RUN, "awgn_20db"))
        self.agent.handle(StartCommand("start-1", RUN, "awgn_20db", NOW, "prepare-1"))
        pair = TelemetryAdapter(self.agent).adapt(ExecutionObservation(
            RUN, "frame-3", "channel-3", NOW, "awgn_20db", 128,
            "ATTACK", "AWGN", "TX confirmed", True,
        ))
        self.assertEqual(pair.telemetry.frame_id, pair.action.frame_id)
        self.assertEqual(pair.telemetry.event.run_id, pair.action.run_id)
        self.assertEqual(pair.telemetry.event.event_type.value, "channel.rf_observed")
        self.assertEqual(pair.action.message_type, "channel.action")
        for payload in (pair.telemetry.to_dict(), pair.action.to_dict()):
            self.assertNotIn("decision", str(payload).lower())
            self.assertNotIn("ACCEPT", str(payload))
            self.assertNotIn("REJECT", str(payload))

    def test_rejects_unconfirmed_wrong_attack_and_wrong_run(self):
        self.agent.handle(prepare(RUN, "malformed"))
        self.agent.handle(StartCommand("start-1", RUN, "malformed", NOW, "prepare-1"))
        adapter = TelemetryAdapter(self.agent)
        base = dict(run_id=RUN, frame_id="f1", event_id="e1", event_time=NOW,
                    scenario="malformed", iq_bytes=8, disposition="ATTACK", attack="MALFORMED")
        with self.assertRaises(ValidationError):
            adapter.adapt(ExecutionObservation(**base))
        with self.assertRaises(ValidationError):
            adapter.adapt(ExecutionObservation(**{**base, "confirmed": True, "attack": "SPOOFING"}))
        with self.assertRaises(ValidationError):
            adapter.adapt(ExecutionObservation(**{**base, "confirmed": True, "run_id": "b" * 32}))

    def test_tamper_intact_frame_is_forward_not_attack(self):
        self.agent.handle(prepare(RUN, "malformed"))
        self.agent.handle(StartCommand("start-1", RUN, "malformed", NOW, "prepare-1"))
        pair = TelemetryAdapter(self.agent).adapt(ExecutionObservation(
            RUN, "frame-2", "e2", NOW, "malformed", 64, "FORWARD", confirmed=True
        ))
        self.assertEqual(pair.action.disposition, "FORWARD")
        self.assertIsNone(pair.action.attack)

    def test_script_output_adapter_uses_only_action_bearing_lines(self):
        adapter = ScriptOutputAdapter()
        common = dict(run_id=RUN, frame_id="frame-3", event_id="e3", event_time=NOW)
        self.assertIsNone(adapter.adapt_line(line="[SPOOF] CRC32 recalculé", scenario="spoofing", **common))
        replay = adapter.adapt_line(
            line="[TX] Bloc #3 | status=REPLAY | 2048 octets", scenario="replay", **common)
        self.assertTrue(replay.confirmed)
        self.assertEqual((replay.disposition, replay.attack), ("ATTACK", "REPLAY"))
        intact = adapter.adapt_line(
            line="[TX] seq=2 | INTACTE | 1024 octets", scenario="malformed", **common)
        self.assertEqual((intact.disposition, intact.attack), ("FORWARD", None))
        with self.assertRaises(ValueError):
            adapter.adapt_line(
                line="TX #1 accepté par ZeroMQ | 128 octets | mode=awgn",
                scenario="baseline", **common)


if __name__ == "__main__":
    unittest.main()
