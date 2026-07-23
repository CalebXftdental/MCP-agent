"""Password hashing for dashboard logins.

Uses `hashlib.scrypt` -- a memory-hard KDF in the Python standard library, so there
is no C-extension build dependency (argon2-cffi needs a toolchain on Windows).
scrypt is an appropriate password hash: slow + memory-hard defeats brute force on
low-entropy human passwords. (API keys, being high-entropy, use fast SHA-256 in
store/keys.py -- different problem, different tool.)

Stored format:  scrypt$<n>$<r>$<p>$<b64salt>$<b64hash>
Verification recomputes with the stored parameters and compares in constant time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N = 2 ** 14      # CPU/memory cost
_R = 8            # block size
_P = 1            # parallelization
_DKLEN = 32
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(_unb64(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64(dk), hash_b64)
