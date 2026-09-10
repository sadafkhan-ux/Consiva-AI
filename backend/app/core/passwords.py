"""Password hashing for first-party authentication.

bcrypt, not a plain SHA family hash: a password is low-entropy human input, so
the defence has to be deliberate slowness. SHA-256 is fast by design, which is
exactly wrong here -- a GPU can try billions of SHA-256 guesses per second
against a stolen hash, but only thousands of bcrypt guesses at cost factor 12.

bcrypt also salts every hash internally, so two users choosing the same password
get different stored values and a precomputed rainbow table is useless.
"""

import bcrypt

# 2**12 rounds. Roughly 250ms per verification on typical server hardware --
# slow enough to make offline cracking expensive, fast enough that a login
# doesn't feel broken. Raise as hardware improves; existing hashes keep working
# because the cost is stored inside each hash.
BCRYPT_ROUNDS = 12

# bcrypt truncates at 72 BYTES and silently ignores the rest, which would mean
# two different long passwords sharing a hash. Reject rather than truncate, so
# nobody ends up with a weaker password than they believe they set.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 12


class PasswordError(ValueError):
    """The supplied password cannot be used (too short, or too long to hash)."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt ignores anything beyond that, which would silently weaken it)"
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison, courtesy of bcrypt's own checkpw.

    Returns False rather than raising on a malformed stored hash: a corrupt row
    must read as "wrong password", never as a 500 that tells an attacker the
    account exists.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
