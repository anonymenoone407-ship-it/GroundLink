
import sqlite3

from pathlib import Path
from tempfile import TemporaryDirectory

from monitoring_db import MonitoringDB


with TemporaryDirectory() as temp:

    db_path = Path(temp) / "monitoring_test.sqlite3"

    monitor = MonitoringDB(db_path)

    metrics = {
        "power_db_rel": 0.04,
        "evm_percent": 10.02,
        "estimated_gain": 1.0,
        "residual_spectral_peak_db": 11.54,
    }

    observation = {
        "satellite_id": "SAT-001",
        "session_id": "TEST-SESSION",
        "sequence_number": 1,
    }

    # TEST 1 : Observation acceptee

    monitor.record_event(
        iq_bytes=96768,
        metrics=metrics,
        status="ACCEPT",
        reason="OK",
        observation=observation,
        scenario="awgn_20db",
        processing_ms=2.4,
    )

    print("[TEST 1] Observation acceptee enregistree")

    # TEST 2 : Erreur CRC

    monitor.record_event(
        iq_bytes=96768,
        metrics=metrics,
        status="REJECT",
        reason="CRC_INVALID",
        scenario="awgn_0db",
    )

    print("[TEST 2] Rejet CRC avec mesures RF enregistre")

    # TEST 3 : Bloc IQ invalide

    monitor.record_event(
        iq_bytes=7,
        metrics=None,
        status="REJECT",
        reason="IQ_LENGTH_INVALID",
        scenario="malformed",
    )

    print("[TEST 3] Bloc IQ invalide enregistre sans metriques")

    # TEST 4 : Reouvrir la base SQLite

    restarted = MonitoringDB(db_path)

    with sqlite3.connect(db_path) as db:

        rows = db.execute("""
            SELECT
                status,
                reason,
                sequence_number,
                evm_percent
            FROM rx_events
            ORDER BY id
        """).fetchall()

    assert len(rows) == 3
    assert rows[0][0:3] == ("ACCEPT", "OK", 1)
    assert rows[1][0:3] == ("REJECT", "CRC_INVALID", None)
    assert rows[2][3] is None

    assert restarted.run_id != monitor.run_id

    print(
        "[TEST 4] Trois evenements conserves "
        "apres reouverture SQLite"
    )


print("[SUCCESS] Monitoring SQLite tests passed")



