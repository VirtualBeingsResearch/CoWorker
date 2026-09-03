from coworker.core.communication_tokens import (
    CONTROL_PARTICIPANT_ID,
    PRIMARY_PARTICIPANT_ID,
    participant_id_for_token_name,
    validate_token_name,
)
from coworker.core.config import APIConfig
from coworker.i18n import locale_context


def test_validate_token_name_accepts_bot_style_ids() -> None:
    assert validate_token_name("cursor") == "cursor"
    assert validate_token_name("webui-2") == "webui-2"
    assert participant_id_for_token_name("cursor") == "openai:cursor"


def test_validate_token_name_rejects_reserved_and_invalid() -> None:
    with locale_context("en"):
        for name in ("api", "control", "API", "1bad", "has space", ""):
            try:
                validate_token_name(name)
            except ValueError:
                continue
            raise AssertionError(name)


def test_primary_and_control_addresses() -> None:
    assert PRIMARY_PARTICIPANT_ID == "openai:api"
    assert CONTROL_PARTICIPANT_ID == "openai:control"


def test_api_config_drops_empty_extra_secrets_and_rejects_reserved() -> None:
    config = APIConfig(
        _env_file=None,
        communication_tokens={"cursor": "cwct_v1_secret", "webui": "  "},
    )
    assert config.communication_tokens == {"cursor": "cwct_v1_secret"}
    try:
        APIConfig(_env_file=None, communication_tokens={"api": "nope"})
    except Exception:
        return
    raise AssertionError("reserved extra name should fail")
