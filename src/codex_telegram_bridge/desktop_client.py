from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

logger = logging.getLogger(__name__)

TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted", "cancelled"}


class DesktopClientError(RuntimeError):
    pass


class DesktopDraftConflictError(DesktopClientError):
    def __init__(self, *, context: str, draft_text: str) -> None:
        self.context = context
        self.draft_text = draft_text
        super().__init__(
            f"Desktop composer is not empty for {context}. Send or discard the draft in Codex Desktop, then retry."
        )


class DesktopSendUnconfirmedError(DesktopClientError):
    def __init__(self, *, thread_id: str, expected_text: str, after_turn_count: int) -> None:
        self.thread_id = thread_id
        self.expected_text = expected_text
        self.after_turn_count = after_turn_count
        super().__init__(f"Desktop did not create a new turn for thread {thread_id}.")


@dataclass(slots=True)
class DesktopSessionInfo:
    debugger_url: str
    page_url: str
    page_title: str


@dataclass(slots=True)
class DesktopRequest:
    request_id: str
    kind: str
    raw: dict[str, Any]


@dataclass(slots=True)
class DesktopTurn:
    turn_id: str | None
    status: str | None
    items: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return (self.status or "") in TERMINAL_TURN_STATUSES


@dataclass(slots=True)
class DesktopConversation:
    thread_id: str
    title: str | None
    cwd: str | None
    host_id: str | None
    source: str | None
    turns: list[DesktopTurn] = field(default_factory=list)
    requests: list[DesktopRequest] = field(default_factory=list)
    runtime_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def latest_turn(self) -> DesktopTurn | None:
        if not self.turns:
            return None
        return self.turns[-1]

    @property
    def preview(self) -> str | None:
        if self.title:
            return self.title
        latest = self.latest_turn
        if latest is None:
            return None
        for item in latest.items:
            if item.get("type") == "userMessage":
                content = item.get("content") or []
                if content and isinstance(content[0], dict):
                    text = content[0].get("text")
                    if text:
                        return str(text).strip() or None
        return None


@dataclass(slots=True)
class DesktopConversationSummary:
    thread_id: str
    title: str | None
    current: bool
    cwd: str | None
    project_label: str | None = None
    project_path: str | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class DesktopProject:
    label: str
    path: str


class CodexDesktopClient:
    def __init__(
        self,
        *,
        app_path: Path,
        remote_debugging_port: int,
        user_data_dir: Path,
        launch_timeout_seconds: float,
        send_ack_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        self._app_path = app_path
        self._remote_debugging_port = remote_debugging_port
        self._user_data_dir = user_data_dir
        self._launch_timeout_seconds = launch_timeout_seconds
        self._send_ack_timeout_seconds = send_ack_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._rpc_lock = asyncio.Lock()
        self._ready_lock = asyncio.Lock()
        self._page_ws_url: str | None = None
        self._next_message_id = 0
        self._cdp_ready = False

    async def start(self) -> DesktopSessionInfo:
        await self._ensure_cdp_ready()
        target = await self._fetch_primary_page_target()
        return DesktopSessionInfo(
            debugger_url=self._page_ws_url or "",
            page_url=str(target.get("url") or ""),
            page_title=str(target.get("title") or ""),
        )

    async def wait_until_task_index_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while True:
            payload = await self._eval_json(_REACT_TREE_INDEX_STATUS_JS)
            if isinstance(payload, dict) and int(payload.get("groupCount") or 0) > 0:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise DesktopClientError("Codex Desktop did not expose the React task index in time.")
            await asyncio.sleep(self._poll_interval_seconds)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self._http.aclose()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            if self._proc.returncode is None:
                self._proc.kill()
                await self._proc.wait()

    async def list_threads(self) -> list[DesktopConversationSummary]:
        payload = await self._eval_json(_REACT_TREE_CONVERSATIONS_JS)
        conversations = payload.get("conversations") if isinstance(payload, dict) else None
        current_thread_id = payload.get("currentConversationId") if isinstance(payload, dict) else None
        result: list[DesktopConversationSummary] = []
        for raw in conversations or []:
            conversation = self._parse_conversation(raw or {})
            result.append(
                DesktopConversationSummary(
                    thread_id=conversation.thread_id,
                    title=conversation.title,
                    current=conversation.thread_id == current_thread_id,
                    cwd=conversation.cwd,
                    project_label=str(raw.get("projectLabel")) if raw.get("projectLabel") is not None else None,
                    project_path=str(raw.get("projectPath")) if raw.get("projectPath") is not None else None,
                    updated_at=_coerce_datetime_optional(raw.get("updatedAt")),
                )
            )
        return result

    async def read_thread(self, thread_id: str) -> DesktopConversation | None:
        conversation = await self._eval_json(_read_thread_from_react_js(thread_id))
        if not conversation:
            return None
        return self._parse_conversation(conversation)

    async def list_projects(self) -> list[DesktopProject]:
        payload = await self._eval_json(_SIDEBAR_PROJECTS_JS)
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list):
            return []
        result: list[DesktopProject] = []
        seen_paths: set[str] = set()
        for raw in projects:
            if not isinstance(raw, dict):
                continue
            label = raw.get("label")
            path = raw.get("path")
            if not isinstance(label, str) or not label.strip():
                continue
            if not isinstance(path, str) or not path.strip():
                continue
            normalized_path = path.strip()
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            result.append(DesktopProject(label=label.strip(), path=normalized_path))
        return result

    async def current_thread_id(self) -> str | None:
        return await self._current_thread_id()

    async def read_composer_state(self) -> dict[str, Any]:
        payload = await self._eval_json(_COMPOSER_STATE_JS)
        if not isinstance(payload, dict):
            return {"ok": False, "text": ""}
        text = payload.get("text")
        return {
            "ok": bool(payload.get("ok")),
            "text": str(text) if isinstance(text, str) else "",
        }

    async def list_visible_buttons(self) -> list[str]:
        payload = await self._eval_json(_VISIBLE_BUTTONS_JS)
        if not isinstance(payload, dict):
            return []
        buttons = payload.get("buttons")
        if not isinstance(buttons, list):
            return []
        return [str(button) for button in buttons if isinstance(button, str)]

    async def snapshot(self) -> dict[str, Any]:
        current_thread_id = await self.current_thread_id()
        current_thread = await self.read_thread(current_thread_id) if current_thread_id is not None else None
        return {
            "current_thread_id": current_thread_id,
            "current_thread": current_thread,
            "threads": await self.list_threads(),
            "projects": await self.list_projects(),
            "composer": await self.read_composer_state(),
            "visible_buttons": await self.list_visible_buttons(),
        }

    async def capture_screenshot(self, path: Path) -> Path:
        payload = await self._call_cdp("Page.captureScreenshot", {"format": "png"})
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            raise DesktopClientError("Desktop did not return screenshot bytes.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    async def start_new_thread(
        self,
        project_path: str,
        text: str,
        *,
        replace_existing_draft: bool = False,
    ) -> DesktopConversation:
        before = {thread.thread_id for thread in await self.list_threads()}
        expected_text = text.strip()
        project = await self._project_for_path(project_path)
        if project is None:
            raise DesktopClientError(f"Desktop project {project_path!r} is not available for starting a new thread.")
        await self._click_project_new_thread_button(project.path)
        await asyncio.sleep(self._poll_interval_seconds)
        composer_text = await self._read_composer_text()
        if composer_text.strip():
            if not replace_existing_draft:
                raise DesktopDraftConflictError(context="a new thread", draft_text=composer_text)
            await self._clear_visible_composer()
        await self._focus_composer()
        await self._insert_text(text.rstrip() + "\n")
        await self._wait_for_composer_text(expected_text=expected_text, error_context="a new thread")
        clicked = await self._eval_json(_click_send_button_js())
        if not clicked or not clicked.get("ok"):
            error = clicked.get("error") if isinstance(clicked, dict) else None
            detail = f" ({error})" if isinstance(error, str) and error else ""
            raise DesktopClientError(f"Desktop send button is not available for a new thread{detail}.")

        deadline = asyncio.get_running_loop().time() + self._send_ack_timeout_limit_seconds()
        while True:
            threads = await self.list_threads()
            for thread in threads:
                if thread.thread_id not in before:
                    conversation = await self.read_thread(thread.thread_id)
                    if conversation is not None and _conversation_has_user_message(
                        conversation,
                        expected_text=expected_text,
                        after_turn_count=0,
                    ):
                        return conversation
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        raise DesktopClientError(
            f"Desktop did not expose the new thread with the expected first message in project {project.label!r}."
        )

    async def activate_thread(self, thread_id: str) -> DesktopConversation:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_error = "Desktop thread is not visible in the current sidebar data."
        while True:
            result = await self._prepare_thread_activation(thread_id)
            if isinstance(result, dict):
                error = result.get("error")
                if isinstance(error, str) and error:
                    last_error = error
            conversation = await self.read_thread(thread_id)
            if conversation is not None:
                current = await self._current_thread_id()
                if current == thread_id:
                    header_title = await self._read_thread_header_title()
                    if _header_matches_conversation(header_title, conversation):
                        return conversation
                    if header_title:
                        last_error = f"header-title-mismatch:{header_title}"
                    else:
                        last_error = "missing-thread-header-title"
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        raise DesktopClientError(f"Desktop did not activate thread {thread_id}: {last_error}")

    async def send_message(self, thread_id: str, text: str) -> DesktopConversation:
        before = await self.read_thread(thread_id)
        if before is None:
            raise DesktopClientError(f"Desktop thread {thread_id} is not available for sending.")
        _raise_if_thread_turn_is_active(thread_id, before)
        await self.activate_thread(thread_id)
        baseline = await self.read_thread(thread_id)
        if baseline is None:
            raise DesktopClientError(f"Desktop thread {thread_id} is not available for sending.")
        _raise_if_thread_turn_is_active(thread_id, baseline)
        before_turn_count = len(baseline.turns)
        expected_text = text.strip()
        await self._wait_for_empty_composer(thread_id)
        await self._focus_composer()
        await self._insert_text(text.rstrip() + "\n")
        await self._wait_for_composer_text(expected_text=expected_text, error_context=f"thread {thread_id}")
        clicked = await self._eval_json(_click_send_button_js())
        if not clicked or not clicked.get("ok"):
            latest = await self.read_thread(thread_id)
            if latest is not None:
                _raise_if_thread_turn_is_active(thread_id, latest)
            error = clicked.get("error") if isinstance(clicked, dict) else None
            detail = f" ({error})" if isinstance(error, str) and error else ""
            raise DesktopClientError(f"Desktop send button is not available{detail}.")

        deadline = asyncio.get_running_loop().time() + self._send_ack_timeout_limit_seconds()
        while True:
            current = await self.read_thread(thread_id)
            if current is not None and _conversation_has_user_message(
                current,
                expected_text=expected_text,
                after_turn_count=before_turn_count,
            ):
                return current
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        raise DesktopSendUnconfirmedError(
            thread_id=thread_id,
            expected_text=expected_text,
            after_turn_count=before_turn_count,
        )

    def _send_ack_timeout_limit_seconds(self) -> float:
        return min(self._launch_timeout_seconds, self._send_ack_timeout_seconds)

    async def click_approval_action(
        self,
        thread_id: str,
        *,
        approve: bool,
        labels: list[str] | None = None,
    ) -> None:
        await self.activate_thread(thread_id)
        candidate_labels = _merge_button_labels(
            labels,
            ["Approve", "Accept", "Allow"] if approve else ["Deny", "Decline"],
        )
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_visible_buttons: list[str] = []
        while True:
            result = await self._eval_json(_click_text_button_js(candidate_labels))
            if result and result.get("ok"):
                return
            if isinstance(result, dict):
                visible_buttons = result.get("visibleButtons")
                if isinstance(visible_buttons, list):
                    last_visible_buttons = [str(label) for label in visible_buttons if isinstance(label, str)]
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)

        action = "approve" if approve else "deny"
        detail = ""
        if last_visible_buttons:
            detail = f" Visible buttons: {', '.join(last_visible_buttons)}."
        raise DesktopClientError(f"Desktop did not expose a visible {action} button for thread {thread_id}.{detail}")

    async def _focus_composer(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_error = "no-visible-composer"
        while True:
            focused = await self._eval_json(_FOCUS_COMPOSER_JS)
            if isinstance(focused, dict):
                if focused.get("ok"):
                    return
                error = focused.get("error")
                if isinstance(error, str) and error:
                    last_error = error
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        detail = f" ({last_error})" if last_error else ""
        raise DesktopClientError(f"Desktop composer is not available{detail}.")

    async def _clear_visible_composer(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_error = "no-visible-composer"
        while True:
            result = await self._eval_json(_CLEAR_COMPOSER_JS)
            if isinstance(result, dict):
                if result.get("ok"):
                    composer_text = await self._read_composer_text()
                    if not composer_text.strip():
                        return
                    last_error = "composer-not-cleared"
                else:
                    error = result.get("error")
                    if isinstance(error, str) and error:
                        last_error = error
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        detail = f" ({last_error})" if last_error else ""
        raise DesktopClientError(f"Desktop composer could not be cleared{detail}.")

    async def _read_composer_text(self) -> str:
        payload = await self._eval_json(_COMPOSER_STATE_JS)
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else None
            detail = f" ({error})" if isinstance(error, str) and error else ""
            raise DesktopClientError(f"Desktop composer is not available{detail}.")
        text = payload.get("text")
        return str(text) if isinstance(text, str) else ""

    async def _read_thread_header_title(self) -> str | None:
        payload = await self._eval_json(_THREAD_HEADER_TITLE_JS)
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        title = payload.get("title")
        if not isinstance(title, str):
            return None
        normalized = title.strip()
        return normalized or None

    async def _wait_for_empty_composer(self, thread_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while True:
            composer_text = await self._read_composer_text()
            if not composer_text.strip():
                return
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)

        raise DesktopClientError(
            f"Desktop composer is not empty for thread {thread_id}. "
            "Send or discard the draft in Codex Desktop, then retry."
        )

    async def _wait_for_composer_text(self, *, expected_text: str, error_context: str) -> None:
        normalized_expected = expected_text.strip()
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while True:
            composer_text = await self._read_composer_text()
            if composer_text.strip() == normalized_expected:
                return
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)

        raise DesktopClientError(
            f"Desktop composer did not accept the expected text for {error_context}."
        )

    async def _prepare_thread_activation(self, thread_id: str) -> dict[str, Any] | None:
        result = await self._eval_json(_prepare_thread_activation_js(thread_id))
        return result if isinstance(result, dict) else None

    async def _insert_text(self, text: str) -> None:
        await self._call_cdp("Input.insertText", {"text": text})

    async def _project_for_path(self, project_path: str) -> DesktopProject | None:
        for project in await self.list_projects():
            if project.path == project_path:
                return project
        return None

    async def _click_project_new_thread_button(self, project_path: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        last_error = f"Desktop project {project_path!r} is not available for starting a new thread."
        while True:
            result = await self._eval_json(_project_button_center_js(project_path))
            if isinstance(result, dict):
                if result.get("ok") and result.get("phase") == "clicked":
                    return
                error = result.get("error")
                if isinstance(error, str) and error:
                    last_error = error
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        raise DesktopClientError(last_error)

    async def _current_thread_id(self) -> str | None:
        data = await self._eval_json(_CURRENT_THREAD_ID_JS)
        if not isinstance(data, dict):
            return None
        thread_id = data.get("threadId")
        return str(thread_id) if thread_id else None

    async def _dispatch_mouse_click(self, x: float, y: float) -> None:
        await self._call_cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        await self._call_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await self._call_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    async def _ensure_page_connection(self) -> None:
        target = await self._fetch_primary_page_target()
        if target is None:
            await self._launch_desktop()
            target = await self._wait_for_page_target()
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise DesktopClientError("Desktop remote debugging target does not expose a websocket URL.")
        if self._ws is None or self._page_ws_url != ws_url or self._ws_state() is not State.OPEN:
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            self._ws = await websockets.connect(ws_url, max_size=50_000_000)
            self._page_ws_url = ws_url
            self._cdp_ready = False

    async def _ensure_cdp_ready(self) -> None:
        await self._ensure_page_connection()
        if self._cdp_ready:
            return
        async with self._ready_lock:
            await self._ensure_page_connection()
            if self._cdp_ready:
                return
            await self._enable_cdp_domains()
            self._cdp_ready = True

    async def _wait_for_page_target(self) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while True:
            target = await self._fetch_primary_page_target()
            if target is not None:
                return target
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(self._poll_interval_seconds)
        raise DesktopClientError(
            f"Codex Desktop did not expose a remote debugging page on port {self._remote_debugging_port}."
        )

    async def _fetch_primary_page_target(self) -> dict[str, Any] | None:
        try:
            response = await self._http.get(f"http://127.0.0.1:{self._remote_debugging_port}/json/list")
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        try:
            targets = response.json()
        except ValueError as exc:
            raise DesktopClientError("Desktop returned invalid JSON from the remote debugging endpoint.") from exc
        if not isinstance(targets, list):
            return None
        for target in targets:
            if not isinstance(target, dict):
                continue
            if target.get("type") != "page":
                continue
            if target.get("title") == "Codex":
                return target
        for target in targets:
            if isinstance(target, dict) and target.get("type") == "page":
                return target
        return None

    async def _launch_desktop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        binary = self._app_path / "Contents" / "MacOS" / "Codex"
        if not binary.exists():
            raise DesktopClientError(f"Codex Desktop binary not found at {binary}.")
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            str(binary),
            f"--remote-debugging-port={self._remote_debugging_port}",
            f"--user-data-dir={self._user_data_dir}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _enable_cdp_domains(self) -> None:
        await self._call_cdp("Page.enable", _skip_ready=True, _allow_reconnect=False)
        await self._call_cdp("Runtime.enable", _skip_ready=True, _allow_reconnect=False)
        await self._call_cdp("DOM.enable", _skip_ready=True, _allow_reconnect=False)
        await self._call_cdp("Input.setIgnoreInputEvents", {"ignore": False}, _skip_ready=True, _allow_reconnect=False)

    async def _eval_json(self, expression: str) -> Any:
        result = await self._call_cdp(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        value = (result.get("result") or {}).get("value")
        return value

    async def _call_cdp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        _skip_ready: bool = False,
        _allow_reconnect: bool = True,
    ) -> dict[str, Any]:
        if not _skip_ready:
            await self._ensure_cdp_ready()
        if self._ws is None:
            raise DesktopClientError("Desktop websocket is not connected.")
        async with self._rpc_lock:
            try:
                self._next_message_id += 1
                message_id = self._next_message_id
                await self._ws.send(
                    json.dumps(
                        {
                            "id": message_id,
                            "method": method,
                            "params": params or {},
                        }
                    )
                )
                while True:
                    raw = await self._ws.recv()
                    message = json.loads(raw)
                    if message.get("id") != message_id:
                        continue
                    if "error" in message:
                        raise DesktopClientError(
                            f"CDP call {method} failed: {message['error'].get('message', 'unknown error')}"
                        )
                    return message.get("result") or {}
            except (ConnectionClosed, OSError) as exc:
                self._reset_connection_state()
                if not _allow_reconnect:
                    raise DesktopClientError(f"Desktop websocket closed during {method}.") from exc
        await self._ensure_cdp_ready()
        return await self._call_cdp(
            method,
            params,
            _skip_ready=True,
            _allow_reconnect=False,
        )

    def _reset_connection_state(self) -> None:
        self._ws = None
        self._page_ws_url = None
        self._cdp_ready = False

    def _ws_state(self) -> State | None:
        if self._ws is None:
            return None
        return getattr(self._ws, "state", None)

    def _parse_conversation(self, raw: dict[str, Any]) -> DesktopConversation:
        turns = [
            DesktopTurn(
                turn_id=str(turn.get("turnId")) if turn.get("turnId") is not None else None,
                status=str(turn.get("status")) if turn.get("status") is not None else None,
                items=list(turn.get("items") or []),
                error=dict(turn.get("error") or {}) or None,
                raw=dict(turn),
            )
            for turn in raw.get("turns") or []
            if isinstance(turn, dict)
        ]
        requests = [
            DesktopRequest(
                request_id=self._coerce_request_id(request),
                kind=self._coerce_request_kind(request),
                raw=dict(request),
            )
            for request in raw.get("requests") or []
            if isinstance(request, dict)
        ]
        runtime_status = None
        runtime = raw.get("threadRuntimeStatus")
        if isinstance(runtime, dict) and runtime.get("type") is not None:
            runtime_status = str(runtime.get("type"))
        return DesktopConversation(
            thread_id=str(raw.get("id")),
            title=str(raw.get("title")) if raw.get("title") is not None else None,
            cwd=str(raw.get("cwd")) if raw.get("cwd") is not None else None,
            host_id=str(raw.get("hostId")) if raw.get("hostId") is not None else None,
            source=str(raw.get("source")) if raw.get("source") is not None else None,
            turns=turns,
            requests=requests,
            runtime_status=runtime_status,
            raw=dict(raw),
        )

    def _coerce_request_id(self, request: dict[str, Any]) -> str:
        for key in ("id", "requestId", "itemId", "toolCallId"):
            value = request.get(key)
            if value is not None:
                return str(value)
        return json.dumps(request, sort_keys=True, ensure_ascii=False)

    def _coerce_request_kind(self, request: dict[str, Any]) -> str:
        for key in ("kind", "type", "requestType"):
            value = request.get(key)
            if value is not None:
                return str(value)
        return "approval"


def _quote_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _coerce_datetime_optional(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000)


def _merge_button_labels(custom_labels: list[str] | None, default_labels: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for label in [*(custom_labels or []), *default_labels]:
        normalized = label.casefold().strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(label)
    return merged


def _conversation_has_user_message(
    conversation: DesktopConversation,
    *,
    expected_text: str,
    after_turn_count: int,
) -> bool:
    normalized_expected = expected_text.strip()
    if not normalized_expected:
        return False
    for turn in conversation.turns[after_turn_count:]:
        if _turn_has_matching_user_input(turn, normalized_expected):
            return True
    return False


def _turn_has_matching_user_input(turn: DesktopTurn, normalized_expected: str) -> bool:
    for item in turn.items:
        if item.get("type") != "userMessage":
            continue
        content = item.get("content") or []
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip() == normalized_expected:
                return True

    params = turn.raw.get("params") if isinstance(turn.raw, dict) else None
    inputs = params.get("input") if isinstance(params, dict) else None
    if not isinstance(inputs, list):
        return False
    for part in inputs:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip() == normalized_expected:
            return True
    return False


_REACT_TREE_CONVERSATIONS_JS = """
(() => {
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return { currentConversationId: null, conversations: [] };

  const stack = [start];
  const seen = new Set();
  const conversations = new Map();
  let sidebarStore = null;
  let currentConversationId = null;

  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (props && typeof props === 'object') {
      if (!currentConversationId && typeof props.currentConversationId === 'string' && props.currentConversationId) {
        currentConversationId = props.currentConversationId;
      }
      if (
        !sidebarStore &&
        Array.isArray(props.groups) &&
        props.collapsedGroups &&
        typeof props.setCollapsedGroups === 'function'
      ) {
        sidebarStore = props;
      }
      if (Array.isArray(props.tasks)) {
        for (const task of props.tasks) {
          const conversation = task?.conversation;
          if (!conversation?.id) continue;
          const existing = conversations.get(conversation.id);
          if (!existing || (conversation.updatedAt ?? 0) >= (existing.updatedAt ?? 0)) {
            conversations.set(conversation.id, conversation);
          }
        }
      }
    }

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }

  if (sidebarStore) {
    for (const group of sidebarStore.groups || []) {
      const projectLabel = typeof group?.label === 'string' && group.label ? group.label : null;
      const projectPath = typeof group?.path === 'string' && group.path ? group.path : null;
      for (const task of group?.tasks || []) {
        const conversation = task?.conversation;
        if (!conversation?.id) continue;
        const existing = conversations.get(conversation.id);
        if (!existing) continue;
        if (projectLabel && existing.projectLabel == null) {
          existing.projectLabel = projectLabel;
        }
        if (projectPath && existing.projectPath == null) {
          existing.projectPath = projectPath;
        }
      }
    }
  }

  return {
    currentConversationId,
    conversations: [...conversations.values()].sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0)),
  };
})()
"""

_REACT_TREE_INDEX_STATUS_JS = """
(() => {
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return { groupCount: 0 };

  const stack = [start];
  const seen = new Set();
  let groupCount = 0;

  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (props && typeof props === 'object' && Array.isArray(props.tasks)) {
      groupCount += 1;
    }

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }

  return { groupCount };
})()
"""

_SIDEBAR_PROJECTS_JS = """
(() => {
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return { projects: [] };

  const stack = [start];
  const seen = new Set();
  let sidebarStore = null;

  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (
      props &&
      typeof props === 'object' &&
      Array.isArray(props.groups) &&
      props.collapsedGroups &&
      typeof props.setCollapsedGroups === 'function'
    ) {
      sidebarStore = props;
      break;
    }

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }

  if (!sidebarStore) return { projects: [] };

  return {
    projects: sidebarStore.groups
      .filter((group) => typeof group?.label === 'string' && group.label && typeof group?.path === 'string' && group.path)
      .map((group) => ({
        label: group.label,
        path: group.path,
      })),
  };
})()
"""

_CURRENT_THREAD_ID_JS = """
(() => {
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return { threadId: null };

  const stack = [start];
  const seen = new Set();

  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (props && typeof props === 'object' && typeof props.currentConversationId === 'string' && props.currentConversationId) {
      return { threadId: props.currentConversationId };
    }

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }

  return { threadId: null };
})()
"""


def _with_visible_composer_js(body: str) -> str:
    return f"""
(() => {{
  const composerNodes = [...document.querySelectorAll('.ProseMirror[contenteditable="true"]')];
  const visibleEntries = composerNodes
    .map((node) => {{
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      const panel = node.closest('div[class*="bg-token-input-background"]');
      const panelRect = panel ? panel.getBoundingClientRect() : null;
      const visible =
        rect.width > 0 &&
        rect.height > 0 &&
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        node.getAttribute('aria-hidden') !== 'true' &&
        panelRect &&
        panelRect.width > 0 &&
        panelRect.height > 0;
      return {{
        node,
        panel,
        sortRect: panelRect || rect,
        visible,
      }};
    }})
    .filter((entry) => entry.visible)
    .sort((left, right) => {{
      if (right.sortRect.width !== left.sortRect.width) {{
        return right.sortRect.width - left.sortRect.width;
      }}
      if (right.sortRect.y !== left.sortRect.y) {{
        return right.sortRect.y - left.sortRect.y;
      }}
      return right.sortRect.x - left.sortRect.x;
    }});
  const composerInfo = visibleEntries[0]
    ? {{
        composer: visibleEntries[0].node,
        panel: visibleEntries[0].panel,
        composerCount: composerNodes.length,
        visibleComposerCount: visibleEntries.length,
      }}
    : null;
{body}
}})()
"""


_FOCUS_COMPOSER_JS = _with_visible_composer_js(
    """
  if (!composerInfo) {
    return { ok: false, error: 'no-visible-composer', composerCount: composerNodes.length, visibleComposerCount: 0 };
  }
  const composer = composerInfo.composer;
  composer.focus();
  const selection = window.getSelection();
  if (selection) {
    const range = document.createRange();
    range.selectNodeContents(composer);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  }
  const activeElement = document.activeElement;
  const focused = activeElement === composer || composer.contains(activeElement);
  return {
    ok: focused,
    error: focused ? null : 'composer-not-focused',
    composerCount: composerInfo.composerCount,
    visibleComposerCount: composerInfo.visibleComposerCount,
  };
"""
)

_CLEAR_COMPOSER_JS = _with_visible_composer_js(
    """
  if (!composerInfo) {
    return { ok: false, error: 'no-visible-composer', composerCount: composerNodes.length, visibleComposerCount: 0 };
  }
  const composer = composerInfo.composer;
  composer.focus();
  const selection = window.getSelection();
  if (!selection) {
    return { ok: false, error: 'no-selection' };
  }
  const range = document.createRange();
  range.selectNodeContents(composer);
  selection.removeAllRanges();
  selection.addRange(range);
  selection.deleteFromDocument();
  composer.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
  const text = composer.innerText ?? '';
  return {
    ok: !String(text).trim(),
    error: String(text).trim() ? 'composer-not-cleared' : null,
    text,
    composerCount: composerInfo.composerCount,
    visibleComposerCount: composerInfo.visibleComposerCount,
  };
"""
)

_COMPOSER_STATE_JS = _with_visible_composer_js(
    """
  if (!composerInfo) {
    return { ok: false, error: 'no-visible-composer', composerCount: composerNodes.length, visibleComposerCount: 0 };
  }
  const composer = composerInfo.composer;
  const activeElement = document.activeElement;
  return {
    ok: true,
    text: composer.innerText ?? '',
    focused: activeElement === composer || composer.contains(activeElement),
    composerCount: composerInfo.composerCount,
    visibleComposerCount: composerInfo.visibleComposerCount,
  };
"""
)

_THREAD_HEADER_TITLE_JS = """
(() => {
  const header = document.querySelector('header');
  if (!header) return { ok: false, error: 'no-header' };

  const candidates = [...header.querySelectorAll('.text-token-foreground')]
    .map((node) => {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      const text = String(node.innerText || node.textContent || '').trim();
      const visible =
        rect.width > 0 &&
        rect.height > 0 &&
        style.visibility !== 'hidden' &&
        style.display !== 'none';
      return {
        text,
        x: rect.x,
        y: rect.y,
        w: rect.width,
        h: rect.height,
        visible,
      };
    })
    .filter((entry) => entry.visible && entry.text)
    .sort((left, right) => {
      if (left.y !== right.y) {
        return left.y - right.y;
      }
      if (left.x !== right.x) {
        return left.x - right.x;
      }
      return left.text.length - right.text.length;
    });

  const title = candidates[0];
  if (!title) return { ok: false, error: 'no-header-title' };
  return { ok: true, title: title.text };
})()
"""

_VISIBLE_BUTTONS_JS = """
(() => {
  const buttons = [];
  const seen = new Set();

  for (const node of document.querySelectorAll('button')) {
    const rect = node.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0 && !node.disabled && node.getAttribute('aria-hidden') !== 'true';
    if (!visible) continue;

    const labels = [
      node.innerText || '',
      node.textContent || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
    ]
      .map((value) => String(value || '').trim())
      .filter(Boolean);

    for (const label of labels) {
      if (seen.has(label)) continue;
      seen.add(label);
      buttons.push(label);
    }
  }

  return { buttons };
})()
"""


def _read_thread_from_react_js(thread_id: str) -> str:
    return f"""
(() => {{
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return null;

  const stack = [start];
  const seen = new Set();

  while (stack.length) {{
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (props && typeof props === 'object' && Array.isArray(props.tasks)) {{
      for (const task of props.tasks) {{
        const conversation = task?.conversation;
        if (conversation?.id === {_quote_json(thread_id)}) {{
          return conversation;
        }}
      }}
    }}

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }}

  return null;
}})()
"""


def _prepare_thread_activation_js(thread_id: str) -> str:
    return f"""
(() => {{
  const targetThreadId = {_quote_json(thread_id)};
  const findRow = () => [...document.querySelectorAll('[role="button"]')].find((node) => {{
    const parent = node.parentElement;
    if (!parent) return false;
    const key = Object.getOwnPropertyNames(parent).find((name) => name.startsWith('__reactProps'));
    const conversation = key ? parent[key]?.children?.props?.item?.task?.conversation : null;
    return conversation?.id === targetThreadId;
  }});

  const clickRow = (row) => {{
    row.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    row.click();
    return {{ ok: true, phase: 'clicked' }};
  }};

  const visibleRow = findRow();
  if (visibleRow) {{
    return clickRow(visibleRow);
  }}

  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return {{ ok: false, error: 'no-react-root' }};

  const stack = [start];
  const seen = new Set();
  let sidebarStore = null;

  while (stack.length) {{
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (
      props &&
      typeof props === 'object' &&
      Array.isArray(props.groups) &&
      props.collapsedGroups &&
      typeof props.setCollapsedGroups === 'function'
    ) {{
      sidebarStore = props;
      break;
    }}

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }}

  if (!sidebarStore) return {{ ok: false, error: 'no-sidebar-store' }};

  const group = sidebarStore.groups.find((entry) =>
    Array.isArray(entry?.tasks) &&
    entry.tasks.some((task) => task?.conversation?.id === targetThreadId)
  );
  if (!group) return {{ ok: false, error: 'thread-not-in-sidebar-store' }};

  if (group.path && sidebarStore.collapsedGroups?.[group.path]) {{
    const nextCollapsedGroups = {{ ...sidebarStore.collapsedGroups }};
    delete nextCollapsedGroups[group.path];
    sidebarStore.setCollapsedGroups(nextCollapsedGroups);
    return {{
      ok: true,
      phase: 'expanded-group',
      groupLabel: group.label ?? null,
      groupPath: group.path,
    }};
  }}

  const section = [...document.querySelectorAll('[role="listitem"]')].find(
    (node) => node.getAttribute('aria-label') === group.label
  );
  if (section) {{
    section.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    const rowAfterScroll = findRow();
    if (rowAfterScroll) {{
      return clickRow(rowAfterScroll);
    }}
    return {{
      ok: true,
      phase: 'scrolled-group',
      groupLabel: group.label ?? null,
      groupPath: group.path ?? null,
    }};
  }}

  return {{
    ok: false,
    error: 'row-not-mounted',
    groupLabel: group.label ?? null,
    groupPath: group.path ?? null,
  }};
}})()
"""


def _project_button_center_js(project_path: str) -> str:
    return f"""
(() => {{
  const targetPath = {_quote_json(project_path)};
  const root = document.getElementById('root');
  const containerKey = root ? Object.getOwnPropertyNames(root).find((name) => name.startsWith('__reactContainer')) : null;
  const start = containerKey ? root[containerKey] : null;
  if (!start) return {{ ok: false, error: 'no-react-root' }};

  const stack = [start];
  const seen = new Set();
  let sidebarStore = null;

  while (stack.length) {{
    const fiber = stack.pop();
    if (!fiber || typeof fiber !== 'object' || seen.has(fiber)) continue;
    seen.add(fiber);

    const props = fiber.memoizedProps;
    if (
      props &&
      typeof props === 'object' &&
      Array.isArray(props.groups) &&
      props.collapsedGroups &&
      typeof props.setCollapsedGroups === 'function'
    ) {{
      sidebarStore = props;
      break;
    }}

    if (fiber.child) stack.push(fiber.child);
    if (fiber.sibling) stack.push(fiber.sibling);
  }}

  if (!sidebarStore) return {{ ok: false, error: 'no-sidebar-store' }};

  const group = sidebarStore.groups.find((entry) => entry?.path === targetPath);
  if (!group || typeof group.label !== 'string' || !group.label) {{
    return {{ ok: false, error: `project-not-in-sidebar-store:${{targetPath}}` }};
  }}

  if (group.path && sidebarStore.collapsedGroups?.[group.path]) {{
    const nextCollapsedGroups = {{ ...sidebarStore.collapsedGroups }};
    delete nextCollapsedGroups[group.path];
    sidebarStore.setCollapsedGroups(nextCollapsedGroups);
    return {{ ok: true, phase: 'expanded-group', groupLabel: group.label, groupPath: group.path }};
  }}

  const matchesStartButton = (node) => {{
    const aria = String(node?.getAttribute('aria-label') || '').trim().toLowerCase();
    const expectedSuffix = ` in ${{group.label}}`.toLowerCase();
    return aria.startsWith('start new ') && aria.endsWith(expectedSuffix);
  }};

  const section = [...document.querySelectorAll('[role="listitem"]')].find((node) => {{
    if (node.getAttribute('aria-label') !== group.label) return false;
    return [...node.querySelectorAll('button')].some(matchesStartButton);
  }});
  if (!section) {{
    return {{
      ok: false,
      error: `project-row-not-mounted:${{group.path}}`,
      groupLabel: group.label,
      groupPath: group.path ?? null,
    }};
  }}

  section.scrollIntoView({{ block: 'center', inline: 'nearest' }});
  const button = [...section.querySelectorAll('button')].find(matchesStartButton);
  if (!button) {{
    return {{
      ok: false,
      error: `project-button-not-mounted:${{group.path}}`,
      groupLabel: group.label,
      groupPath: group.path ?? null,
    }};
  }}

  button.click();
  return {{ ok: true, phase: 'clicked', groupLabel: group.label, groupPath: group.path ?? null }};
}})()
"""


def _click_text_button_js(labels: list[str]) -> str:
    return f"""
(() => {{
  const normalize = (value) => String(value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const labels = {_quote_json(labels)}
    .map((value) => normalize(value))
    .filter(Boolean);
  const labelMatches = (candidate, label) =>
    candidate === label || candidate.startsWith(`${{label}} `) || candidate.startsWith(`${{label}}\\n`);

  const buttonEntries = [...document.querySelectorAll('button')].map((node) => {{
    const rect = node.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0 && !node.disabled && node.getAttribute('aria-hidden') !== 'true';
    const rawLabels = [
      node.innerText || '',
      node.textContent || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
    ]
      .map((value) => String(value || '').trim())
      .filter(Boolean);
    const normalizedLabels = [...new Set(rawLabels.map((value) => normalize(value)).filter(Boolean))];
    return {{ node, visible, rawLabels, normalizedLabels }};
  }});

  const button = buttonEntries.find((entry) =>
    entry.visible && entry.normalizedLabels.some((candidate) => labels.some((label) => labelMatches(candidate, label)))
  );
  if (!button) {{
    const visibleButtons = [...new Set(
      buttonEntries
        .filter((entry) => entry.visible)
        .flatMap((entry) => entry.rawLabels)
    )];
    return {{ ok: false, visibleButtons }};
  }}
  button.node.click();
  return {{ ok: true }};
}})()
"""


def _click_send_button_js() -> str:
    return _with_visible_composer_js(
        """
  if (!composerInfo) return { ok: false, error: 'no-visible-composer' };
  const panel = composerInfo.panel;
  if (!panel) return { ok: false, error: 'no-composer-panel' };
  const button = [...panel.querySelectorAll('button')]
    .filter((node) => {
      const cls = node.className || '';
      if (typeof cls !== 'string') {
        return false;
      }
      const rect = node.getBoundingClientRect();
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        (cls.includes('size-token-button-composer') || cls.includes('bg-token-foreground'))
      );
    })
    .sort((left, right) => {
      const leftRect = left.getBoundingClientRect();
      const rightRect = right.getBoundingClientRect();
      if (leftRect.x !== rightRect.x) {
        return rightRect.x - leftRect.x;
      }
      return rightRect.y - leftRect.y;
    })[0];
  if (!button) return { ok: false, error: 'send-button-not-found' };
  if (button.disabled) return { ok: false, error: 'send-button-disabled' };
  button.click();
  return { ok: true };
"""
    )


def _raise_if_thread_turn_is_active(thread_id: str, conversation: DesktopConversation) -> None:
    latest_turn = conversation.latest_turn
    if latest_turn is None or latest_turn.is_terminal:
        return
    turn_id = latest_turn.turn_id or "unknown"
    raise DesktopClientError(
        f"Desktop thread {thread_id} is still running turn {turn_id}. Wait for it to finish, then retry."
    )


def _header_matches_conversation(header_title: str | None, conversation: DesktopConversation) -> bool:
    normalized_header = _normalize_header_title(header_title)
    if not normalized_header:
        return False
    for candidate in [conversation.title, conversation.preview]:
        normalized_candidate = _normalize_header_title(candidate)
        if normalized_candidate and normalized_candidate == normalized_header:
            return True
    return False


def _normalize_header_title(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    first_line = value.splitlines()[0].strip()
    return first_line.casefold()


__all__ = [
    "CodexDesktopClient",
    "DesktopClientError",
    "DesktopConversation",
    "DesktopConversationSummary",
    "DesktopSendUnconfirmedError",
    "DesktopProject",
    "DesktopRequest",
    "DesktopSessionInfo",
    "DesktopTurn",
    "TERMINAL_TURN_STATUSES",
]
