
import math
import sqlite3
import uuid

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class MonitoringDB:
    """
    Historique RF et securite GroundLink Guardian.
    Ne stocke pas les echantillons IQ.
    """

    def __init__(self, db_path=None):

        if db_path is None:
            db_path = (
                Path.home()
                / "groundlink"
                / "state"
                / "monitoring.sqlite3"
            )

        self.db_path = Path(db_path)
        self.run_id = uuid.uuid4().hex

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with closing(
            sqlite3.connect(self.db_path, timeout=5)
        ) as db:

            with db:

                db.execute("""
                    CREATE TABLE IF NOT EXISTS rx_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_time TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        scenario TEXT NOT NULL,
                        iq_bytes INTEGER NOT NULL,
                        power_db_rel REAL,
                        evm_percent REAL,
                        estimated_gain REAL,
                        residual_spectral_peak_db REAL,
                        status TEXT NOT NULL
                            CHECK(status IN ('ACCEPT', 'REJECT')),
                        reason TEXT NOT NULL,
                        satellite_id TEXT,
                        session_id TEXT,
                        sequence_number INTEGER,
                        processing_ms REAL,
                        rf_level TEXT,
                        rf_reasons TEXT
                    )
                """)

                db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_rx_events_run
                    ON rx_events(run_id)
                """)

                # Migration pour une base deja existante
                columns = {
                    row[1]
                    for row in db.execute(
                        "PRAGMA table_info(rx_events)"
                    )
                }

                if "rf_level" not in columns:
                    db.execute("""
                        ALTER TABLE rx_events
                        ADD COLUMN rf_level TEXT
                    """)

                if "rf_reasons" not in columns:
                    db.execute("""
                        ALTER TABLE rx_events
                        ADD COLUMN rf_reasons TEXT
                    """)

        self.db_path.chmod(0o600)

    def record_event(
        self,
        iq_bytes,
        status,
        reason,
        metrics=None,
        observation=None,
        scenario="unlabeled",
        processing_ms=None,
        rf_level=None,
        rf_reasons=None,
    ):

        # Validation des champs

        if type(iq_bytes) is not int or iq_bytes < 0:
            raise ValueError("IQ_BYTES_INVALID")

        if status not in ("ACCEPT", "REJECT"):
            raise ValueError("STATUS_INVALID")

        if not isinstance(reason, str) or not reason:
            raise ValueError("REASON_INVALID")

        if not isinstance(scenario, str) or not scenario:
            raise ValueError("SCENARIO_INVALID")

        if metrics is not None and not isinstance(
            metrics, dict
        ):
            raise ValueError("METRICS_INVALID")

        if observation is not None and not isinstance(
            observation, dict
        ):
            raise ValueError("OBSERVATION_INVALID")

        if processing_ms is not None:
            if (
                type(processing_ms) not in (int, float)
                or not math.isfinite(processing_ms)
                or processing_ms < 0
            ):
                raise ValueError("PROCESSING_MS_INVALID")

        # Validation du diagnostic RF

        if rf_level not in (
            None,
            "NORMAL",
            "DEGRADED",
            "ANOMALY",
            "UNKNOWN",
        ):
            raise ValueError("RF_LEVEL_INVALID")

        if (
            rf_reasons is not None
            and not isinstance(rf_reasons, str)
        ):
            raise ValueError("RF_REASONS_INVALID")

        metrics = metrics or {}

        def finite_or_none(key):

            value = metrics.get(key)

            if value is None:
                return None

            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
            ):
                raise ValueError(
                    f"METRIC_INVALID:{key}"
                )

            return float(value)

        # Ne conserver l'identite que pour
        # les observations acceptees

        trusted = (
            observation
            if status == "ACCEPT"
            else None
        )

        trusted = trusted or {}

        # Insertion SQLite

        with closing(
            sqlite3.connect(self.db_path, timeout=5)
        ) as db:

            with db:

                cursor = db.execute("""
                    INSERT INTO rx_events (
                        event_time,
                        run_id,
                        scenario,
                        iq_bytes,
                        power_db_rel,
                        evm_percent,
                        estimated_gain,
                        residual_spectral_peak_db,
                        status,
                        reason,
                        satellite_id,
                        session_id,
                        sequence_number,
                        processing_ms,
                        rf_level,
                        rf_reasons
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, (
                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                    self.run_id,
                    scenario,
                    iq_bytes,

                    finite_or_none("power_db_rel"),
                    finite_or_none("evm_percent"),
                    finite_or_none("estimated_gain"),
                    finite_or_none(
                        "residual_spectral_peak_db"
                    ),

                    status,
                    reason,

                    trusted.get("satellite_id"),
                    trusted.get("session_id"),
                    trusted.get("sequence_number"),

                    (
                        None
                        if processing_ms is None
                        else float(processing_ms)
                    ),

                    rf_level,
                    rf_reasons,
                ))

                return cursor.lastrowid




