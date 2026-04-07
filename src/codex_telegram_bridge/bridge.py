from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from .codex_rpc import CodexAppServerClient, JsonRpcError
from .config import AppConfig
from .formatting import (
    chunk_text,
    extract_latest_agent_message_from_turn,
    extract_latest_terminal_from_thread,
    format_approval_prompt,
    format_external_message,
    format_turn_failure,
)
from .state import ApprovalCleanupMessage, BridgeState, QueuedInput, ThreadState
from .telegram_api import SentMessage, TelegramApiError, TelegramBotApi

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingApproval:
    request_id: int | str
    kind: str
    params: dict[str, Any]
    thread_id: str
    turn_id: str | None
    item_id: str | None
    chat_id: int
    message_id: int


class BridgeApp:
    def __init__(self, config: AppConfig, state: BridgeState) -> None:
        self.config = config
        self.state = state
        self.telegram = TelegramBotApi(
            bot_token=config.telegram.bot_token,
            base_url=config.telegram.api_base_url,
        )
        self.codex = CodexAppServerClient(
            config.codex.command,
            client_name=config.codex.client_name,
            client_title=config.codex.client_title,
            client_version=config.codex.client_version,
            experimental_api=config.codex.experimental_api,
            opt_out_notification_methods=config.codex.opt_out_notification_methods,
            notification_handler=self._handle_codex_notification,
            request_handler=self._handle_codex_request,
        )
        self._shutdown = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started_items: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_agent_item_by_turn: dict[str, dict[str, tuple[str | None, str]]] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._loaded_threads: set[str] = set()
        self._turn_to_thread: dict[str, str] = {}
        self._external_source_kinds = list(config.bridge.external_source_kinds)
        self._state_lock = asyncio.Lock()

    async def run(self) -> None:
        initialize_result = await self.codex.start()
        logger.info(
            "Connected to Codex app-server (codexHome=%s, platform=%s/%s)",
            initialize_result.codex_home,
            initialize_result.platform_family,
            initialize_result.platform_os,
        )

        await self._cleanup_stale_approval_messages()

        self._tasks.add(asyncio.create_task(self._telegram_updates_loop(), name="telegram-updates"))
        if self.config.bridge.poll_external_threads:
            self._tasks.add(asyncio.create_task(self._external_thread_poller_loop(), name="external-thread-poller"))

        try:
            await self._shutdown.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self.codex.close()
        await self.telegram.close()
        await self._save_state()

    async def stop(self) -> None:
        self._shutdown.set()

    async def _cleanup_stale_approval_messages(self) -> None:
        stale = list(self.state.approval_cleanup_messages)
        if not stale:
            return
        for item in stale:
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=item.chat_id, message_id=item.message_id)
        self.state.approval_cleanup_messages = []
        await self._save_state()

    async def _telegram_updates_loop(self) -> None:
        offset = self.state.telegram_update_offset or 0
        while not self._shutdown.is_set():
            try:
                updates = await self.telegram.get_updates(offset=offset, timeout=30)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling failed")
                await asyncio.sleep(2)
                continue

            for update in updates:
                update_id = int(update["update_id"])
                offset = max(offset, update_id + 1)
                self.state.telegram_update_offset = offset
                await self._save_state()
                try:
                    await self._process_telegram_update(update)
                except Exception:
                    logger.exception("Failed handling Telegram update: %s", update)

    async def _process_telegram_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            await self._handle_telegram_message(update["message"])
            return
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
            return

    async def _handle_telegram_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id"))
        chat_type = chat.get("type")
        if chat_type != "private":
            logger.info("Ignoring non-private chat %s", chat_id)
            return

        from_user = message.get("from") or {}
        if from_user.get("is_bot"):
            return

        if not await self._authorize_chat(chat_id):
            logger.warning("Ignoring unauthorized chat %s", chat_id)
            return

        text = message.get("text") or message.get("caption")
        if not text:
            await self._safe_send_message(
                chat_id=chat_id,
                text="Only text messages are supported right now.",
                reply_to_message_id=int(message["message_id"]),
            )
            return

        message_id = int(message["message_id"])
        reply_to = message.get("reply_to_message") or {}
        reply_to_message_id = reply_to.get("message_id")
        thread_id: str | None = None
        if reply_to_message_id is not None:
            thread_id = self.state.lookup_thread_for_message(chat_id, int(reply_to_message_id))
        is_new_thread = thread_id is None

        if is_new_thread:
            thread_id = await self._start_new_thread(chat_id=chat_id, user_message_id=message_id)
        assert thread_id is not None

        thread_state = self.state.get_or_create_thread(thread_id)
        self.state.bind_message(chat_id, message_id, thread_id)
        thread_state.primary_chat_id = chat_id
        thread_state.pending_message_ids.append(message_id)
        await self._save_state()
        await self._safe_set_reaction(chat_id=chat_id, message_id=message_id, emoji=self.config.telegram.processing_reaction)

        if is_new_thread:
            await self._start_turn(thread_state, text=text, source_chat_id=chat_id)
            return

        await self._ensure_thread_loaded(thread_id)
        if thread_state.current_turn_id:
            await self._steer_or_queue(thread_state, text=text, chat_id=chat_id, message_id=message_id)
            return
        await self._start_turn(thread_state, text=text, source_chat_id=chat_id)

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query["id"])
        data = callback_query.get("data") or ""
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id")) if chat else None
        from_user = callback_query.get("from") or {}
        if chat_id is None:
            await self.telegram.answer_callback_query(callback_id, text="No chat context")
            return
        if not await self._authorize_chat(chat_id):
            await self.telegram.answer_callback_query(callback_id, text="Unauthorized")
            return

        try:
            action, request_key = data.split(":", 1)
        except ValueError:
            await self.telegram.answer_callback_query(callback_id, text="Bad action")
            return

        pending = self._pending_approvals.get(request_key)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This approval is no longer active.")
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=chat_id, message_id=int(message["message_id"]))
            return

        await self.telegram.answer_callback_query(callback_id)

        try:
            if pending.kind == "permissions":
                if action == "approve":
                    result = {"permissions": pending.params.get("permissions") or {}}
                else:
                    result = {"permissions": {}}
                await self.codex.respond_result(pending.request_id, result)
            else:
                decision = "accept" if action == "approve" else "decline"
                await self.codex.respond_result(pending.request_id, {"decision": decision})
        except Exception:
            logger.exception("Failed responding to approval %s", pending.request_id)
            await self.telegram.answer_callback_query(callback_id, text="Failed to answer approval")
            return

        if self.config.telegram.delete_approval_messages:
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=pending.chat_id, message_id=pending.message_id)
        self._pending_approvals.pop(request_key, None)
        self.state.approval_cleanup_messages = [
            item
            for item in self.state.approval_cleanup_messages
            if not (item.chat_id == pending.chat_id and item.message_id == pending.message_id)
        ]
        await self._save_state()

    async def _handle_codex_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = str(thread.get("id"))
            if thread_id:
                self._loaded_threads.add(thread_id)
                self.state.get_or_create_thread(thread_id).preview = thread.get("preview") or None
                await self._save_state()
            return

        if method == "thread/status/changed":
            thread_id = str(params.get("threadId"))
            status = (params.get("status") or {}).get("type")
            if thread_id and status == "notLoaded":
                self._loaded_threads.discard(thread_id)
            return

        if method == "thread/closed":
            thread_id = str(params.get("threadId"))
            if thread_id:
                self._loaded_threads.discard(thread_id)
            return

        if method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id")) if turn.get("id") is not None else None
            thread_id = self._turn_to_thread.get(turn_id or "")
            if thread_id and turn_id:
                thread_state = self.state.get_or_create_thread(thread_id)
                thread_state.current_turn_id = turn_id
                await self._save_state()
            return

        if method == "item/started":
            thread_id = str(params.get("threadId") or "")
            item = params.get("item") or {}
            item_id = str(item.get("id")) if item.get("id") is not None else None
            if thread_id and item_id:
                self._started_items.setdefault(thread_id, {})[item_id] = item
            return

        if method == "item/completed":
            thread_id = str(params.get("threadId") or "")
            turn_id = str(params.get("turnId") or "")
            item = params.get("item") or {}
            item_id = str(item.get("id")) if item.get("id") is not None else None
            if not thread_id or not turn_id or not item_id:
                return
            self._turn_to_thread[turn_id] = thread_id
            if item.get("type") == "agentMessage" and item.get("text"):
                self._last_agent_item_by_turn.setdefault(thread_id, {})[turn_id] = (item_id, str(item.get("text")))
            self._started_items.setdefault(thread_id, {})[item_id] = item
            return

        if method == "serverRequest/resolved":
            request_id = str(params.get("requestId"))
            pending = self._pending_approvals.pop(request_id, None)
            if pending and self.config.telegram.delete_approval_messages:
                with contextlib.suppress(Exception):
                    await self.telegram.delete_message(chat_id=pending.chat_id, message_id=pending.message_id)
            if pending:
                self.state.approval_cleanup_messages = [
                    item
                    for item in self.state.approval_cleanup_messages
                    if not (item.chat_id == pending.chat_id and item.message_id == pending.message_id)
                ]
                await self._save_state()
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            await self._handle_turn_completed(turn)
            return

        if method == "error":
            logger.warning("Codex emitted error event: %s", params)
            return

    async def _handle_codex_request(self, method: str, request_id: int | str, params: dict[str, Any]) -> None:
        if method == "item/commandExecution/requestApproval":
            await self._create_approval_prompt("command", request_id, params)
            return
        if method == "item/fileChange/requestApproval":
            await self._create_approval_prompt("file", request_id, params)
            return
        if method == "item/permissions/requestApproval":
            await self._create_approval_prompt("permissions", request_id, params)
            return
        if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
            thread_id = str(params.get("threadId") or "")
            await self._notify_unsupported_request(thread_id, method)
            logger.warning("Leaving unsupported request unanswered: %s", method)
            return

        logger.warning("Unhandled server request %s; leaving unanswered", method)

    async def _create_approval_prompt(self, kind: str, request_id: int | str, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or "")
        item_id = str(params.get("itemId")) if params.get("itemId") is not None else None
        turn_id = str(params.get("turnId")) if params.get("turnId") is not None else None
        thread_state = self.state.get_or_create_thread(thread_id)
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            logger.warning("Approval arrived for thread %s with no Telegram chat; denying", thread_id)
            if kind == "permissions":
                await self.codex.respond_result(request_id, {"permissions": {}})
            else:
                await self.codex.respond_result(request_id, {"decision": "decline"})
            return

        started_item = None
        if item_id is not None:
            started_item = (self._started_items.get(thread_id) or {}).get(item_id)

        prompt = format_approval_prompt(kind, params, started_item)
        reply_to = self._reply_target_for_thread(thread_state)
        sent = await self._safe_send_message(
            chat_id=chat_id,
            text=prompt,
            reply_to_message_id=reply_to,
            inline_keyboard=[
                [
                    {"text": "Approve", "callback_data": f"approve:{request_id}"},
                    {"text": "Deny", "callback_data": f"deny:{request_id}"},
                ]
            ],
        )
        if sent is None:
            logger.warning("Failed to send approval prompt; denying %s", request_id)
            if kind == "permissions":
                await self.codex.respond_result(request_id, {"permissions": {}})
            else:
                await self.codex.respond_result(request_id, {"decision": "decline"})
            return

        pending = PendingApproval(
            request_id=request_id,
            kind=kind,
            params=params,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
        )
        self._pending_approvals[str(request_id)] = pending
        self.state.approval_cleanup_messages.append(
            ApprovalCleanupMessage(chat_id=sent.chat_id, message_id=sent.message_id)
        )
        self.state.bind_message(sent.chat_id, sent.message_id, thread_id)
        await self._save_state()

    async def _notify_unsupported_request(self, thread_id: str, method: str) -> None:
        thread_state = self.state.get_or_create_thread(thread_id)
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            return
        await self._safe_send_message(
            chat_id=chat_id,
            reply_to_message_id=self._reply_target_for_thread(thread_state),
            text=f"Codex requested {method}, which this bridge does not answer yet. Please handle it locally.",
        )

    async def _start_new_thread(self, *, chat_id: int, user_message_id: int) -> str:
        params = dict(self.config.codex.thread_start)
        result = await self.codex.request("thread/start", params)
        thread = result.get("thread") or {}
        thread_id = str(thread["id"])
        self._loaded_threads.add(thread_id)
        thread_state = self.state.get_or_create_thread(thread_id)
        thread_state.primary_chat_id = chat_id
        thread_state.last_chain_message_id = user_message_id
        thread_state.preview = thread.get("preview") or None
        await self._save_state()
        return thread_id

    async def _ensure_thread_loaded(self, thread_id: str) -> None:
        if thread_id in self._loaded_threads:
            return
        try:
            await self.codex.request("thread/resume", {"threadId": thread_id})
            self._loaded_threads.add(thread_id)
        except JsonRpcError:
            logger.exception("Failed to resume thread %s", thread_id)
            raise

    async def _start_turn(self, thread_state: ThreadState, *, text: str, source_chat_id: int) -> None:
        await self._ensure_thread_loaded(thread_state.thread_id)
        params = {
            "threadId": thread_state.thread_id,
            "input": [{"type": "text", "text": text}],
            **self.config.codex.turn_start_defaults,
        }
        try:
            result = await self.codex.request("turn/start", params)
        except JsonRpcError as exc:
            logger.warning("turn/start failed for %s: %s", thread_state.thread_id, exc)
            thread_state.queued_inputs.append(
                QueuedInput(chat_id=source_chat_id, message_id=thread_state.pending_message_ids[-1], text=text)
            )
            await self._save_state()
            await self._safe_send_message(
                chat_id=source_chat_id,
                reply_to_message_id=thread_state.pending_message_ids[-1],
                text="Codex is busy right now. I queued your message and will try again after the current turn finishes.",
            )
            return

        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if turn_id is not None:
            turn_id = str(turn_id)
            thread_state.current_turn_id = turn_id
            self._turn_to_thread[turn_id] = thread_state.thread_id
        await self._save_state()

    async def _steer_or_queue(self, thread_state: ThreadState, *, text: str, chat_id: int, message_id: int) -> None:
        turn_id = thread_state.current_turn_id
        if not turn_id:
            thread_state.queued_inputs.append(QueuedInput(chat_id=chat_id, message_id=message_id, text=text))
            await self._save_state()
            return
        try:
            result = await self.codex.request(
                "turn/steer",
                {
                    "threadId": thread_state.thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text}],
                },
            )
            accepted_turn_id = result.get("turnId")
            if accepted_turn_id is not None:
                accepted_turn_id = str(accepted_turn_id)
                thread_state.current_turn_id = accepted_turn_id
                self._turn_to_thread[accepted_turn_id] = thread_state.thread_id
            await self._save_state()
        except JsonRpcError as exc:
            logger.warning("turn/steer failed for %s: %s", thread_state.thread_id, exc)
            thread_state.queued_inputs.append(QueuedInput(chat_id=chat_id, message_id=message_id, text=text))
            await self._save_state()

    async def _handle_turn_completed(self, turn: dict[str, Any]) -> None:
        turn_id = str(turn.get("id")) if turn.get("id") is not None else None
        thread_id = self._turn_to_thread.get(turn_id or "")
        if not thread_id:
            logger.warning("turn/completed without known threadId: %s", turn)
            return

        thread_state = self.state.get_or_create_thread(thread_id)
        if turn_id and thread_state.current_turn_id == turn_id:
            thread_state.current_turn_id = None

        item_id, text = self._last_agent_item_by_turn.get(thread_id, {}).get(turn_id or "", (None, None))
        if text and item_id and item_id != thread_state.last_delivered_item_id:
            reply_to = self._reply_target_for_completion(thread_state)
            sent = await self._send_thread_text_reply(thread_state, text=text, reply_to_message_id=reply_to)
            if sent is not None:
                thread_state.last_delivered_item_id = item_id
                thread_state.last_delivered_turn_id = turn_id
        elif (turn.get("status") or "") in {"failed", "interrupted"}:
            reply_to = self._reply_target_for_completion(thread_state)
            sent = await self._send_thread_text_reply(
                thread_state,
                text=format_turn_failure(turn),
                reply_to_message_id=reply_to,
            )
            if sent is not None and turn_id:
                thread_state.last_delivered_turn_id = turn_id
                thread_state.last_delivered_item_id = f"status:{turn_id}"

        await self._mark_pending_messages_done(thread_state)
        await self._start_next_queued_input(thread_state)
        await self._save_state()

    async def _start_next_queued_input(self, thread_state: ThreadState) -> None:
        if thread_state.current_turn_id or not thread_state.queued_inputs:
            return
        next_input = thread_state.queued_inputs.pop(0)
        if next_input.message_id not in thread_state.pending_message_ids:
            thread_state.pending_message_ids.append(next_input.message_id)
        await self._start_turn(thread_state, text=next_input.text, source_chat_id=next_input.chat_id)

    async def _mark_pending_messages_done(self, thread_state: ThreadState) -> None:
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            thread_state.pending_message_ids = []
            return
        for message_id in list(thread_state.pending_message_ids):
            await self._safe_set_reaction(
                chat_id=chat_id,
                message_id=message_id,
                emoji=self.config.telegram.done_reaction,
            )
        thread_state.pending_message_ids = []

    async def _external_thread_poller_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._poll_external_threads_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("External thread poller failed")
            await asyncio.sleep(self.config.bridge.external_poll_interval_seconds)

    async def _poll_external_threads_once(self) -> None:
        params: dict[str, Any] = {
            "limit": self.config.bridge.external_poll_limit,
            "sortKey": "updated_at",
        }
        if self._external_source_kinds:
            params["sourceKinds"] = self._external_source_kinds
        try:
            result = await self.codex.request("thread/list", params)
        except JsonRpcError as exc:
            if params.get("sourceKinds"):
                logger.warning(
                    "thread/list rejected sourceKinds=%s (%s); falling back to interactive-only",
                    self._external_source_kinds,
                    exc,
                )
                self._external_source_kinds = []
                result = await self.codex.request("thread/list", {"limit": self.config.bridge.external_poll_limit, "sortKey": "updated_at"})
            else:
                raise

        for thread in result.get("data") or []:
            await self._maybe_deliver_external_thread(thread)

    async def _maybe_deliver_external_thread(self, thread_summary: dict[str, Any]) -> None:
        thread_id = str(thread_summary.get("id") or "")
        if not thread_id:
            return
        thread_state = self.state.get_or_create_thread(thread_id)
        updated_at = thread_summary.get("updatedAt")
        if updated_at is not None:
            updated_at = int(updated_at)
        thread_state.preview = thread_summary.get("preview") or thread_state.preview

        should_read = False
        if updated_at is not None and (thread_state.last_seen_updated_at is None or updated_at > thread_state.last_seen_updated_at):
            should_read = True
        if thread_state.pending_message_ids:
            should_read = True
        if not should_read:
            return

        result = await self.codex.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = result.get("thread") or {}
        terminal = extract_latest_terminal_from_thread(thread)
        if updated_at is not None:
            thread_state.last_seen_updated_at = updated_at
        if terminal is None:
            await self._save_state()
            return

        terminal_item_id = terminal.get("item_id")
        terminal_turn_id = terminal.get("turn_id")
        terminal_text = terminal.get("text")

        if terminal_item_id and terminal_item_id == thread_state.last_delivered_item_id:
            await self._save_state()
            return
        if not terminal_item_id and terminal_turn_id and f"status:{terminal_turn_id}" == thread_state.last_delivered_item_id:
            await self._save_state()
            return

        reply_to = self._reply_target_for_completion(thread_state)
        if terminal_text:
            text = terminal_text
            if thread_state.last_chain_message_id is None:
                text = format_external_message(
                    self.config.bridge.external_header_template,
                    preview=thread_state.preview,
                    text=terminal_text,
                )
        else:
            text = f"Codex finished with status {terminal.get('status')}"
            if terminal.get("error_message"):
                text += f": {terminal['error_message']}"

        sent = await self._send_thread_text_reply(thread_state, text=text, reply_to_message_id=reply_to)
        if sent is not None:
            thread_state.last_delivered_turn_id = terminal_turn_id
            thread_state.last_delivered_item_id = terminal_item_id or f"status:{terminal_turn_id}"
        await self._mark_pending_messages_done(thread_state)
        await self._start_next_queued_input(thread_state)
        await self._save_state()

    async def _send_thread_text_reply(
        self,
        thread_state: ThreadState,
        *,
        text: str,
        reply_to_message_id: int | None,
    ) -> SentMessage | None:
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            logger.warning("No Telegram chat bound for thread %s", thread_state.thread_id)
            return None

        chunks = chunk_text(text, self.config.bridge.max_message_chars)
        first_sent: SentMessage | None = None
        current_reply_to = reply_to_message_id
        for chunk in chunks:
            sent = await self._safe_send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=current_reply_to,
            )
            if sent is None:
                return first_sent
            if first_sent is None:
                first_sent = sent
            self.state.bind_message(chat_id, sent.message_id, thread_state.thread_id)
            current_reply_to = sent.message_id
            thread_state.last_chain_message_id = sent.message_id
        return first_sent

    async def _safe_send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
    ) -> SentMessage | None:
        try:
            return await self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                inline_keyboard=inline_keyboard,
            )
        except TelegramApiError:
            logger.exception("Telegram sendMessage failed")
            return None

    async def _safe_set_reaction(self, *, chat_id: int, message_id: int, emoji: str | None) -> None:
        try:
            await self.telegram.set_message_reaction(chat_id=chat_id, message_id=message_id, emoji=emoji)
        except Exception:
            logger.debug("Unable to set reaction %r on %s/%s", emoji, chat_id, message_id, exc_info=True)

    async def _authorize_chat(self, chat_id: int) -> bool:
        if self.config.telegram.allowed_chat_ids:
            return chat_id in self.config.telegram.allowed_chat_ids

        configured_primary = self.config.telegram.primary_chat_id or self.state.primary_chat_id
        if configured_primary is not None:
            return chat_id == configured_primary

        if not self.config.bridge.allow_first_private_chat:
            return False

        self.state.primary_chat_id = chat_id
        await self._save_state()
        logger.info("Primary Telegram chat bound to %s", chat_id)
        return True

    def _chat_for_thread(self, thread_state: ThreadState) -> int | None:
        return thread_state.primary_chat_id or self.config.telegram.primary_chat_id or self.state.primary_chat_id

    def _reply_target_for_thread(self, thread_state: ThreadState) -> int | None:
        if thread_state.pending_message_ids:
            return thread_state.pending_message_ids[-1]
        return thread_state.last_chain_message_id

    def _reply_target_for_completion(self, thread_state: ThreadState) -> int | None:
        if thread_state.pending_message_ids:
            return thread_state.pending_message_ids[-1]
        return thread_state.last_chain_message_id

    def _extract_thread_id_from_turn(self, turn: dict[str, Any]) -> str | None:
        if turn.get("threadId") is not None:
            return str(turn.get("threadId"))
        items = turn.get("items") or []
        for item in items:
            if item.get("threadId") is not None:
                return str(item.get("threadId"))
        return None

    async def _save_state(self) -> None:
        async with self._state_lock:
            self.state.save(self.config.bridge.state_path)


__all__ = ["BridgeApp"]
