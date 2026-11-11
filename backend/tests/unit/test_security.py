import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_is_argon2id_and_salted():
    h1, h2 = hash_password("a-long-test-password"), hash_password("a-long-test-password")
    assert h1.startswith("$argon2id$")
    assert h1 != h2  # per-hash salt
    assert "a-long-test-password" not in h1


def test_verify_password():
    h = hash_password("a-long-test-password")
    assert verify_password("a-long-test-password", h)
    assert not verify_password("wrong-password-here", h)


def test_verify_password_with_no_hash_is_always_false():
    # Used for unknown emails; must never succeed, even for the internal dummy's own input.
    assert not verify_password("anything", None)
    assert not verify_password("agent-x-timing-equalisation", None)


def test_verify_password_rejects_garbage_hash():
    assert not verify_password("x", "not-a-hash")


def test_token_round_trip(settings):
    uid = uuid.uuid4()
    assert decode_access_token(create_access_token(uid, settings), settings) == uid


def test_expired_token_rejected(settings):
    old = datetime.now(UTC) - timedelta(minutes=settings.access_token_expire_minutes + 5)
    token = create_access_token(uuid.uuid4(), settings, now=old)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_tampered_token_rejected(settings):
    token = create_access_token(uuid.uuid4(), settings)
    head, payload, sig = token.split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{head}.{payload}.{flipped}", settings)


def test_wrong_secret_rejected(settings):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "iss": settings.jwt_issuer, "iat": now, "exp": now + timedelta(minutes=5)},
        "x" * 40,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_alg_none_rejected(settings):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "iss": settings.jwt_issuer, "iat": now, "exp": now + timedelta(minutes=5)},
        key=None,
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


@pytest.mark.parametrize("missing", ["sub", "exp", "iat", "iss"])
def test_token_missing_required_claim_rejected(settings, missing):
    now = datetime.now(UTC)
    claims = {"sub": str(uuid.uuid4()), "iss": settings.jwt_issuer, "iat": now, "exp": now + timedelta(minutes=5)}
    del claims[missing]
    token = jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_wrong_issuer_rejected(settings):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "iss": "someone-else", "iat": now, "exp": now + timedelta(minutes=5)},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_non_uuid_subject_rejected(settings):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": "not-a-uuid", "iss": settings.jwt_issuer, "iat": now, "exp": now + timedelta(minutes=5)},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)
