"""AES-256-GCM vault for wallet private keys.

* master key: 32 random bytes in a file OUTSIDE the repo/DB (chmod 600); DB leak alone reveals nothing.
* AAD binds each ciphertext to (user_id, wallet address): a ciphertext copied to another row fails to decrypt.
* plaintext keys never touch disk; they are handed to the engine wrapper over a pipe.
"""
import os
import re
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account

VERSION = b"\x01"
_HEX_KEY = re.compile(r"(0x)?[0-9a-fA-F]{64}")


class VaultError(Exception):
    pass


def create_master_key(path: str) -> None:
    if os.path.exists(path):
        raise VaultError(f"{path} already exists — refusing to overwrite (would orphan every stored key)")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(32))


def load_master_key(path: str) -> bytes:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise VaultError(f"{path} must be chmod 600")
    k = open(path, "rb").read()
    if len(k) != 32:
        raise VaultError("master key must be exactly 32 bytes")
    return k


def normalize_private_key(text: str) -> str:
    """Return 0x-prefixed lowercase key or raise. Rejects mnemonics and anything that is not 32 bytes hex."""
    v = (text or "").strip().strip('"').strip("'")
    if not _HEX_KEY.fullmatch(v):
        raise VaultError("not a 64-hex-char private key")
    v = "0x" + v[-64:].lower()
    if int(v, 16) == 0:
        raise VaultError("zero key")
    Account.from_key(v)                      # raises if >= secp256k1 order
    return v


class Vault:
    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise VaultError("bad master key")
        self._a = AESGCM(master_key)

    @staticmethod
    def _aad(user_id: int, address: str) -> bytes:
        return f"osh|{int(user_id)}|{address.lower()}".encode()

    def encrypt(self, user_id: int, private_key: str) -> tuple:
        pk = normalize_private_key(private_key)
        addr = Account.from_key(pk).address
        nonce = os.urandom(12)
        blob = VERSION + nonce + self._a.encrypt(nonce, pk.encode(), self._aad(user_id, addr))
        return addr, blob

    def decrypt(self, user_id: int, address: str, blob: bytes) -> str:
        if not blob or blob[:1] != VERSION:
            raise VaultError("unknown blob version")
        try:
            pk = self._a.decrypt(blob[1:13], blob[13:], self._aad(user_id, address)).decode()
        except Exception:
            raise VaultError("decryption failed (wrong master key, user or address)")
        if Account.from_key(pk).address.lower() != address.lower():
            raise VaultError("decrypted key does not match address")
        return pk

    # generic secrets (OpenSea API keys); AAD binds to user + purpose
    def seal(self, user_id: int, purpose: str, secret: str) -> bytes:
        nonce = os.urandom(12)
        return VERSION + nonce + self._a.encrypt(nonce, secret.encode(), self._aad(user_id, purpose))

    def open(self, user_id: int, purpose: str, blob: bytes) -> str:
        if not blob or blob[:1] != VERSION:
            raise VaultError("unknown blob version")
        try:
            return self._a.decrypt(blob[1:13], blob[13:], self._aad(user_id, purpose)).decode()
        except Exception:
            raise VaultError("decryption failed")
