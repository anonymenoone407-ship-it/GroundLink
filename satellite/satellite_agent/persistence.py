"""Durable idempotent command processing in a dedicated SQLite database."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable

from .agent import AGENT_ID, SatelliteAgent, StateError
from .command_protocol import CommandAcknowledgement, CommandRequest


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CommandExpired(ValueError):
    pass


class DurableCommandProcessor:
    def __init__(
        self,
        database_path: Path,
        agent: SatelliteAgent,
        *,
        now: Callable[[], datetime] = _utc_now,
        max_clock_skew_seconds: float = 5.0,
        max_command_age_seconds: float = 30.0,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.database_path.exists():
            descriptor = os.open(
                self.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
        self.agent = agent
        self._now = now
        self._skew = max_clock_skew_seconds
        self._max_age = max_command_age_seconds
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.database_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                canonical_command TEXT NOT NULL,
                result_json TEXT NOT NULL,
                stored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._db.commit()
        row = self._db.execute("SELECT state_json FROM agent_state WHERE singleton=1").fetchone()
        if row:
            self.agent.restore(json.loads(row[0]), conservative_restart=True)
            self._persist_state_only()
        else:
            self._persist_state_only()

    def close(self) -> None:
        self._db.close()

    def _timestamp(self) -> str:
        return self._now().isoformat()

    def _persist_state_only(self) -> None:
        self._db.execute(
            "INSERT INTO agent_state(singleton,state_json,updated_at) VALUES(1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (_canonical(self.agent.snapshot()), self._timestamp()),
        )
        self._db.commit()

    def _check_freshness(self, request: CommandRequest) -> None:
        now = self._now()
        sent = datetime.fromisoformat(request.sent_at.replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(request.deadline_at.replace("Z", "+00:00"))
        issued = datetime.fromisoformat(request.command.issued_at.replace("Z", "+00:00"))
        if now > deadline:
            raise CommandExpired("command deadline has expired")
        if sent.timestamp() - now.timestamp() > self._skew or issued.timestamp() - now.timestamp() > self._skew:
            raise CommandExpired("command timestamp is too far in the future")
        if now.timestamp() - sent.timestamp() > self._max_age or now.timestamp() - issued.timestamp() > self._max_age:
            raise CommandExpired("command is too old")
        if issued > deadline:
            raise CommandExpired("command was issued after its deadline")

    def process(self, request: CommandRequest) -> CommandAcknowledgement:
        if request.target_agent_id != AGENT_ID:
            raise ValueError("request target is not vm1-satellite")
        self._check_freshness(request)
        command_dict = request.command.to_dict()
        canonical = _canonical(command_dict)
        command_id = request.command.command_id
        with self._lock:
            row = self._db.execute(
                "SELECT canonical_command,result_json FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row:
                if row[0] != canonical:
                    return self._ack(request, {
                        "accepted": False,
                        "state": self.agent.state,
                        "preparation_id": None,
                        "reason": "command_id collision",
                    })
                return self._ack(request, json.loads(row[1]))

            before = self.agent.snapshot()
            try:
                result = self.agent.handle_command(command_dict)
                outcome = {
                    "accepted": True,
                    "state": result.state,
                    "preparation_id": result.preparation_id,
                    "reason": None,
                }
            except StateError as exc:
                self.agent.restore(before, conservative_restart=False)
                outcome = {
                    "accepted": False,
                    "state": self.agent.state,
                    "preparation_id": None,
                    "reason": str(exc)[:256],
                }
            try:
                with self._db:
                    self._db.execute(
                        "INSERT INTO commands(command_id,canonical_command,result_json,stored_at) VALUES(?,?,?,?)",
                        (command_id, canonical, _canonical(outcome), self._timestamp()),
                    )
                    self._db.execute(
                        "UPDATE agent_state SET state_json=?,updated_at=? WHERE singleton=1",
                        (_canonical(self.agent.snapshot()), self._timestamp()),
                    )
            except Exception:
                self.agent.restore(before, conservative_restart=False)
                raise
            return self._ack(request, outcome)

    def _ack(
        self, request: CommandRequest, outcome: dict
    ) -> CommandAcknowledgement:
        command = request.command
        preparation_id = outcome["preparation_id"] if command.command_type.value in {"PREPARE", "START"} else None
        return CommandAcknowledgement(
            request_id=request.request_id,
            agent_id=AGENT_ID,
            command_id=command.command_id,
            command_type=command.command_type.value,
            run_id=str(command.run_id),
            scenario=command.scenario,
            accepted=outcome["accepted"],
            state=outcome["state"],
            preparation_id=preparation_id,
            reason=outcome["reason"],
            observed_at=self._timestamp(),
        )
