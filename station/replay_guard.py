import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ReplayGuard:

    def __init__(
        self,
        db_path=None,
        max_age_seconds=300,
        future_tolerance_seconds=30,
        reorder_window=32
    ):

        if db_path is None:
            db_path = (
                Path.home()
                / "groundlink"
                / "state"
                / "replay_guard.sqlite3"
            )

        self.db_path = Path(db_path)
        self.max_age_seconds = max_age_seconds
        self.future_tolerance_seconds = future_tolerance_seconds
        self.reorder_window = reorder_window

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700
        )

        with sqlite3.connect(self.db_path, timeout=5) as db:

            db.execute("""
                CREATE TABLE IF NOT EXISTS accepted_observations (
                    satellite_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    received_at TEXT NOT NULL,

                    PRIMARY KEY (
                        satellite_id,
                        session_id,
                        sequence_number
                    )
                )
            """)

    def check_and_record(self, observation):
        """
        IMPORTANT :
        Appeler cette fonction uniquement APRÈS
        vérification CRC32 et signature Ed25519.

        Si l'observation est nouvelle et fraîche :
            l'enregistrer et retourner True.

        Sinon :
            lever ValueError avec le motif de rejet.
        """

        if not isinstance(observation, dict):
            raise ValueError("OBSERVATION_FORMAT_INVALID")

        satellite_id = observation.get("satellite_id")
        session_id = observation.get("session_id")
        seq = observation.get("sequence_number")
        timestamp = observation.get("timestamp")

        # Vérifier les identifiants
        if (
            not isinstance(satellite_id, str)
            or not 0 < len(satellite_id) <= 64
        ):
            raise ValueError("SATELLITE_ID_INVALID")

        if (
            not isinstance(session_id, str)
            or not 0 < len(session_id) <= 128
        ):
            raise ValueError("SESSION_ID_INVALID")

        if type(seq) is not int or not 1 <= seq < 2**63:
            raise ValueError("SEQUENCE_NUMBER_INVALID")

        if not isinstance(timestamp, str):
            raise ValueError("TIMESTAMP_INVALID")

        # Lire le timestamp signé
        try:
            observed_at = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )

        except ValueError as exc:
            raise ValueError("TIMESTAMP_INVALID") from exc

        if observed_at.tzinfo is None:
            raise ValueError("TIMESTAMP_WITHOUT_TIMEZONE")

        # Vérifier la fraîcheur
        now = datetime.now(timezone.utc)

        age = (now - observed_at).total_seconds()

        if age > self.max_age_seconds:
            raise ValueError("TIMESTAMP_EXPIRED")

        if age < -self.future_tolerance_seconds:
            raise ValueError("TIMESTAMP_IN_FUTURE")

        # Vérification atomique et enregistrement SQLite
        with sqlite3.connect(
            self.db_path,
            timeout=5
        ) as db:

            db.execute("BEGIN IMMEDIATE")

            highest = db.execute(
                """
                SELECT MAX(sequence_number)
                FROM accepted_observations
                WHERE satellite_id = ?
                  AND session_id = ?
                """,
                (satellite_id, session_id)
            ).fetchone()[0]

            # Rejeter une séquence trop ancienne.
            # Une petite réorganisation reste possible.
            if (
                highest is not None
                and seq <= highest - self.reorder_window
            ):
                raise ValueError("SEQUENCE_TOO_OLD")

            try:

                db.execute(
                    """
                    INSERT INTO accepted_observations
                    (
                        satellite_id,
                        session_id,
                        sequence_number,
                        received_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        satellite_id,
                        session_id,
                        seq,
                        now.isoformat()
                    )
                )

            except sqlite3.IntegrityError as exc:
                raise ValueError("REPLAY_DETECTED") from exc

        return True
