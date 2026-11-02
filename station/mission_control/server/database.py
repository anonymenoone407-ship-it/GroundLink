"""Dedicated SQLite persistence for Mission Control runs and event envelopes."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


class MissionControlDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('MOCK', 'LIVE')),
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STOPPED', 'NOT_READY')),
                    created_at TEXT NOT NULL,
                    stopped_at TEXT,
                    message TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS only_one_active_run
                    ON runs((1)) WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    run_sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, event_id),
                    UNIQUE(run_id, run_sequence)
                );
                CREATE INDEX IF NOT EXISTS events_by_run_sequence
                    ON events(run_id, run_sequence);
                CREATE TABLE IF NOT EXISTS agent_requests (
                    agent_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS agent_requests_by_received
                    ON agent_requests(received_at);
                CREATE TABLE IF NOT EXISTS security_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('ACCEPTED', 'REJECTED')),
                    category TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            for name in ("frame_id", "source_agent_id", "source_role"):
                if name not in columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS one_producer_event_per_frame
                   ON events(run_id, source_agent_id, event_type, frame_id)
                   WHERE frame_id IS NOT NULL AND source_agent_id IS NOT NULL"""
            )
            db.commit()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def create_run(self, run_id: str, scenario: str, mode: str) -> dict[str, Any]:
        status = "ACTIVE" if mode == "MOCK" else "NOT_READY"
        message = (
            "MOCK mode: synthetic non-security events only"
            if mode == "MOCK"
            else "NOT_READY: authenticated VM agents are not integrated"
        )
        now = utc_now()
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO runs(run_id, scenario, mode, status, created_at, message) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, scenario, mode, status, now, message),
                )
                db.commit()
        except sqlite3.IntegrityError as exc:
            if "only_one_active_run" in str(exc) or "UNIQUE constraint failed: index" in str(exc):
                raise ConflictError("another MOCK run is already active") from exc
            raise ConflictError("run_id already exists") from exc
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(run_id)
        return self._run(row)

    def stop_run(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                db.rollback()
                raise NotFoundError(run_id)
            if row["status"] == "ACTIVE":
                db.execute(
                    "UPDATE runs SET status = 'STOPPED', stopped_at = ?, message = ? WHERE run_id = ? AND status = 'ACTIVE'",
                    (utc_now(), "Run stopped safely", run_id),
                )
            db.commit()
        return self.get_run(run_id)

    def store_event(
        self, event: dict[str, Any], *, frame_id: str | None = None,
        source_agent_id: str | None = None, source_role: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        run_id = event["run_id"]
        event_id = event["event_id"]
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT scenario, status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                db.rollback()
                raise NotFoundError(run_id)
            if run["status"] != "ACTIVE":
                db.rollback()
                raise ConflictError("events are accepted only for an ACTIVE run")
            existing = db.execute(
                """SELECT run_sequence, event_json, frame_id, source_agent_id, source_role
                   FROM events WHERE run_id = ? AND event_id = ?""",
                (run_id, event_id),
            ).fetchone()
            if existing is not None:
                db.rollback()
                payload = json.loads(existing["event_json"])
                if payload != event or (
                    existing["frame_id"], existing["source_agent_id"], existing["source_role"]
                ) != (frame_id, source_agent_id, source_role):
                    raise ConflictError("event_id was already used for a different event")
                if existing["frame_id"] is not None:
                    payload.update(
                        frame_id=existing["frame_id"],
                        source_agent_id=existing["source_agent_id"],
                        source_role=existing["source_role"],
                    )
                payload["run_sequence"] = existing["run_sequence"]
                return payload, True
            sequence = db.execute(
                "SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            try:
                db.execute(
                """INSERT INTO events(run_id, run_sequence, event_id, event_time, event_type,
                   event_json, stored_at, frame_id, source_agent_id, source_role)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    sequence,
                    event_id,
                    event["event_time"],
                    event["event_type"],
                    json.dumps(event, separators=(",", ":"), sort_keys=True),
                    utc_now(),
                    frame_id, source_agent_id, source_role,
                ),
                )
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ConflictError("frame already has an event of this type from this producer") from exc
            db.commit()
        stored = dict(event)
        if frame_id is not None:
            stored.update(frame_id=frame_id, source_agent_id=source_agent_id, source_role=source_role)
        stored["run_sequence"] = sequence
        return stored, False

    def list_events(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT run_sequence, event_json, frame_id, source_agent_id, source_role
                   FROM events WHERE run_id = ? ORDER BY run_sequence LIMIT ?""",
                (run_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            event = json.loads(row["event_json"])
            if row["frame_id"] is not None:
                event.update(frame_id=row["frame_id"], source_agent_id=row["source_agent_id"], source_role=row["source_role"])
            event["run_sequence"] = row["run_sequence"]
            result.append(event)
        return result

    def has_frame_event(self, run_id: str, frame_id: str, event_type: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT 1 FROM events WHERE run_id = ? AND frame_id = ? AND event_type = ?",
                (run_id, frame_id, event_type),
            ).fetchone()
        return row is not None

    def claim_agent_request(self, agent_id: str, request_id: str, sent_at: str) -> None:
        """Atomically persist a transport nonce before payload persistence."""
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO agent_requests(agent_id, request_id, sent_at, received_at) VALUES (?, ?, ?, ?)",
                    (agent_id, request_id, sent_at, utc_now()),
                )
                db.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError("replayed agent request") from exc

    def record_security_audit(self, agent_id: str, outcome: str, category: str) -> None:
        if outcome not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("invalid audit outcome")
        if not agent_id or len(agent_id) > 64 or not category or len(category) > 64:
            raise ValueError("invalid audit field")
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO security_audit(occurred_at, agent_id, outcome, category) VALUES (?, ?, ?, ?)",
                (utc_now(), agent_id, outcome, category),
            )
