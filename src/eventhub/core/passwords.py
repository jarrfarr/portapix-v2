"""Password hashing shared by the dashboard, installer and copyparty sync.

Dashboard passwords use argon2id via argon2-cffi.  Copyparty can verify
argon2id hashes if given the same salt and algorithm settings, so EventHub
stores a copyparty-specific hash next to each user instead of keeping a
plaintext password for the file server.
"""

from __future__ import annotations

import os

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type as ArgonType
from argon2.low_level import hash_secret

# Tests opt into cheap parameters; production always uses argon2 defaults.
_FAST = os.environ.get("EVENTHUB_FAST_HASH") == "1"

_hasher = (
    PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    if _FAST
    else PasswordHasher()
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def copyparty_hash(password: str, salt: str) -> str:
    """Reproduce copyparty's `--ah-alg argon2` hash format (leading '+')."""

    digest = hash_secret(
        secret=password.encode("utf-8"),
        salt=salt.encode("utf-8"),
        time_cost=1 if _FAST else 3,
        memory_cost=(8 if _FAST else 256) * 1024,
        parallelism=1 if _FAST else 4,
        hash_len=24,
        type=ArgonType.ID,
        version=19,
    )
    encoded = digest.split(b"$")[-1].decode("ascii")
    encoded = encoded.replace("/", "_").replace("+", "-")
    return "+" + encoded
