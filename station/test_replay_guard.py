from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from replay_guard import ReplayGuard


with TemporaryDirectory() as temp:

    # Base temporaire réservée à ce test
    db_path = Path(temp) / "test_replay.sqlite3"

    guard = ReplayGuard(db_path=db_path)

    observation = {
        "satellite_id": "SAT-001",
        "session_id": "TEST-SESSION-01",
        "sequence_number": 1,
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    }

    # TEST 1 : Première réception
    assert guard.check_and_record(observation)

    print("[TEST 1] Première observation : ACCEPT")

    # TEST 2 : Même observation retransmise
    try:
        guard.check_and_record(observation)

        raise AssertionError("Replay accepté par erreur")

    except ValueError as error:
        assert str(error) == "REPLAY_DETECTED"

        print("[TEST 2] Replay : REJECT / REPLAY_DETECTED")

    # TEST 3 : Simuler un redémarrage
    guard = ReplayGuard(db_path=db_path)

    try:
        guard.check_and_record(observation)

        raise AssertionError(
            "Replay accepté après redémarrage"
        )

    except ValueError as error:
        assert str(error) == "REPLAY_DETECTED"

        print("[TEST 3] Replay après redémarrage : REJECT")

    # TEST 4 : Nouvelle séquence
    new_observation = dict(observation)
    new_observation["sequence_number"] = 2

    assert guard.check_and_record(new_observation)

    print("[TEST 4] Nouvelle séquence : ACCEPT")

    # TEST 5 : Observation trop ancienne
    old_observation = dict(observation)

    old_observation["sequence_number"] = 3

    old_observation["timestamp"] = (
        datetime.now(timezone.utc)
        - timedelta(hours=1)
    ).isoformat()

    try:
        guard.check_and_record(old_observation)

        raise AssertionError("Observation ancienne acceptée")

    except ValueError as error:
        assert str(error) == "TIMESTAMP_EXPIRED"

        print("[TEST 5] Ancien timestamp : REJECT")


print("[SUCCESS] Tous les tests Anti-Replay réussis")
