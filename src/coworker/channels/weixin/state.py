from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from coworker.i18n import tr


@dataclass
class WeixinAccountState:
    cursor: str = ""
    context_tokens: dict[str, str] = field(default_factory=dict)


@dataclass
class WeixinState:
    accounts: dict[str, WeixinAccountState] = field(default_factory=dict)


class WeixinStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> WeixinState:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return WeixinState()
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(tr("channel.weixin.state_load_failed", error=error))
            return WeixinState()
        if not isinstance(payload, dict):
            return WeixinState()
        accounts = payload.get("accounts")
        if not isinstance(accounts, dict):
            return WeixinState()
        return WeixinState(
            accounts={
                str(account_id): _account_state(account)
                for account_id, account in accounts.items()
                if account_id and isinstance(account, dict)
            }
        )

    def save(self, state: WeixinState) -> None:
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "accounts": {
                            account_id: {
                                "cursor": account.cursor,
                                "context_tokens": account.context_tokens,
                            }
                            for account_id, account in state.accounts.items()
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except OSError as error:
            logger.warning(tr("channel.weixin.state_save_failed", error=error))
            temporary.unlink(missing_ok=True)


def _account_state(payload: dict[str, object]) -> WeixinAccountState:
    tokens = payload.get("context_tokens")
    return WeixinAccountState(
        cursor=str(payload.get("cursor") or ""),
        context_tokens={
            str(user_id): str(token)
            for user_id, token in tokens.items()
            if user_id and token
        }
        if isinstance(tokens, dict)
        else {},
    )
