
import json
import struct
import zlib

import numpy as np
import zmq

PREAMBLE = b"\x1a\xcf\xfc\x1d"
SAMPLES_PER_SYMBOL = 4

def bpsk_demodulate(iq_data):
    if len(iq_data) % 8 != 0:
        raise ValueError("Taille IQ invalide")

    iq = np.frombuffer(
        iq_data, dtype=np.complex64
    )

    if len(iq) % SAMPLES_PER_SYMBOL != 0:
        raise ValueError("Nombre d'échantillons invalide")

    symbols = iq.reshape(
        -1, SAMPLES_PER_SYMBOL
    ).real.mean(axis=1)

    bits = (symbols < 0).astype(np.uint8)

    return np.packbits(bits).tobytes()

def decode_frame(frame):
    if len(frame) < 10:
        raise ValueError("Trame trop courte")

    if frame[:4] != PREAMBLE:
        raise ValueError("Préambule incorrect")

    payload_length = struct.unpack(
        "!H", frame[4:6]
    )[0]

    expected_length = 6 + payload_length + 4

    if len(frame) != expected_length:
        raise ValueError("Longueur de trame incorrecte")

    payload = frame[6:6 + payload_length]

    received_crc = struct.unpack(
        "!I", frame[-4:]
    )[0]

    calculated_crc = zlib.crc32(payload)

    if received_crc != calculated_crc:
        raise ValueError("CRC32 incorrect")

    return json.loads(payload.decode("utf-8"))

context = zmq.Context()
receiver = context.socket(zmq.PULL)
receiver.bind("tcp://10.10.2.10:5556")

print("[STATION] Récepteur BPSK actif")
print("[STATION] En attente des signaux IQ...")

try:
    while True:
        iq_data = receiver.recv()

        try:
            frame = bpsk_demodulate(iq_data)
            observation = decode_frame(frame)

            print("\n[STATION] Trame BPSK reçue")
            print("[STATION] Préambule : OK")
            print("[STATION] CRC32 : OK")
            print("[STATION] Observation :")
            print(observation)

        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            print(f"[STATION] Trame rejetée : {error}")

except KeyboardInterrupt:
    print("\n[STATION] Arrêt demandé")

finally:
    receiver.close(linger=0)
    context.term()
