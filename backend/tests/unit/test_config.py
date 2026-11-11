import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = dict(
    db_host="h",
    db_port=5432,
    db_user="u",
    db_password="p@ss/word#1",
    db_name="a",
    db_test_name="a_test",
    jwt_secret_key="s" * 40,
    access_token_expire_minutes=5,
)


def make(**over):
    return Settings(_env_file=None, **{**BASE, **over})


def test_short_jwt_secret_rejected():
    with pytest.raises(ValidationError):
        make(jwt_secret_key="short")


def test_asymmetric_or_none_algorithm_rejected():
    for alg in ("none", "RS256"):
        with pytest.raises(ValidationError):
            make(jwt_algorithm=alg)


def test_non_positive_token_lifetime_rejected():
    with pytest.raises(ValidationError):
        make(access_token_expire_minutes=0)


def test_special_characters_in_password_survive_url_building():
    url = make().database_url
    assert url.password == "p@ss/word#1"
    assert url.database == "a"
    assert make().test_database_url.database == "a_test"


def test_secrets_are_not_leaked_by_repr():
    text = repr(make())
    assert "p@ss/word#1" not in text
    assert "s" * 40 not in text
