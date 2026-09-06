"""Salted local password hashing using PBKDF2-HMAC-SHA256."""
import hashlib
import hmac
import secrets

ITERATIONS = 600_000
MAX_PASSWORD_LENGTH = 1024


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 1 <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError("Invalid password length")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}'


def verify_password(password: str, encoded: str | None) -> bool:
    if not isinstance(password, str) or not 1 <= len(password) <= MAX_PASSWORD_LENGTH or not encoded:
        return False
    try:
        scheme, rounds, salt, expected = encoded.split('$')
        if scheme != 'pbkdf2_sha256' or int(rounds) != ITERATIONS or len(salt) != 32 or len(expected) != 64:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False


# Unknown accounts still perform a password derivation before failing.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
