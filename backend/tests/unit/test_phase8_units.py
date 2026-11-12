"""Phase 8 pure tests: log redaction and context, production configuration rules, backup safety guards, retention."""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.core.logging import JsonFormatter, log_context, redact
from app.ops import backup

STRONG = "Vq8#tZ1m!Kp4@Wn7$Rc2^Lx9&Hb5*Jd3(Fs6)Gy0_Qe8+Ua1=Ot4"


# ---- logging -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, leaked", [
    ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl", "c2lnbmF0dXJlLXZhbHVl"),
    ("password=hunter2-secret and more", "hunter2-secret"),
    ("api_key: 'sk-live-0123456789abcdef'", "0123456789abcdef"),
    ("postgresql+psycopg://agentx_app:S3cr3t!@127.0.0.1/agentx", "S3cr3t!"),
    ("key sk-ant-api03-ABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
])
def test_secrets_are_redacted(raw, leaked):
    assert leaked not in redact(raw)


def _record(msg, fields=None, exc=None):
    r = logging.LogRecord("agentx.test", logging.ERROR, __file__, 1, msg, None, exc)
    if fields:
        r.fields = fields
    return json.loads(JsonFormatter().format(r))


def test_json_records_carry_context_and_redact_fields_and_tracebacks():
    with log_context(request_id="req-1", agent_id=7):
        out = _record("cycle failed", {"cycle_id": 3, "password": "x", "notes": "my private note", "user_id": "u"})
    assert out["request_id"] == "req-1" and out["agent_id"] == 7 and out["cycle_id"] == 3
    assert out["password"] == "<redacted>" and out["notes"] == "<redacted>" and out["user_id"] == "u"
    try:
        raise RuntimeError("db error password=TopSecret1")
    except RuntimeError:
        import sys

        out = _record("boom", exc=sys.exc_info())
    assert "TopSecret1" not in out["exc"] and out["error_category"] == "RuntimeError"
    assert "request_id" not in _record("outside")  # context does not leak past its scope


# ---- production configuration ------------------------------------------------------------------------------------


def production(**over) -> Settings:
    base = get_settings()
    values = dict(
        environment="production", db_host=base.db_host, db_port=base.db_port, db_user=base.db_user,
        db_password=SecretStr("a-long-production-password!"), db_name="agentx_prod", db_test_name="agentx_test",
        jwt_secret_key=SecretStr(STRONG), access_token_expire_minutes=30,
    )
    values.update(over)
    return Settings(_env_file=None, **values)


def test_a_sound_production_configuration_starts_with_safe_defaults():
    s = production()
    assert s.production_config_problems() == []
    assert s.api_docs_enabled is False and s.ingestion_operator_only is True and s.execution_mode == "disabled"


@pytest.mark.parametrize("over, fragment", [
    ({"jwt_secret_key": SecretStr("change-me-to-a-long-random-value-change-me-0123456789")}, "JWT_SECRET_KEY"),
    ({"jwt_secret_key": SecretStr("a" * 64)}, "JWT_SECRET_KEY"),
    ({"db_password": SecretStr("short")}, "DB_PASSWORD"),
    ({"db_name": "agentx_test"}, "DB_NAME"),
    ({"db_name": "agentx_staging"}, "DB_NAME"),
    ({"access_token_expire_minutes": 600}, "ACCESS_TOKEN_EXPIRE_MINUTES"),
    ({"enable_api_docs": True}, "ENABLE_API_DOCS"),
    ({"cors_allow_origins": ["*"]}, "CORS_ALLOW_ORIGINS"),
    ({"cors_allow_origins": ["http://app.example"]}, "CORS_ALLOW_ORIGINS"),
    ({"ingestion_requires_operator": False}, "INGESTION_REQUIRES_OPERATOR"),
    ({"marketdata_provider": "fixture"}, "MARKETDATA_PROVIDER"),
])
def test_unsafe_production_configuration_refuses_to_start(over, fragment):
    with pytest.raises(ValueError, match=fragment):
        production(**over)


def test_unknown_environment_is_refused_and_development_defaults_are_unchanged():
    with pytest.raises(ValueError):
        production(environment="prod")
    dev = get_settings().model_copy(update={"environment": "development"})
    assert dev.api_docs_enabled and not dev.ingestion_operator_only and dev.production_config_problems() == []
    staging = get_settings().model_copy(update={"environment": "staging"})
    assert staging.ingestion_operator_only and not staging.api_docs_enabled


def test_secret_values_never_appear_in_problem_messages():
    try:
        production(db_password=SecretStr("tiny"))
    except ValueError as exc:
        assert "tiny" not in str(exc)


# ---- backup guards and retention ---------------------------------------------------------------------------------


@pytest.mark.parametrize("target, ok", [
    ("agentx_restore_check", True), ("agentx", False), ("agentx_test", False), ("agentx_staging", False),
    ("agentx_staging_restore_check", False), ("restore", False), ("agentx_restore_check_x", False),
])
def test_restore_target_must_be_a_disposable_database(target, ok):
    s = get_settings()
    assert (backup.restore_target_problem(s, target) is None) is ok


def test_restore_check_refuses_before_touching_anything(tmp_path):
    with pytest.raises(backup.BackupError, match="disposable"):
        backup.restore_check(get_settings(), tmp_path / "x.dump", get_settings().db_name)


def test_retention_keeps_the_newest_and_the_young(tmp_path):
    now = time.time()
    ages_days = [0, 1, 5, 40, 50, 60]
    for i, age in enumerate(ages_days):
        p = tmp_path / f"agentx_agentx_{i}.dump"
        p.write_bytes(b"x")
        backup.manifest_path(p).write_text("{}")
        os.utime(p, (now - age * 86400, now - age * 86400))
    plan = backup.prune(tmp_path, keep_last=2, keep_days=30, now=datetime.fromtimestamp(now, UTC))
    assert sorted(plan["would_remove"]) == ["agentx_agentx_3.dump", "agentx_agentx_4.dump", "agentx_agentx_5.dump"]
    assert len(list(tmp_path.glob("*.dump"))) == 6  # dry run deletes nothing
    backup.prune(tmp_path, keep_last=2, keep_days=30, apply=True, now=datetime.fromtimestamp(now, UTC))
    assert sorted(p.name for p in tmp_path.glob("*.dump")) == [f"agentx_agentx_{i}.dump" for i in range(3)]
    assert not Path(tmp_path / "agentx_agentx_5.manifest.json").exists()
    with pytest.raises(backup.BackupError):
        backup.prune(tmp_path, keep_last=0, keep_days=1)
