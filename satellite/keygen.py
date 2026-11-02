
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey
)
from cryptography.hazmat.primitives import serialization

KEY_DIR = Path.home() / "groundlink" / "keys"

PRIVATE_PATH = KEY_DIR / "satellite_private.pem"
PUBLIC_PATH = KEY_DIR / "satellite_public.pem"

KEY_DIR.mkdir(parents=True, exist_ok=True)

os.chmod(KEY_DIR, 0o700)

# Éviter de remplacer une clé existante
if PRIVATE_PATH.exists() or PUBLIC_PATH.exists():
    raise SystemExit(
        "[ERROR] Clés existantes : génération annulée."
    )

# Génération de la paire de clés
private_key = Ed25519PrivateKey.generate()
public_key = private_key.public_key()

# Export de la clé privée
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

PRIVATE_PATH.write_bytes(private_bytes)
os.chmod(PRIVATE_PATH, 0o600)

# Export de la clé publique
public_bytes = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)

PUBLIC_PATH.write_bytes(public_bytes)

print("[KEYGEN] Paire Ed25519 générée")
print("[KEYGEN] Clé privée sécurisée sur Satellite")
print("[KEYGEN] Clé publique prête pour Station")
