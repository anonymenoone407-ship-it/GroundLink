from replay_guard import ReplayGuard
import zmq

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey
)

from groundlink_secure_common import (
    bpsk_demodulate,
    verify_signed_frame
)


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


# ==========================================
# VERIFICATION DU TRUST ANCHOR
# ==========================================

def check_trust_anchor():

    if not PUBLIC_KEY.is_file():
        raise FileNotFoundError(
            f"Clé publique introuvable : {PUBLIC_KEY}"
        )

    public_key = serialization.load_pem_public_key(
        PUBLIC_KEY.read_bytes()
    )

    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError(
            "Le Trust Anchor n'est pas une clé Ed25519."
        )

    print("[STATION] Trust Anchor Ed25519 chargé")


# ==========================================
# RECEPTION SECURISEE
# ==========================================

def main():

    check_trust_anchor()
    guard = ReplayGuard()

    print("[STATION] Anti-Replay SQLite activé")
    print(f"[STATION] Database : {guard.db_path}")


    context = zmq.Context()

    socket = context.socket(zmq.PULL)

    socket.setsockopt(zmq.LINGER, 0)

    socket.setsockopt(zmq.RCVTIMEO, 1000)

    socket.bind(STATION_ADDRESS)

    print("[STATION] Secure BPSK Receiver")
    print(f"[STATION] Écoute : {STATION_ADDRESS}")
    print("[STATION] Attente des trames IQ...")
    print()

    received_count = 0
    accepted_count = 0
    rejected_count = 0

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

            try:

                # 1. Démodulation BPSK
                frame = bpsk_demodulate(
                    iq_bytes
                )

                # 2. Vérification CRC32
                # 3. Vérification Ed25519
                observation = verify_signed_frame(
                    frame,
                    PUBLIC_KEY
                )

                # 4. Vérification de l'identité attendue
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

                if observation.get("measurement_type") != "ocean_surface_wind":
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

                if not isinstance(wind, (int, float)):
                    raise ValueError(
                        "WIND_SPEED_INVALID"
                    )

                # 5. Observation acceptée


                # Anti-Replay après CRC32, Ed25519
                # et validation des champs
                guard.check_and_record(observation)
                accepted_count += 1

                print(
                    f"[ACCEPT] seq={seq} | "
                    f"satellite={satellite_id} | "
                    f"wind={wind} m/s | "
		    f"CRC32=OK | Ed25519=OK | AntiReplay=OK"                )

                print()

            except ValueError as error:

                rejected_count += 1

                print(
                    f"[REJECT] Bloc #{received_count} | "
                    f"reason={error}"
                )

                print()

    except KeyboardInterrupt:

        print("\n[STATION] Arrêt manuel")

    finally:

        socket.close()
        context.term()

        print()
        print("========== BILAN STATION ==========")

        print(
            f"IQ reçus : {received_count}"
        )

        print(
            f"ACCEPT : {accepted_count}"
        )

        print(
            f"REJECT : {rejected_count}"
        )

        print("===================================")


if __name__ == "__main__":
    main()
