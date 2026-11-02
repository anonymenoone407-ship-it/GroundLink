
import json
import struct
import time
import zlib
from datetime import datetime, timezone

import numpy as np
import zmq

PREAMBLE = b"\x1a\xcf\xfc\x1d"
SAMPLES_PER_SYMBOL = 4

def create_frame(observation):
    payload = json.dumps(observation).encode("utf-8")

    length = struct.pack("!H", len(payload))
    crc = struct.pack("!I", zlib.crc32(payload))

    return PREAMBLE + length + payload + crc

def bpsk_modulate(frame):
    bits = np.unpackbits(
        np.frombuffer(frame, dtype=np.uint8)
    )

    # BPSK : 0 -> +1, 1 -> -1
    symbols = 1.0 - 2.0 * bits.astype(np.float32)

    # 4 échantillons par symbole
    iq = np.repeat(
        symbols, SAMPLES_PER_SYMBOL
    ).astype(np.complex64)

    return iq

context = zmq.Context()
sender = context.socket(zmq.PUSH)

sender.setsockopt(zmq.IMMEDIATE, 1)
sender.setsockopt(zmq.SNDTIMEO, 5000)

sender.connect("tcp://10.10.1.1:5555")

print("[SATELLITE] Émetteur BPSK prêt")

try:
    for sequence in range(1, 6):
        observation = {
            "satellite_id": "SAT-001",
            "observation_id": 1000 + sequence,
            "sequence_number": sequence,
            "latitude": 35.8,
            "longitude": 12.45,
            "wind_speed_ms": 18.5,
            "wind_direction_deg": 240,
            "acquisition_time": datetime.now(
                timezone.utc
            ).isoformat()
        }

        frame = create_frame(observation)
        iq = bpsk_modulate(frame)

        sender.send(iq.tobytes())

        print(
            f"[SATELLITE] Trame {sequence} envoyée "
            f"| {len(iq)} échantillons IQ"
        )

        time.sleep(0.5)

except zmq.Again:
    print("[ERREUR] Canal non disponible")

finally:
    sender.close(linger=2000)
    context.term()
