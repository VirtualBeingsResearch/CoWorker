from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from coworker.channels.weixin.media import (
    WEIXIN_MEDIA_MAX_BYTES,
    WeixinMediaTooLargeError,
    decrypt_aes_ecb_file,
)
from coworker.version import __version__

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_BOT_TYPE = "3"
DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 35.0
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_ILINK_APP_ID = "bot"
_MESSAGE_TYPE_BOT = 2
_MESSAGE_STATE_FINISH = 2
_TEXT_ITEM_TYPE = 1


@dataclass(frozen=True)
class WeixinCredentials:
    bot_id: str
    token: str
    base_url: str = DEFAULT_BASE_URL
    user_id: str = ""


class WeixinClient:
    """Async Tencent iLink JSON client used by the personal-Weixin channel."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._http = httpx.AsyncClient(transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def start_login(self, local_tokens: list[str] | None = None) -> dict[str, Any]:
        return await self._post_public(
            f"ilink/bot/get_bot_qrcode?bot_type={DEFAULT_BOT_TYPE}",
            {"local_token_list": list(local_tokens or [])[-10:]},
        )

    async def poll_login(
        self,
        qrcode: str,
        verify_code: str = "",
    ) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        response = await self._http.get(
            self._url(endpoint),
            headers=self._common_headers(),
            timeout=DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    async def get_updates(
        self,
        cursor: str,
        timeout_seconds: float = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self._post(
            "ilink/bot/getupdates",
            {
                "get_updates_buf": cursor,
                "base_info": self._base_info(),
            },
            timeout_seconds=timeout_seconds,
        )

    async def send_text(
        self,
        user_id: str,
        text: str,
        context_token: str = "",
    ) -> None:
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": user_id,
                "client_id": f"coworker-{secrets.token_hex(12)}",
                "message_type": _MESSAGE_TYPE_BOT,
                "message_state": _MESSAGE_STATE_FINISH,
                "item_list": [
                    {
                        "type": _TEXT_ITEM_TYPE,
                        "text_item": {"text": text},
                    }
                ],
                "context_token": context_token or None,
            },
            "base_info": self._base_info(),
        }
        response = await self._post("ilink/bot/sendmessage", payload)
        if response.get("ret") not in (None, 0):
            raise RuntimeError(
                f"Weixin sendmessage ret={response.get('ret')} "
                f"errmsg={response.get('errmsg', '')}"
            )

    async def download_media(
        self,
        destination: Path,
        *,
        encrypt_query_param: str = "",
        aes_key: bytes | None = None,
        full_url: str = "",
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        max_bytes: int = WEIXIN_MEDIA_MAX_BYTES,
    ) -> int:
        """Download one CDN media item into ``destination``; return its plaintext size.

        The encrypted payload streams to a sibling ``.enc`` file and is then
        decrypted in place chunk by chunk, so memory stays bounded regardless
        of size. CDN payloads are AES-128-ECB encrypted; when ``aes_key`` is
        ``None`` the bytes are kept as downloaded. Mirrors the official
        ``@tencent-weixin/openclaw-weixin`` client: ``full_url`` wins over a
        URL built from ``encrypt_query_param``, and the request carries no
        iLink auth headers. Oversized payloads raise
        ``WeixinMediaTooLargeError`` carrying the observed size and the limit.
        """
        if full_url:
            url = full_url
        else:
            url = (
                f"{cdn_base_url.rstrip('/')}/download"
                f"?encrypted_query_param={quote(encrypt_query_param, safe='')}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload_path = destination.with_name(f"{destination.name}.enc")
        try:
            received = 0
            with payload_path.open("wb") as handle:
                async with self._http.stream("GET", url) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise WeixinMediaTooLargeError(int(declared), max_bytes)
                    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        received += len(chunk)
                        if received > max_bytes:
                            raise WeixinMediaTooLargeError(
                                max(received, int(declared) if declared.isdigit() else 0),
                                max_bytes,
                            )
                        handle.write(chunk)
            if aes_key is not None:
                size = decrypt_aes_ecb_file(payload_path, destination, aes_key)
            else:
                size = payload_path.stat().st_size
                payload_path.replace(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            # 密文副本只服务于解密/改名：无论成功与否都不能留在附件目录里，
            # 否则每个加密附件都会多出一份等大的 .enc 文件。
            payload_path.unlink(missing_ok=True)
        return size

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        response = await self._http.post(
            self._url(endpoint),
            json=payload,
            headers=self._authenticated_headers(),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    async def _post_public(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._http.post(
            self._url(endpoint),
            json=payload,
            headers=self._common_headers(),
            timeout=15.0,
        )
        response.raise_for_status()
        return response.json()

    def _url(self, endpoint: str) -> str:
        return f"{self._base_url}/{endpoint.lstrip('/')}"

    def _authenticated_headers(self) -> dict[str, str]:
        headers = {
            **self._common_headers(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _common_headers() -> dict[str, str]:
        return {
            "iLink-App-Id": _ILINK_APP_ID,
            "iLink-App-ClientVersion": str(_client_version()),
        }

    @staticmethod
    def _base_info() -> dict[str, str]:
        return {
            "channel_version": __version__,
            "bot_agent": f"Coworker/{__version__}",
        }


def credentials_from_login(response: dict[str, Any]) -> WeixinCredentials | None:
    if response.get("status") != "confirmed":
        return None
    bot_id = str(response.get("ilink_bot_id") or "").strip()
    token = str(response.get("bot_token") or "").strip()
    if not bot_id or not token:
        return None
    return WeixinCredentials(
        bot_id=bot_id,
        token=token,
        base_url=str(response.get("baseurl") or DEFAULT_BASE_URL).rstrip("/"),
        user_id=str(response.get("ilink_user_id") or ""),
    )


def _random_wechat_uin() -> str:
    number = secrets.randbits(32)
    return base64.b64encode(str(number).encode()).decode()


def _client_version() -> int:
    parts = __version__.split(".", maxsplit=3)
    numbers: list[int] = []
    for part in parts[:3]:
        digits = "".join(character for character in part if character.isdigit())
        numbers.append(int(digits or 0) & 0xFF)
    major, minor, patch = (*numbers, 0, 0, 0)[:3]
    return (major << 16) | (minor << 8) | patch
