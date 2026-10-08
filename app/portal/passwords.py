"""Password hashing with scrypt from the standard library: memory-hard, salted, no extra dependency.

Stored format: scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>, so parameters can be raised later and old
hashes still verify.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2**14, 8, 1, 32
MIN_LENGTH = 10


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return "$".join(("scrypt", str(N), str(R), str(P), _b64(salt), _b64(digest)))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = base64.b64decode(digest)
    actual = hashlib.scrypt(
        password.encode(),
        salt=base64.b64decode(salt),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)


# Verified against when the e-mail is unknown, so a failed login costs the same time either way and
# response timing does not reveal which addresses have accounts.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
