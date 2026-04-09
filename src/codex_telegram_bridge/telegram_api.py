from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
LONG_POLL_TIMEOUT_SLACK_SECONDS = 5.0


class TelegramApiError(RuntimeError):
    pass


@dataclass(slots=True)
class SentMessage:
    chat_id: int
    message_id: int
    raw: dict[str, Any]


class TelegramBotApi:
    def __init__(self, bot_token: str, *, base_url: str = "https://api.telegram.org") -> None:
        self._base_url = base_url.rstrip("/")
        self._bot_token = bot_token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_REQUEST_TIMEOUT_SECONDS, connect=DEFAULT_CONNECT_TIMEOUT_SECONDS)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        assert isinstance(result, dict)
        return result

    async def get_updates(
        self,
        *,
        offset: int,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        request_timeout = httpx.Timeout(
            float(timeout) + LONG_POLL_TIMEOUT_SLACK_SECONDS,
            connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": allowed_updates or ["message", "callback_query"],
            },
            timeout=request_timeout,
        )
        assert isinstance(result, list)
        return result

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ) -> SentMessage:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if entities:
            payload["entities"] = entities
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        result = await self._call("sendMessage", payload)
        return SentMessage(
            chat_id=int(result["chat"]["id"]),
            message_id=int(result["message_id"]),
            raw=result,
        )

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        return bool(await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return bool(await self._call("answerCallbackQuery", payload))

    async def set_message_reaction(self, *, chat_id: int, message_id: int, emoji: str | None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if emoji:
            payload["reaction"] = [{"type": "emoji", "emoji": emoji}]
        else:
            payload["reaction"] = []
        return bool(await self._call("setMessageReaction", payload))

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: httpx.Timeout | float | None = None,
    ) -> Any:
        url = f"{self._base_url}/bot{self._bot_token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise TelegramApiError(f"Telegram API request failed for {method}: {detail}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict) and not data.get("ok", False):
            description = data.get("description") or f"Telegram API call failed: {method}"
            error_code = data.get("error_code")
            if error_code is not None:
                raise TelegramApiError(f"{description} (error_code={error_code}, http_status={response.status_code})")
            raise TelegramApiError(f"{description} (http_status={response.status_code})")

        if response.is_error:
            body = response.text.strip()
            detail = f"HTTP {response.status_code}"
            if body:
                detail = f"{detail}: {body}"
            raise TelegramApiError(f"Telegram API request failed for {method}: {detail}")

        if not isinstance(data, dict):
            raise TelegramApiError(f"Telegram API returned a non-JSON response for {method}")

        return data.get("result")


__all__ = ["SentMessage", "TelegramApiError", "TelegramBotApi"]
