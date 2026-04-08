from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .desktop_client import (
    TERMINAL_TURN_STATUSES,
    CodexDesktopClient,
    DesktopClientError,
    DesktopConversation,
    DesktopConversationSummary,
    DesktopProject,
    DesktopRequest,
)
from .formatting import extract_latest_agent_message_from_turn, format_approval_prompt, format_turn_failure, render_markdown_chunks
from .state import ApprovalCleanupMessage, BridgeState, QueuedInput, ThreadState
from .telegram_api import SentMessage, TelegramApiError, TelegramBotApi

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingApproval:
    callback_key: str
    request_id: str
    kind: str
    thread_id: str
    chat_id: int
    message_id: int


@dataclass(slots=True)
class PendingProjectSelection:
    callback_key: str
    chat_id: int
    source_message_id: int
    picker_message_id: int
    text: str
    projects: list[DesktopProject]


@dataclass(slots=True)
class PendingAttachSelection:
    callback_key: str
    chat_id: int
    source_message_id: int
    picker_message_id: int
    threads: list[DesktopConversationSummary]


class BridgeApp:
    def __init__(self, config: AppConfig, state: BridgeState) -> None:
        self.config = config
        self.state = state
        self.telegram = TelegramBotApi(
            bot_token=config.telegram.bot_token,
            base_url=config.telegram.api_base_url,
        )
        self.desktop = CodexDesktopClient(
            app_path=config.desktop.app_path,
            remote_debugging_port=config.desktop.remote_debugging_port,
            user_data_dir=config.desktop.user_data_dir,
            launch_timeout_seconds=config.desktop.launch_timeout_seconds,
            poll_interval_seconds=config.desktop.poll_interval_seconds,
        )
        self._shutdown = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_project_selections: dict[str, PendingProjectSelection] = {}
        self._pending_attach_selections: dict[str, PendingAttachSelection] = {}
        self._missing_thread_counts: dict[str, int] = {}
        self._next_approval_key = 0
        self._state_lock = asyncio.Lock()

    async def run(self) -> None:
        session = await self.desktop.start()
        logger.info(
            "Connected to Codex Desktop (title=%s, page=%s, debugger=%s)",
            session.page_title,
            session.page_url,
            session.debugger_url,
        )
        await self.desktop.wait_until_task_index_ready()
        bot = await self.telegram.get_me()
        logger.info(
            "Connected to Telegram bot @%s (id=%s)",
            bot.get("username") or "unknown",
            bot.get("id"),
        )

        await self._cleanup_stale_approval_messages()
        await self._sync_all_threads_once()

        self._tasks.add(asyncio.create_task(self._telegram_updates_loop(), name="telegram-updates"))
        self._tasks.add(asyncio.create_task(self._thread_sync_loop(), name="desktop-thread-sync"))

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
        await self.desktop.close()
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
            except TelegramApiError as exc:
                logger.warning("Telegram polling failed: %s", exc)
                await asyncio.sleep(2)
                continue
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

    async def _thread_sync_loop(self) -> None:
        interval = self.config.desktop.poll_interval_seconds
        while not self._shutdown.is_set():
            try:
                await self._sync_all_threads_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Desktop thread sync failed")
            await asyncio.sleep(interval)

    async def _sync_all_threads_once(self) -> None:
        for thread_id in list(self.state.threads):
            try:
                await self._sync_thread(thread_id)
            except DesktopClientError as exc:
                logger.warning("Skipping desktop sync for %s: %s", thread_id, exc)

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

        control = self._parse_control_command(text)
        if control is not None:
            await self._handle_control_command(
                command=control[0],
                argument=control[1],
                chat_id=chat_id,
                message_id=int(message["message_id"]),
            )
            return

        await self._handle_user_text_message(chat_id=chat_id, message=message, text=text)

    async def _handle_user_text_message(self, *, chat_id: int, message: dict[str, Any], text: str) -> None:
        message_id = int(message["message_id"])
        reply_to = message.get("reply_to_message") or {}
        reply_to_message_id = reply_to.get("message_id")
        if reply_to_message_id is not None:
            thread_id = self.state.lookup_thread_for_message(chat_id, int(reply_to_message_id))
            if thread_id is None:
                await self._safe_send_message(
                    chat_id=chat_id,
                    text=(
                        "This reply target is not bound to a known bridge thread. "
                        "Send a new top-level message to start a thread, or attach one explicitly."
                    ),
                    reply_to_message_id=message_id,
                )
                return
        else:
            await self._prompt_for_new_thread_project(
                chat_id=chat_id,
                user_message_id=message_id,
                text=text,
            )
            return

        assert thread_id is not None
        thread_state = self.state.get_or_create_thread(thread_id)
        thread_state.primary_chat_id = chat_id
        await self._safe_set_reaction(chat_id=chat_id, message_id=message_id, emoji=self.config.telegram.processing_reaction)

        if thread_state.current_turn_id:
            self.state.bind_message(chat_id, message_id, thread_state.thread_id)
            thread_state.pending_message_ids.append(message_id)
            thread_state.queued_inputs.append(QueuedInput(chat_id=chat_id, message_id=message_id, text=text))
            await self._save_state()
            return

        await self._send_thread_input(
            thread_state,
            text=text,
            source_chat_id=chat_id,
            source_message_id=message_id,
            bind_on_success=True,
        )

    async def _handle_control_command(
        self,
        *,
        command: str,
        argument: str | None,
        chat_id: int,
        message_id: int,
    ) -> None:
        if command == "attach":
            await self._handle_attach_command(chat_id=chat_id, message_id=message_id, thread_id=argument)
            return

    async def _handle_attach_command(self, *, chat_id: int, message_id: int, thread_id: str | None) -> None:
        normalized_thread_id = (thread_id or "").strip()
        if not normalized_thread_id:
            await self._prompt_for_attach_thread(chat_id=chat_id, message_id=message_id)
            return

        try:
            conversation = await self.desktop.activate_thread(normalized_thread_id)
        except DesktopClientError:
            await self._safe_send_message(
                chat_id=chat_id,
                text=f"Failed to attach thread {normalized_thread_id}. Check that it is visible in Codex Desktop.",
                reply_to_message_id=message_id,
            )
            return

        thread_state = self.state.get_or_create_thread(normalized_thread_id)
        thread_state.primary_chat_id = chat_id
        thread_state.preview = conversation.preview or thread_state.preview
        latest_turn = conversation.latest_turn
        thread_state.current_turn_id = latest_turn.turn_id if latest_turn and not latest_turn.is_terminal else None
        self.state.bind_message(chat_id, message_id, normalized_thread_id)
        await self._save_state()

        confirmation = await self._safe_send_message(
            chat_id=chat_id,
            text=f"Attached thread {normalized_thread_id}. Reply in this chain to continue it from Telegram.",
            reply_to_message_id=message_id,
        )
        if confirmation is None:
            return

        self.state.bind_message(confirmation.chat_id, confirmation.message_id, normalized_thread_id)
        thread_state.last_chain_message_id = confirmation.message_id
        await self._save_state()

        latest = self._extract_latest_agent_message_from_conversation(conversation)
        if latest is None:
            return
        item_id, turn_id, latest_text = latest
        sent = await self._send_thread_text_reply(
            thread_state,
            text=latest_text,
            reply_to_message_id=confirmation.message_id,
        )
        if sent is not None:
            thread_state.last_delivered_item_id = item_id
            thread_state.last_delivered_turn_id = turn_id
            await self._save_state()

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query["id"])
        data = callback_query.get("data") or ""
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id")) if chat else None
        if chat_id is None:
            await self.telegram.answer_callback_query(callback_id, text="No chat context")
            return
        if not await self._authorize_chat(chat_id):
            await self.telegram.answer_callback_query(callback_id, text="Unauthorized")
            return

        parts = data.split(":")
        if len(parts) < 2:
            await self.telegram.answer_callback_query(callback_id, text="Bad action")
            return
        action = parts[0]
        callback_key = parts[1]

        if action == "project":
            await self._handle_project_selection_callback(
                callback_id=callback_id,
                callback_key=callback_key,
                selection_index_text=parts[2] if len(parts) > 2 else None,
                callback_message=message,
            )
            return
        if action == "project-cancel":
            await self._handle_project_cancel_callback(
                callback_id=callback_id,
                callback_key=callback_key,
                callback_message=message,
            )
            return
        if action == "attach":
            await self._handle_attach_selection_callback(
                callback_id=callback_id,
                callback_key=callback_key,
                selection_index_text=parts[2] if len(parts) > 2 else None,
                callback_message=message,
            )
            return
        if action == "attach-cancel":
            await self._handle_attach_cancel_callback(
                callback_id=callback_id,
                callback_key=callback_key,
                callback_message=message,
            )
            return

        pending = self._pending_approvals.get(callback_key)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This approval is no longer active.")
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=chat_id, message_id=int(message["message_id"]))
            return

        approve = action == "approve"
        try:
            await self.desktop.click_approval_action(pending.thread_id, approve=approve)
        except DesktopClientError:
            logger.exception("Failed responding to approval %s", pending.request_id)
            await self.telegram.answer_callback_query(callback_id, text="Failed to answer approval")
            return

        await self.telegram.answer_callback_query(callback_id)
        await self._clear_pending_approval(callback_key, delete_message=self.config.telegram.delete_approval_messages)

    async def _prompt_for_new_thread_project(
        self,
        *,
        chat_id: int,
        user_message_id: int,
        text: str,
    ) -> None:
        try:
            projects = await self.desktop.list_projects()
        except DesktopClientError as exc:
            await self._safe_send_message(
                chat_id=chat_id,
                text=f"Failed to load Codex Desktop projects: {exc}",
                reply_to_message_id=user_message_id,
            )
            return

        if not projects:
            await self._safe_send_message(
                chat_id=chat_id,
                text="Codex Desktop does not expose any project folders right now.",
                reply_to_message_id=user_message_id,
            )
            return

        callback_key = self._next_callback_key()
        picker = await self._safe_send_message(
            chat_id=chat_id,
            text="Choose a Codex Desktop project for this new thread.",
            reply_to_message_id=user_message_id,
            inline_keyboard=self._build_project_picker_keyboard(callback_key, projects),
        )
        if picker is None:
            return

        self._pending_project_selections[callback_key] = PendingProjectSelection(
            callback_key=callback_key,
            chat_id=chat_id,
            source_message_id=user_message_id,
            picker_message_id=picker.message_id,
            text=text,
            projects=projects,
        )
        self.state.approval_cleanup_messages.append(
            ApprovalCleanupMessage(chat_id=picker.chat_id, message_id=picker.message_id)
        )
        await self._save_state()

    async def _prompt_for_attach_thread(self, *, chat_id: int, message_id: int) -> None:
        try:
            threads = await self.desktop.list_threads()
        except DesktopClientError as exc:
            await self._safe_send_message(
                chat_id=chat_id,
                text=f"Failed to load recent Codex Desktop sessions: {exc}",
                reply_to_message_id=message_id,
            )
            return

        recent_threads = threads[:8]
        if not recent_threads:
            await self._safe_send_message(
                chat_id=chat_id,
                text="Codex Desktop does not expose any recent sessions right now.",
                reply_to_message_id=message_id,
            )
            return

        callback_key = self._next_callback_key()
        picker = await self._safe_send_message(
            chat_id=chat_id,
            text="Choose a recent Codex Desktop session to attach.",
            reply_to_message_id=message_id,
            inline_keyboard=self._build_attach_picker_keyboard(callback_key, recent_threads),
        )
        if picker is None:
            return

        self._pending_attach_selections[callback_key] = PendingAttachSelection(
            callback_key=callback_key,
            chat_id=chat_id,
            source_message_id=message_id,
            picker_message_id=picker.message_id,
            threads=recent_threads,
        )
        self.state.approval_cleanup_messages.append(
            ApprovalCleanupMessage(chat_id=picker.chat_id, message_id=picker.message_id)
        )
        await self._save_state()

    async def _start_new_thread_from_message(
        self,
        *,
        chat_id: int,
        user_message_id: int,
        text: str,
        project: DesktopProject,
    ) -> ThreadState | None:
        try:
            conversation = await self.desktop.start_new_thread(project.path, text)
        except DesktopClientError as exc:
            await self._safe_send_message(
                chat_id=chat_id,
                text=f"Failed to start a new Codex Desktop thread in {project.label}: {exc}",
                reply_to_message_id=user_message_id,
            )
            return None

        thread_state = self.state.get_or_create_thread(conversation.thread_id)
        thread_state.primary_chat_id = chat_id
        thread_state.last_chain_message_id = user_message_id
        thread_state.preview = conversation.preview
        thread_state.pending_message_ids = [user_message_id]
        latest_turn = conversation.latest_turn
        thread_state.current_turn_id = latest_turn.turn_id if latest_turn and not latest_turn.is_terminal else None
        self.state.bind_message(chat_id, user_message_id, conversation.thread_id)
        await self._save_state()
        await self._safe_set_reaction(chat_id=chat_id, message_id=user_message_id, emoji=self.config.telegram.processing_reaction)
        return thread_state

    async def _handle_project_selection_callback(
        self,
        *,
        callback_id: str,
        callback_key: str,
        selection_index_text: str | None,
        callback_message: dict[str, Any],
    ) -> None:
        pending = self._pending_project_selections.pop(callback_key, None)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This project selection is no longer active.")
            await self._delete_callback_message(callback_message)
            return

        if selection_index_text is None:
            await self.telegram.answer_callback_query(callback_id, text="Bad project selection")
            await self._clear_project_selection(pending, delete_message=True)
            return

        try:
            selection_index = int(selection_index_text)
        except ValueError:
            await self.telegram.answer_callback_query(callback_id, text="Bad project selection")
            await self._clear_project_selection(pending, delete_message=True)
            return

        if not (0 <= selection_index < len(pending.projects)):
            await self.telegram.answer_callback_query(callback_id, text="Bad project selection")
            await self._clear_project_selection(pending, delete_message=True)
            return

        await self.telegram.answer_callback_query(callback_id)
        await self._clear_project_selection(pending, delete_message=True)
        await self._start_new_thread_from_message(
            chat_id=pending.chat_id,
            user_message_id=pending.source_message_id,
            text=pending.text,
            project=pending.projects[selection_index],
        )

    async def _handle_project_cancel_callback(
        self,
        *,
        callback_id: str,
        callback_key: str,
        callback_message: dict[str, Any],
    ) -> None:
        pending = self._pending_project_selections.pop(callback_key, None)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This project selection is no longer active.")
            await self._delete_callback_message(callback_message)
            return
        await self.telegram.answer_callback_query(callback_id, text="Cancelled")
        await self._clear_project_selection(pending, delete_message=True)

    async def _handle_attach_selection_callback(
        self,
        *,
        callback_id: str,
        callback_key: str,
        selection_index_text: str | None,
        callback_message: dict[str, Any],
    ) -> None:
        pending = self._pending_attach_selections.pop(callback_key, None)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This attach selection is no longer active.")
            await self._delete_callback_message(callback_message)
            return

        if selection_index_text is None:
            await self.telegram.answer_callback_query(callback_id, text="Bad attach selection")
            await self._clear_attach_selection(pending, delete_message=True)
            return

        try:
            selection_index = int(selection_index_text)
        except ValueError:
            await self.telegram.answer_callback_query(callback_id, text="Bad attach selection")
            await self._clear_attach_selection(pending, delete_message=True)
            return

        if not (0 <= selection_index < len(pending.threads)):
            await self.telegram.answer_callback_query(callback_id, text="Bad attach selection")
            await self._clear_attach_selection(pending, delete_message=True)
            return

        await self.telegram.answer_callback_query(callback_id)
        await self._clear_attach_selection(pending, delete_message=True)
        await self._handle_attach_command(
            chat_id=pending.chat_id,
            message_id=pending.source_message_id,
            thread_id=pending.threads[selection_index].thread_id,
        )

    async def _handle_attach_cancel_callback(
        self,
        *,
        callback_id: str,
        callback_key: str,
        callback_message: dict[str, Any],
    ) -> None:
        pending = self._pending_attach_selections.pop(callback_key, None)
        if pending is None:
            await self.telegram.answer_callback_query(callback_id, text="This attach selection is no longer active.")
            await self._delete_callback_message(callback_message)
            return
        await self.telegram.answer_callback_query(callback_id, text="Cancelled")
        await self._clear_attach_selection(pending, delete_message=True)

    async def _send_thread_input(
        self,
        thread_state: ThreadState,
        *,
        text: str,
        source_chat_id: int,
        source_message_id: int,
        bind_on_success: bool = False,
    ) -> None:
        try:
            conversation = await self.desktop.send_message(thread_state.thread_id, text)
        except DesktopClientError as exc:
            logger.warning("Desktop send failed for %s: %s", thread_state.thread_id, exc)
            self._remove_pending_message(thread_state, source_message_id)
            await self._safe_set_reaction(chat_id=source_chat_id, message_id=source_message_id, emoji=None)
            await self._safe_send_message(
                chat_id=source_chat_id,
                text=f"Failed to send to Codex Desktop: {exc}",
                reply_to_message_id=source_message_id,
            )
            await self._save_state()
            return

        if bind_on_success:
            self.state.bind_message(source_chat_id, source_message_id, thread_state.thread_id)
            if source_message_id not in thread_state.pending_message_ids:
                thread_state.pending_message_ids.append(source_message_id)

        thread_state.preview = conversation.preview or thread_state.preview
        latest_turn = conversation.latest_turn
        if latest_turn is not None and not latest_turn.is_terminal:
            thread_state.current_turn_id = latest_turn.turn_id
        else:
            thread_state.current_turn_id = None
        await self._save_state()

    async def _sync_thread(self, thread_id: str) -> None:
        conversation = await self.desktop.read_thread(thread_id)
        if conversation is None:
            misses = self._missing_thread_counts.get(thread_id, 0) + 1
            self._missing_thread_counts[thread_id] = misses
            if misses >= 3:
                logger.warning("Desktop thread %s is not currently exposed by the Desktop task index.", thread_id)
            return
        self._missing_thread_counts.pop(thread_id, None)

        thread_state = self.state.get_or_create_thread(thread_id)
        changed = False
        preview = conversation.preview
        if preview != thread_state.preview:
            thread_state.preview = preview
            changed = True

        if await self._sync_approval_requests(thread_state, conversation):
            changed = True
        if await self._sync_turn_state(thread_state, conversation):
            changed = True

        if changed:
            await self._save_state()

    async def _sync_approval_requests(self, thread_state: ThreadState, conversation: DesktopConversation) -> bool:
        changed = False
        active_request_ids = {request.request_id for request in conversation.requests}
        for request in conversation.requests:
            if self._find_pending_approval(thread_state.thread_id, request.request_id) is not None:
                continue
            if await self._create_approval_prompt(thread_state, request):
                changed = True

        for callback_key, pending in list(self._pending_approvals.items()):
            if pending.thread_id != thread_state.thread_id:
                continue
            if pending.request_id in active_request_ids:
                continue
            await self._clear_pending_approval(callback_key, delete_message=self.config.telegram.delete_approval_messages)
            changed = True

        return changed

    async def _create_approval_prompt(self, thread_state: ThreadState, request: DesktopRequest) -> bool:
        chat_id = self._chat_for_thread(thread_state)
        if chat_id is None:
            logger.warning("Approval arrived for thread %s with no Telegram chat; leaving it pending in Desktop", thread_state.thread_id)
            return False

        callback_key = self._next_callback_key()
        prompt = self._format_desktop_request_prompt(request)
        reply_to = self._reply_target_for_thread(thread_state)
        sent = await self._safe_send_message(
            chat_id=chat_id,
            text=prompt,
            reply_to_message_id=reply_to,
            inline_keyboard=[
                [
                    {"text": "Approve", "callback_data": f"approve:{callback_key}"},
                    {"text": "Deny", "callback_data": f"deny:{callback_key}"},
                ]
            ],
        )
        if sent is None:
            return False

        pending = PendingApproval(
            callback_key=callback_key,
            request_id=request.request_id,
            kind=request.kind,
            thread_id=thread_state.thread_id,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
        )
        self._pending_approvals[callback_key] = pending
        self.state.approval_cleanup_messages.append(
            ApprovalCleanupMessage(chat_id=sent.chat_id, message_id=sent.message_id)
        )
        self.state.bind_message(sent.chat_id, sent.message_id, thread_state.thread_id)
        return True

    async def _clear_pending_approval(self, callback_key: str, *, delete_message: bool) -> None:
        pending = self._pending_approvals.pop(callback_key, None)
        if pending is None:
            return
        if delete_message:
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=pending.chat_id, message_id=pending.message_id)
        self._forget_cleanup_message(chat_id=pending.chat_id, message_id=pending.message_id)
        await self._save_state()

    async def _sync_turn_state(self, thread_state: ThreadState, conversation: DesktopConversation) -> bool:
        latest_turn = conversation.latest_turn
        if latest_turn is None:
            if thread_state.current_turn_id is not None:
                thread_state.current_turn_id = None
                return True
            return False

        changed = False
        expected_current_turn_id = latest_turn.turn_id if not latest_turn.is_terminal else None
        if thread_state.current_turn_id != expected_current_turn_id:
            thread_state.current_turn_id = expected_current_turn_id
            changed = True

        if latest_turn.is_terminal:
            if await self._deliver_terminal_turn(thread_state, latest_turn):
                changed = True
            if thread_state.pending_message_ids:
                await self._mark_pending_messages_done(thread_state)
                changed = True
            if thread_state.queued_inputs:
                await self._start_next_queued_input(thread_state)
                changed = True

        return changed

    async def _deliver_terminal_turn(self, thread_state: ThreadState, turn: Any) -> bool:
        turn_payload = turn.raw if isinstance(turn.raw, dict) else {"items": turn.items, "status": turn.status, "error": turn.error}
        item_id, text = extract_latest_agent_message_from_turn(turn_payload)
        if text and item_id and item_id != thread_state.last_delivered_item_id:
            reply_to = self._reply_target_for_completion(thread_state)
            sent = await self._send_thread_text_reply(thread_state, text=text, reply_to_message_id=reply_to)
            if sent is None:
                return False
            thread_state.last_delivered_item_id = item_id
            thread_state.last_delivered_turn_id = turn.turn_id
            return True

        if (
            turn.turn_id
            and turn.turn_id != thread_state.last_delivered_turn_id
            and (turn.status or "") in TERMINAL_TURN_STATUSES - {"completed"}
        ):
            reply_to = self._reply_target_for_completion(thread_state)
            sent = await self._send_thread_text_reply(
                thread_state,
                text=format_turn_failure(turn_payload),
                reply_to_message_id=reply_to,
            )
            if sent is None:
                return False
            thread_state.last_delivered_turn_id = turn.turn_id
            thread_state.last_delivered_item_id = f"status:{turn.turn_id}"
            return True

        return False

    async def _start_next_queued_input(self, thread_state: ThreadState) -> None:
        if thread_state.current_turn_id or not thread_state.queued_inputs:
            return
        next_input = thread_state.queued_inputs.pop(0)
        await self._send_thread_input(
            thread_state,
            text=next_input.text,
            source_chat_id=next_input.chat_id,
            source_message_id=next_input.message_id,
        )

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

        first_sent: SentMessage | None = None
        current_reply_to = reply_to_message_id
        for chunk in render_markdown_chunks(text, self.config.bridge.max_message_chars):
            sent = await self._safe_send_message(
                chat_id=chat_id,
                text=chunk.text,
                reply_to_message_id=current_reply_to,
                entities=chunk.entities or None,
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
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
    ) -> SentMessage | None:
        try:
            return await self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                entities=entities,
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

    def _parse_control_command(self, text: str) -> tuple[str, str | None] | None:
        stripped = text.strip()
        if not stripped:
            return None
        command, _, remainder = stripped.partition(" ")
        normalized = command.lower().removeprefix("/")
        if normalized != "attach":
            return None
        argument = remainder.strip() or None
        return normalized, argument

    def _extract_latest_agent_message_from_conversation(
        self,
        conversation: DesktopConversation,
    ) -> tuple[str, str | None, str] | None:
        for turn in reversed(conversation.turns):
            item_id, text = extract_latest_agent_message_from_turn(turn.raw)
            if item_id is None or text is None:
                continue
            return item_id, turn.turn_id, text
        return None

    def _find_pending_approval(self, thread_id: str, request_id: str) -> PendingApproval | None:
        for pending in self._pending_approvals.values():
            if pending.thread_id == thread_id and pending.request_id == request_id:
                return pending
        return None

    def _next_callback_key(self) -> str:
        self._next_approval_key += 1
        return str(self._next_approval_key)

    def _format_desktop_request_prompt(self, request: DesktopRequest) -> str:
        kind = request.kind.lower()
        if "command" in kind:
            return format_approval_prompt("command", request.raw, None)
        if "file" in kind:
            return format_approval_prompt("file", request.raw, None)
        if "permission" in kind:
            return format_approval_prompt("permissions", request.raw, None)

        lines = [f"Approval needed: {request.kind}"]
        reason = request.raw.get("reason")
        if reason:
            lines.append(f"Reason: {reason}")
        command = request.raw.get("command")
        if command:
            lines.extend(["", "Command:", str(command)])
        cwd = request.raw.get("cwd")
        if cwd:
            lines.append(f"CWD: {cwd}")
        return "\n".join(lines).strip()

    def _remove_pending_message(self, thread_state: ThreadState, message_id: int) -> None:
        thread_state.pending_message_ids = [
            pending_message_id
            for pending_message_id in thread_state.pending_message_ids
            if pending_message_id != message_id
        ]

    def _build_project_picker_keyboard(
        self,
        callback_key: str,
        projects: list[DesktopProject],
    ) -> list[list[dict[str, Any]]]:
        duplicate_labels = {
            project.label
            for project in projects
            if sum(1 for candidate in projects if candidate.label == project.label) > 1
        }
        keyboard = [
            [
                {
                    "text": self._format_project_button_text(project, include_path=project.label in duplicate_labels),
                    "callback_data": f"project:{callback_key}:{index}",
                }
            ]
            for index, project in enumerate(projects)
        ]
        keyboard.append([{"text": "Cancel", "callback_data": f"project-cancel:{callback_key}"}])
        return keyboard

    def _build_attach_picker_keyboard(
        self,
        callback_key: str,
        threads: list[DesktopConversationSummary],
    ) -> list[list[dict[str, Any]]]:
        keyboard = [
            [{"text": self._format_attach_button_text(thread), "callback_data": f"attach:{callback_key}:{index}"}]
            for index, thread in enumerate(threads)
        ]
        keyboard.append([{"text": "Cancel", "callback_data": f"attach-cancel:{callback_key}"}])
        return keyboard

    def _format_project_button_text(self, project: DesktopProject, *, include_path: bool) -> str:
        if not include_path:
            return project.label
        return f"{project.label} ({project.path})"

    def _format_attach_button_text(self, thread: DesktopConversationSummary) -> str:
        project = (thread.project_label or "project").strip()
        title = (thread.title or thread.thread_id).strip()
        if len(title) > 36:
            title = f"{title[:33]}..."
        return f"{project}: {title}"

    async def _clear_project_selection(self, pending: PendingProjectSelection, *, delete_message: bool) -> None:
        if delete_message:
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=pending.chat_id, message_id=pending.picker_message_id)
        self._forget_cleanup_message(chat_id=pending.chat_id, message_id=pending.picker_message_id)
        await self._save_state()

    async def _clear_attach_selection(self, pending: PendingAttachSelection, *, delete_message: bool) -> None:
        if delete_message:
            with contextlib.suppress(Exception):
                await self.telegram.delete_message(chat_id=pending.chat_id, message_id=pending.picker_message_id)
        self._forget_cleanup_message(chat_id=pending.chat_id, message_id=pending.picker_message_id)
        await self._save_state()

    async def _delete_callback_message(self, callback_message: dict[str, Any]) -> None:
        chat = callback_message.get("chat") or {}
        message_id = callback_message.get("message_id")
        chat_id = chat.get("id")
        if message_id is None or chat_id is None:
            return
        with contextlib.suppress(Exception):
            await self.telegram.delete_message(chat_id=int(chat_id), message_id=int(message_id))
        self._forget_cleanup_message(chat_id=int(chat_id), message_id=int(message_id))
        await self._save_state()

    def _forget_cleanup_message(self, *, chat_id: int, message_id: int) -> None:
        self.state.approval_cleanup_messages = [
            item
            for item in self.state.approval_cleanup_messages
            if not (item.chat_id == chat_id and item.message_id == message_id)
        ]

    async def _save_state(self) -> None:
        async with self._state_lock:
            self.state.save(self.config.bridge.state_path)


__all__ = ["BridgeApp"]
