from __future__ import annotations

import asyncio
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
        poll_interval_seconds: float,
    ) -> None:
        self._app_path = app_path
        self._remote_debugging_port = remote_debugging_port
        self._user_data_dir = user_data_dir
        self._launch_timeout_seconds = launch_timeout_seconds
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

    async def start_new_thread(self, project_path: str, text: str) -> DesktopConversation:
        before = {thread.thread_id for thread in await self.list_threads()}
        expected_text = text.strip()
        project = await self._project_for_path(project_path)
        if project is None:
            raise DesktopClientError(f"Desktop project {project_path!r} is not available for starting a new thread.")
        await self._click_project_new_thread_button(project.path)
        await asyncio.sleep(self._poll_interval_seconds)
        await self._focus_composer()
        await self._insert_text(text.rstrip() + "\n")
        clicked = await self._eval_json(_click_send_button_js())
        if not clicked or not clicked.get("ok"):
            error = clicked.get("error") if isinstance(clicked, dict) else None
            detail = f" ({error})" if isinstance(error, str) and error else ""
            raise DesktopClientError(f"Desktop send button is not available for a new thread{detail}.")

        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
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
                    return conversation
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
        composer_text = await self._read_composer_text()
        if composer_text.strip():
            raise DesktopClientError(
                f"Desktop composer is not empty for thread {thread_id}. "
                "Send or discard the draft in Codex Desktop, then retry."
            )
        await self._focus_composer()
        await self._insert_text(text.rstrip() + "\n")
        clicked = await self._eval_json(_click_send_button_js())
        if not clicked or not clicked.get("ok"):
            latest = await self.read_thread(thread_id)
            if latest is not None:
                _raise_if_thread_turn_is_active(thread_id, latest)
            error = clicked.get("error") if isinstance(clicked, dict) else None
            detail = f" ({error})" if isinstance(error, str) and error else ""
            raise DesktopClientError(f"Desktop send button is not available{detail}.")

        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
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
        raise DesktopClientError(f"Desktop did not create a new turn for thread {thread_id}.")

    async def click_approval_action(self, thread_id: str, *, approve: bool) -> None:
        await self.activate_thread(thread_id)
        labels = ["Approve", "Accept", "Allow"] if approve else ["Deny", "Decline"]
        result = await self._eval_json(_click_text_button_js(labels))
        if not result or not result.get("ok"):
            action = "approve" if approve else "deny"
            raise DesktopClientError(f"Desktop did not expose a visible {action} button for thread {thread_id}.")

    async def _focus_composer(self) -> None:
        focused = await self._eval_json(_FOCUS_COMPOSER_JS)
        if not focused or not focused.get("ok"):
            raise DesktopClientError("Desktop composer is not available.")

    async def _read_composer_text(self) -> str:
        payload = await self._eval_json(_COMPOSER_STATE_JS)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise DesktopClientError("Desktop composer is not available.")
        text = payload.get("text")
        return str(text) if isinstance(text, str) else ""

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
  const row = document.querySelector('[role="button"][aria-current="page"]');
  if (!row) return { threadId: null };
  const parent = row.parentElement;
  const key = parent ? Object.getOwnPropertyNames(parent).find((name) => name.startsWith('__reactProps')) : null;
  const conversation = key && parent ? parent[key]?.children?.props?.item?.task?.conversation : null;
  return { threadId: conversation?.id ?? null };
})()
"""

_FOCUS_COMPOSER_JS = """
(() => {
  const composer = document.querySelector('.ProseMirror[contenteditable="true"]');
  if (!composer) return { ok: false };
  composer.focus();
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(composer);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
  return { ok: true };
})()
"""

_COMPOSER_STATE_JS = """
(() => {
  const composer = document.querySelector('.ProseMirror[contenteditable="true"]');
  if (!composer) return { ok: false };
  return { ok: true, text: composer.innerText ?? '' };
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

  const section = [...document.querySelectorAll('[role="listitem"]')].find((node) => {{
    if (node.getAttribute('aria-label') !== group.label) return false;
    return [...node.querySelectorAll('button')].some(
      (button) => button.getAttribute('aria-label') === `Start new thread in ${{group.label}}`
    );
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
  const button = [...section.querySelectorAll('button')].find(
    (node) => node.getAttribute('aria-label') === `Start new thread in ${{group.label}}`
  );
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
  const labels = {_quote_json(labels)};
  const button = [...document.querySelectorAll('button')].find((node) => {{
    const text = (node.innerText || node.textContent || '').trim();
    const aria = (node.getAttribute('aria-label') || '').trim();
    return labels.includes(text) || labels.includes(aria);
  }});
  if (!button) return {{ ok: false }};
  button.click();
  return {{ ok: true }};
}})()
"""


def _click_send_button_js() -> str:
    return """
(() => {
  const composer = document.querySelector('.ProseMirror[contenteditable="true"]');
  if (!composer) return { ok: false, error: 'no-composer' };
  const panel = composer.closest('div.bg-token-input-background');
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
})()
"""


def _raise_if_thread_turn_is_active(thread_id: str, conversation: DesktopConversation) -> None:
    latest_turn = conversation.latest_turn
    if latest_turn is None or latest_turn.is_terminal:
        return
    turn_id = latest_turn.turn_id or "unknown"
    raise DesktopClientError(
        f"Desktop thread {thread_id} is still running turn {turn_id}. Wait for it to finish, then retry."
    )


__all__ = [
    "CodexDesktopClient",
    "DesktopClientError",
    "DesktopConversation",
    "DesktopConversationSummary",
    "DesktopProject",
    "DesktopRequest",
    "DesktopSessionInfo",
    "DesktopTurn",
    "TERMINAL_TURN_STATUSES",
]
