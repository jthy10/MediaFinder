"""Secrets-at-rest encryption, optional admin password and session cookies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:"
SESSION_DAYS = 30
MIN_PASSWORD = 8


class SecretBox:
    """Encrypts credentials stored in config.json.

    The key comes from ``MEDIAFINDER_SECRET_KEY`` if set (so it can live outside the data
    folder, e.g. in your compose file / secrets manager), otherwise from ``secret.key``
    next to the config file (created on first use, owner-only).
    """

    def __init__(self, directory: str, env_key: str = ""):
        if env_key:
            raw = hashlib.sha256(env_key.encode()).digest()
        else:
            path = os.path.join(directory, "secret.key")
            if os.path.exists(path):
                raw = base64.urlsafe_b64decode(open(path, "rb").read().strip())
            else:
                raw = os.urandom(32)
                os.makedirs(directory, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(base64.urlsafe_b64encode(raw))
        self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        self.session_key = hashlib.sha256(b"mediafinder-session|" + raw).digest()

    def encrypt(self, value: str) -> str:
        if not value or value.startswith(PREFIX):
            return value
        return PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not value or not value.startswith(PREFIX):
            return value  # legacy plaintext; re-encrypted on next save
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise ValueError("Cannot decrypt stored credentials: the secret key changed. "
                             "Restore the key or re-enter your connections.")


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return {"salt": salt.hex(), "hash": h.hex()}


def check_password(password: str, record: dict) -> bool:
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]), n=2**14, r=8, p=1)
    return hmac.compare_digest(h.hex(), record["hash"])


class Auth:
    """Optional single-admin login. Password comes from env (locked) or the UI (stored hashed)."""

    def __init__(self, store, box: SecretBox, env_password: str = ""):
        self.store, self.box, self.env_password = store, box, env_password
        self._fails: dict[str, list[float]] = {}

    @property
    def required(self) -> bool:
        return bool(self.env_password or self.store.snapshot().get("auth"))

    def verify(self, password: str) -> bool:
        if self.env_password:
            return hmac.compare_digest(password.encode(), self.env_password.encode())
        rec = self.store.snapshot().get("auth")
        return bool(rec) and check_password(password, rec)

    def set_password(self, new: str) -> None:
        rec = hash_password(new) if new else None
        self.store.mutate(lambda d: d.__setitem__("auth", rec))

    # --- sessions: "<expiry>.<random>.<hmac>" ---
    def make_token(self) -> str:
        body = f"{int(time.time()) + SESSION_DAYS * 86400}.{secrets.token_hex(8)}"
        return f"{body}.{self._sig(body)}"

    def _sig(self, body: str) -> str:
        return hmac.new(self.box.session_key, body.encode(), hashlib.sha256).hexdigest()

    def valid_token(self, token: str | None) -> bool:
        try:
            exp, rnd, sig = (token or "").split(".")
            return hmac.compare_digest(sig, self._sig(f"{exp}.{rnd}")) and int(exp) > time.time()
        except ValueError:
            return False

    # --- brute-force throttle: 5 failures per minute per client ---
    def throttled(self, client: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(client, []) if now - t < 60]
        self._fails[client] = recent
        return len(recent) >= 5

    def record_failure(self, client: str) -> None:
        self._fails.setdefault(client, []).append(time.time())
