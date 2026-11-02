
import argparse
import csv
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import zmq

from groundlink_secure_common import (
    create_signed_frame,
    bpsk_modulate
)

CHANNEL_ADDRESS = "tcp://10.10.1.1:5555"

PRIVATE_KEY = (
    Path.home()
    / "groundlink"
    / "keys"
    / "satellite_private.pem"
)

DEFAULT_DATASET = Path("data/normalized/wind_observations_public.csv")
NUMBER_OF_FRAMES = 5

def select_dataset_rows(path, offset, count=NUMBER_OF_FRAMES):
    if offset < 0:
        raise ValueError("dataset offset must be non-negative")
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {"source_observation_id", "source_timestamp_utc", "latitude", "longitude", "wind_speed_ms", "wind_direction_deg"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("normalized dataset has missing required fields")
    selected = rows[offset:offset + count]
    if len(selected) != count:
        raise ValueError(f"dataset offset {offset} does not have {count} consecutive observations")
    return selected

def build_observation(row, session_id, sequence_number, transmitted_at=None):
    timestamp = (transmitted_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if row is None:
        return {"satellite_id": "SAT-001", "session_id": session_id,
                "observation_id": 1000 + sequence_number, "sequence_number": sequence_number,
                "measurement_type": "ocean_surface_wind", "data_origin": "simulated",
                "latitude": 35.8, "longitude": 12.45, "wind_speed_ms": 18.5,
                "wind_direction_deg": 240, "timestamp": timestamp}
    acquisition = datetime.fromisoformat(row["source_timestamp_utc"].replace("Z", "+00:00"))
    if acquisition.tzinfo is None or acquisition.utcoffset() != timezone.utc.utcoffset(acquisition):
        raise ValueError("source_timestamp_utc must be UTC")
    return {"satellite_id": "SAT-001", "session_id": session_id,
            "observation_id": int(acquisition.strftime("%Y%m%d%H")), "sequence_number": sequence_number,
            "measurement_type": "ocean_surface_wind", "data_origin": "nasa_power",
            "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
            "wind_speed_ms": float(row["wind_speed_ms"]), "wind_direction_deg": float(row["wind_direction_deg"]),
            "timestamp": timestamp}

def run(mode="synthetic", dataset=DEFAULT_DATASET, dataset_offset=0, dry_run=False):

    if not PRIVATE_KEY.is_file():
        raise FileNotFoundError(
            f"Clé privée introuvable : {PRIVATE_KEY}"
        )

    rows = select_dataset_rows(dataset, dataset_offset) if mode == "dataset" else [None] * NUMBER_OF_FRAMES
    context = socket = None
    if not dry_run:
        context = zmq.Context()
        socket = context.socket(zmq.PUSH)
        socket.setsockopt(zmq.IMMEDIATE, 1)
        socket.setsockopt(zmq.SNDTIMEO, 5000)
        socket.setsockopt(zmq.LINGER, 1000)
        socket.connect(CHANNEL_ADDRESS)

    session_id = uuid.uuid4().hex

    print("[SATELLITE] Secure BPSK transmitter")
    print("[SATELLITE] Signature : Ed25519")
    print(f"[SATELLITE] Mode : {mode} | Dry-run : {dry_run}")
    print(f"[SATELLITE] Session : {session_id}")

    # Laisser le temps à ZeroMQ d'établir la connexion
    if socket is not None:
        time.sleep(1.5)

    sent_count = 0

    try:

        for seq, row in enumerate(rows, 1):
            observation = build_observation(row, session_id, seq)

            # 1. Signer l'observation et créer la trame
            frame = create_signed_frame(
                observation,
                PRIVATE_KEY
            )

            # 2. Moduler la trame en BPSK
            iq_samples = bpsk_modulate(frame)

            # 3. Transmettre vers VM3
            try:
                if socket is not None:
                    socket.send(iq_samples.tobytes())

            except zmq.Again:
                print(
                    f"[ERROR] Timeout ZeroMQ : seq={seq}"
                )
                break

            sent_count += 1

            print(
                f"[TX] seq={seq} | "
                f"wind={observation['wind_speed_ms']} m/s | "
                f"frame={len(frame)} octets | "
                f"IQ={len(iq_samples)} samples | "
                "Ed25519=SIGNED"
            )

            if socket is not None:
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[SATELLITE] Arrêt manuel")

    finally:
        if socket is not None:
            socket.close()
        if context is not None:
            context.term()

        print(
            f"[SATELLITE] BILAN : "
            f"{sent_count}/{NUMBER_OF_FRAMES} "
            + ("trames générées localement" if dry_run else "trames remises à ZeroMQ")
        )

    return sent_count

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("synthetic", "dataset"), default="synthetic")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dataset-offset", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.mode, args.dataset, args.dataset_offset, args.dry_run)

if __name__ == "__main__":
    main()
