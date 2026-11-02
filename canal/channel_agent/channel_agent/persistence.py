"""SQLite-backed command idempotency and conservative Channel recovery."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Callable, Iterator

from groundlink_command_transport.command_protocol import CommandAcknowledgement, CommandRequest
from mission_control.agent_contract import command_from_dict

from .core import ChannelAgent, CommandRejected


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DurableCommandProcessor:
    """Serializes state changes and commits canonical command/result before reply."""

    def __init__(self, database: Path | str, *, clock: Callable[[], str] = _utc_now) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = RLock()
        self._initialize()
        state, run_id, scenario, preparation_id = self._recover()
        self.agent = ChannelAgent(
            recovered_state=state, recovered_run_id=run_id,
            recovered_scenario=scenario, recovered_preparation_id=preparation_id,
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=2.0, isolation_level="IMMEDIATE")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    canonical_command TEXT NOT NULL,
                    acknowledgement TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    state TEXT NOT NULL, run_id TEXT, scenario TEXT, preparation_id TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO agent_state VALUES (1, 'IDLE', NULL, NULL, NULL, 'initial');
            """)
            db.commit()

    def _recover(self) -> tuple[str, str | None, str | None, str | None]:
        with self._connection() as db:
            state, run_id, scenario, preparation_id = db.execute(
                "SELECT state,run_id,scenario,preparation_id FROM agent_state WHERE singleton=1"
            ).fetchone()
            # A recovered RUNNING/STOPPING process is never assumed alive or ready. ERROR blocks
            # retries of START until a correlated STOP establishes a safe IDLE state.
            if state in {"RUNNING", "STOPPING", "ERROR"}:
                state = "ERROR"
                db.execute("UPDATE agent_state SET state='ERROR',updated_at=? WHERE singleton=1", (self._clock(),))
                db.commit()
            return state, run_id, scenario, preparation_id

    def process(self, request: CommandRequest) -> CommandAcknowledgement:
        if request.target_agent_id != "vm3-channel":
            raise ValueError("request targets a different agent")
        command_dict = request.command.to_dict()
        canonical = _canonical(command_dict)
        with self._lock, self._connection() as db:
            prior = db.execute(
                "SELECT canonical_command,acknowledgement FROM commands WHERE command_id=?",
                (request.command.command_id,),
            ).fetchone()
            if prior:
                if prior[0] != canonical:
                    raise CommandRejected("command_id collision")
                stored = json.loads(prior[1])
                stored["request_id"] = request.request_id
                return CommandAcknowledgement(**stored)

            local_command = command_from_dict(command_dict)
            try:
                result = self.agent.handle(local_command)
                accepted, reason = True, None
            except CommandRejected as exc:
                accepted, reason = False, str(exc)
                result = None
            ack = CommandAcknowledgement(
                request_id=request.request_id, agent_id="vm3-channel",
                command_id=local_command.command_id,
                command_type=local_command.command_type.value,
                run_id=str(local_command.run_id), scenario=local_command.scenario,
                accepted=accepted, state=self.agent.state,
                preparation_id=(
                    result.preparation_id if result else
                    (local_command.preparation_id if local_command.command_type.value == "START" else None)
                ),
                reason=reason, observed_at=self._clock(),
            )
            # A rejected command is durable too: retry returns the identical semantic result.
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (local_command.command_id, canonical, _canonical(ack.to_dict()), self._clock()),
            )
            db.execute(
                "UPDATE agent_state SET state=?,run_id=?,scenario=?,preparation_id=?,updated_at=? WHERE singleton=1",
                (self.agent.state, self.agent.run_id, self.agent.scenario,
                 result.preparation_id if result else getattr(self.agent, "_preparation_id", None), self._clock()),
            )
            db.commit()
            return ack
