
import os
import time
from pathlib import Path

import zmq

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from groundlink_secure_common import (
    bpsk_demodulate,
    verify_signed_frame,
)
from replay_guard import ReplayGuard
from rf_metrics import analyze_iq
from anomaly_detector import detect_rf_anomaly
from monitoring_db import MonitoringDB


# ==========================================
# CONFIGURATION
# ==========================================

STATION_ADDRESS = "tcp://10.10.2.10:5556"
PUBLIC_KEY = Path.home() / "groundlink" / "keys" / "satellite_public.pem"
EXPECTED_SATELLITE = "SAT-001"
EXPECTED_FRAMES = 5

# Etiquette experimentale uniquement :
# elle n'intervient pas dans la detection ni les controles de securite.
SCENARIO = os.environ.get("GL_SCENARIO", "unlabeled")


def check_trust_anchor():
    if not PUBLIC_KEY.is_file():
        raise FileNotFoundError(f"Cle publique introuvable : {PUBLIC_KEY}")

    public_key = serialization.load_pem_public_key(PUBLIC_KEY.read_bytes())
    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError("Trust Anchor invalide : Ed25519 attendu")

    print("[STATION] Trust Anchor Ed25519 charge")


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

    print("[STATION] Secure BPSK Receiver + RF Detection + SQLite")
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
            print(f"[RX] Bloc IQ #{received_count} | {len(iq_bytes)} octets")

            start = time.perf_counter()
            metrics = None
            trusted_observation = None

            # Valeurs par defaut, y compris si l'analyse RF echoue.
            rf_level = "UNKNOWN"
            rf_reasons = "RF_METRICS_UNAVAILABLE"
            status = "REJECT"
            reason = "UNPROCESSED"

            try:
                # 1. Mesurer le signal IQ.
                metrics = analyze_iq(iq_bytes)
                peak = metrics["residual_spectral_peak_db"]
                peak_text = "N/A" if peak is None else f"{peak:.2f} dB"

                print(
                    "[RF] "
                    f"Power={metrics['power_db_rel']:.2f} dB | "
                    f"EVM={metrics['evm_percent']:.2f}% | "
                    f"Gain={metrics['estimated_gain']:.3f} | "
                    f"SpectralPeak={peak_text}"
                )

                # 2. Diagnostic RF independant du CRC32 et d'Ed25519.
                rf_result = detect_rf_anomaly(metrics)
                rf_level = rf_result["level"]
                rf_reasons = ", ".join(rf_result["reasons"])
                print(f"[RF-DETECT] {rf_level} | reasons={rf_reasons}")

                # 3. Demodulation, CRC32 et signature Ed25519.
                frame = bpsk_demodulate(iq_bytes)
                observation = verify_signed_frame(frame, PUBLIC_KEY)

                # 4. Validation des donnees authentifiees.
                if not isinstance(observation, dict):
                    raise ValueError("OBSERVATION_FORMAT_INVALID")

                satellite_id = observation.get("satellite_id")
                if satellite_id != EXPECTED_SATELLITE:
                    raise ValueError("SATELLITE_ID_INVALID")

                if observation.get("measurement_type") != "ocean_surface_wind":
                    raise ValueError("MEASUREMENT_TYPE_INVALID")

                seq = observation.get("sequence_number")
                if type(seq) is not int or seq < 1:
                    raise ValueError("SEQUENCE_NUMBER_INVALID")

                wind = observation.get("wind_speed_ms")
                if type(wind) not in (int, float):
                    raise ValueError("WIND_SPEED_INVALID")

                # 5. Anti-Replay, apres verification cryptographique.
                guard.check_and_record(observation)

                # 6. Decision de securite.
                trusted_observation = observation
                status = "ACCEPT"
                reason = "OK"
                accepted_count += 1
                print(
                    f"[ACCEPT] seq={seq} | satellite={satellite_id} | "
                    f"wind={wind} m/s | CRC32=OK | Ed25519=OK | AntiReplay=OK"
                )

            except ValueError as error:
                reason = str(error)
                rejected_count += 1
                print(f"[REJECT] Bloc #{received_count} | reason={reason}")

            finally:
                processing_ms = (time.perf_counter() - start) * 1000.0

                # 7. Journaliser la decision RF + securite ensemble.
                # Ne pas inclure d'identite non authentifiee.
                try:
                    event_id = monitor.record_event(
                        iq_bytes=len(iq_bytes),
                        status=status,
                        reason=reason,
                        metrics=metrics,
                        observation=trusted_observation,
                        scenario=SCENARIO,
                        processing_ms=processing_ms,
                        rf_level=rf_level,
                        rf_reasons=rf_reasons,
                    )
                    print(
                        f"[DB] Event #{event_id} saved | "
                        f"RF={rf_level} | {status} | {reason}"
                    )
                except Exception as error:
                    db_error_count += 1
                    print(f"[DB_ERROR] {type(error).__name__}: {error}")

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




