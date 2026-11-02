
import os
import time
from pathlib import Path

import zmq

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey
)

from groundlink_secure_common import (
    bpsk_demodulate,
    verify_signed_frame
)

from replay_guard import ReplayGuard
from rf_metrics import analyze_iq
from monitoring_db import MonitoringDB


# ==========================================
# CONFIGURATION
# ==========================================

STATION_ADDRESS = "tcp://10.10.2.10:5556"

PUBLIC_KEY = (
    Path.home()
    / "groundlink"
    / "keys"
    / "satellite_public.pem"
)

EXPECTED_SATELLITE = "SAT-001"
EXPECTED_FRAMES = 5

# Etiquette experimentale definie sur Station
SCENARIO = os.environ.get("GL_SCENARIO", "unlabeled")


# ==========================================
# TRUST ANCHOR
# ==========================================

def check_trust_anchor():

    if not PUBLIC_KEY.is_file():
        raise FileNotFoundError(
            f"Cle publique introuvable : {PUBLIC_KEY}"
        )

    public_key = serialization.load_pem_public_key(
        PUBLIC_KEY.read_bytes()
    )

    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError(
            "Trust Anchor invalide : Ed25519 attendu"
        )

    print("[STATION] Trust Anchor Ed25519 charge")


# ==========================================
# RECEPTION ET MONITORING
# ==========================================

def main():

    check_trust_anchor()

    guard = ReplayGuard()
    monitor = MonitoringDB()

    print("[STATION] Anti-Replay SQLite active")
    print(f"[STATION] Replay DB : {guard.db_path}")
    print(f"[STATION] Monitoring DB : {monitor.db_path}")
    print(f"[STATION] Run ID : {monitor.run_id}")
    print(f"[STATION] Scenario : {SCENARIO}")

    context = zmq.Context()
    socket = context.socket(zmq.PULL)

    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.RCVTIMEO, 1000)

    socket.bind(STATION_ADDRESS)

    print(
        "[STATION] Secure BPSK Receiver "
        "+ RF Monitoring + SQLite"
    )

    print(f"[STATION] Ecoute : {STATION_ADDRESS}")
    print("[STATION] Attente des trames IQ...\n")

    received_count = 0
    accepted_count = 0
    rejected_count = 0
    db_error_count = 0

    try:

        while received_count < EXPECTED_FRAMES:

            try:
                iq_bytes = socket.recv()

            except zmq.Again:
                continue

            received_count += 1

            print(
                f"[RX] Bloc IQ #{received_count} "
                f"| {len(iq_bytes)} octets"
            )

            start = time.perf_counter()

            metrics = None
            trusted_observation = None

            status = "REJECT"
            reason = "UNPROCESSED"

            try:

                # ==================================
                # 1. METRIQUES RF
                # ==================================

                metrics = analyze_iq(iq_bytes)

                peak = metrics[
                    "residual_spectral_peak_db"
                ]

                peak_text = (
                    "N/A"
                    if peak is None
                    else f"{peak:.2f} dB"
                )

                print(
                    "[RF] "
                    f"Power={metrics['power_db_rel']:.2f} dB | "
                    f"EVM={metrics['evm_percent']:.2f}% | "
                    f"Gain={metrics['estimated_gain']:.3f} | "
                    f"SpectralPeak={peak_text}"
                )

                # ==================================
                # 2. DEMODULATION BPSK
                # ==================================

                frame = bpsk_demodulate(iq_bytes)

                # ==================================
                # 3. CRC32 + ED25519
                # ==================================

                observation = verify_signed_frame(
                    frame,
                    PUBLIC_KEY
                )

                # ==================================
                # 4. VALIDATION DES CHAMPS
                # ==================================

                if not isinstance(observation, dict):
                    raise ValueError(
                        "OBSERVATION_FORMAT_INVALID"
                    )

                satellite_id = observation.get(
                    "satellite_id"
                )

                if satellite_id != EXPECTED_SATELLITE:
                    raise ValueError(
                        "SATELLITE_ID_INVALID"
                    )

                if observation.get(
                    "measurement_type"
                ) != "ocean_surface_wind":
                    raise ValueError(
                        "MEASUREMENT_TYPE_INVALID"
                    )

                seq = observation.get(
                    "sequence_number"
                )

                if type(seq) is not int or seq < 1:
                    raise ValueError(
                        "SEQUENCE_NUMBER_INVALID"
                    )

                wind = observation.get(
                    "wind_speed_ms"
                )

                if type(wind) not in (int, float):
                    raise ValueError(
                        "WIND_SPEED_INVALID"
                    )

                # ==================================
                # 5. ANTI-REPLAY
                # ==================================

                guard.check_and_record(observation)

                # ==================================
                # 6. ACCEPTATION
                # ==================================

                trusted_observation = observation

                status = "ACCEPT"
                reason = "OK"

                accepted_count += 1

                print(
                    f"[ACCEPT] seq={seq} | "
                    f"satellite={satellite_id} | "
                    f"wind={wind} m/s | "
                    "CRC32=OK | Ed25519=OK | "
                    "AntiReplay=OK"
                )

            except ValueError as error:

                reason = str(error)
                rejected_count += 1

                print(
                    f"[REJECT] Bloc #{received_count} "
                    f"| reason={reason}"
                )

            finally:

                # Temps RF + securite, hors ecriture DB
                processing_ms = (
                    time.perf_counter() - start
                ) * 1000.0

                # ==================================
                # 7. JOURNALISATION SQLITE
                # ==================================

                try:

                    event_id = monitor.record_event(
                        iq_bytes=len(iq_bytes),
                        status=status,
                        reason=reason,
                        metrics=metrics,
                        observation=trusted_observation,
                        scenario=SCENARIO,
                        processing_ms=processing_ms
                    )

                    print(
                        f"[DB] Event #{event_id} saved "
                        f"| {status} | {reason}"
                    )

                except Exception as error:

                    db_error_count += 1

                    print(
                        f"[DB_ERROR] "
                        f"{type(error).__name__}: {error}"
                    )

                print()

    except KeyboardInterrupt:

        print("\n[STATION] Arret manuel")

    finally:

        socket.close()
        context.term()

        print("========== BILAN STATION ==========")
        print(f"IQ recus : {received_count}")
        print(f"ACCEPT : {accepted_count}")
        print(f"REJECT : {rejected_count}")
        print(f"DB errors : {db_error_count}")
        print("===================================")


if __name__ == "__main__":
    main()



