
import json
import struct
import zlib
from pathlib import Path

import numpy as np

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

PREAMBLE = b"\x1a\xcf\xfc\x1d"
SAMPLES_PER_SYMBOL = 4
SIGNATURE_SIZE = 64
MAX_PAYLOAD_BYTES = 4096

def canonical_bytes(observation):
    return json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

def create_signed_frame(observation, private_key_path):

    payload = canonical_bytes(observation)

    if not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
        raise ValueError("PAYLOAD_LENGTH_INVALID")

    private_key = serialization.load_pem_private_key(
        Path(private_key_path).read_bytes(),
        password=None
    )

    signature = private_key.sign(payload)

    body = (
        struct.pack("!H", len(payload))
        + payload
        + signature
    )

    crc = zlib.crc32(body) & 0xffffffff

    return PREAMBLE + body + struct.pack("!I", crc)

def verify_signed_frame(frame, public_key_path):

    if len(frame) < 4 + 2 + SIGNATURE_SIZE + 4:
        raise ValueError("FRAME_TOO_SHORT")

    if frame[:4] != PREAMBLE:
        raise ValueError("PREAMBLE_INVALID")

    size = struct.unpack("!H", frame[4:6])[0]

    if not 0 < size <= MAX_PAYLOAD_BYTES:
        raise ValueError("PAYLOAD_LENGTH_INVALID")

    expected_size = 4 + 2 + size + SIGNATURE_SIZE + 4

    if len(frame) != expected_size:
        raise ValueError("FRAME_LENGTH_INVALID")

    crc_expected = struct.unpack("!I", frame[-4:])[0]

    crc_actual = zlib.crc32(frame[4:-4]) & 0xffffffff

    if crc_expected != crc_actual:
        raise ValueError("CRC_INVALID")

    payload = frame[6:6 + size]
    signature = frame[6 + size:-4]

    public_key = serialization.load_pem_public_key(
        Path(public_key_path).read_bytes()
    )

    try:
        public_key.verify(signature, payload)

    except InvalidSignature as exc:
        raise ValueError("SIGNATURE_INVALID") from exc

    return json.loads(payload.decode("utf-8"))

def bpsk_modulate(frame):

    bits = np.unpackbits(
        np.frombuffer(frame, dtype=np.uint8)
    )

    symbols = 1.0 - 2.0 * bits.astype(np.float32)

    return np.repeat(
        symbols, SAMPLES_PER_SYMBOL
    ).astype(np.complex64)

def bpsk_demodulate(iq_bytes):

    if len(iq_bytes) % np.dtype(np.complex64).itemsize:
        raise ValueError("IQ_BYTE_LENGTH_INVALID")

    iq = np.frombuffer(iq_bytes, dtype=np.complex64)

    if iq.size == 0 or iq.size % SAMPLES_PER_SYMBOL:
        raise ValueError("IQ_SYMBOL_ALIGNMENT_INVALID")

    symbols = iq.reshape(
        -1, SAMPLES_PER_SYMBOL
    ).real.mean(axis=1)

    bits = (symbols < 0).astype(np.uint8)

    return np.packbits(bits).tobytes()
