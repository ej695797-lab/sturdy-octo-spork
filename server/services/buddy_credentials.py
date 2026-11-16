"""Credential encoding verified against the supplied live79 client's helper."""
import base64
import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# EnvironmentVariables.test in the supplied APK, not a player credential.
_PREFIX = '6w1Czhl847EO'


def encrypt_credential(plaintext: str, account_id: str, nonce: str) -> str:
    # live79 0x509f74c concatenates the baked prefix, account ID, and login
    # oculusNonce before 0x509ef8c decrypts. The login state machine at
    # 0x53dada8 writes this argument to oculusNonce, not password.
    key = (_PREFIX + account_id + nonce).encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", key, b"Ivan Medvedev", 1000, 48)
    raw = plaintext.encode("utf-16le")
    padding = 16 - len(raw) % 16
    raw += bytes([padding]) * padding
    enc = Cipher(algorithms.AES(derived[:32]), modes.CBC(derived[32:])).encryptor()
    # The game's encoder replaces '+' with spaces; its decoder reverses this.
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode().replace("+", " ")
