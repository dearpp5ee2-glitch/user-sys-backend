"""Password hashing and bearer tokens, standard library only."""

import hashlib
import hmac
import os
import secrets

ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260000
SALT_BYTES = 16
TOKEN_BYTES = 32


def hash_password(password):
    """Return `pbkdf2_sha256$iterations$salt$hash` for a plaintext password."""
    salt = os.urandom(SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    )
    return "%s$%d$%s$%s" % (ALGORITHM, PBKDF2_ITERATIONS, salt, digest.hex())


def verify_password(password, stored):
    """Constant-time check of a password against a stored hash."""
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        rounds = int(iterations)
    except (AttributeError, ValueError):
        return False
    if algorithm != ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), rounds
    )
    return hmac.compare_digest(digest.hex(), expected)


def new_token():
    """A random bearer token handed to the client. Only its hash is stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Verifying against this when the username is unknown keeps login timing flat,
# so the endpoint does not reveal which accounts exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
