from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[str, int | str, dict[str, Any]], Awaitable[None]]


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data

    def __str__(self) -> str:
        return f"JsonRpcError(code={self.code}, message={super().__str__()})"


@dataclass(slots=True)
class InitializeResult:
    user_agent: str | None = None
    codex_home: str | None = None
    platform_family: str | None = None
    platform_os: str | None = None


class CodexAppServerClient:
    def __init__(
        self,
        command: list[str],
        *,
        client_name: str,
        client_title: str,
        client_version: str,
        experimental_api: bool,
        opt_out_notification_methods: list[str],
        notification_handler: NotificationHandler,
        request_handler: RequestHandler,
    ) -> None:
        self._command = command
        self._client_name = client_name
        self._client_title = client_title
        self._client_version = client_version
        self._experimental_api = experimental_api
        self._opt_out_notification_methods = opt_out_notification_methods
        self._notification_handler = notification_handler
        self._request_handler = request_handler

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._request_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._closed = False

    async def start(self) -> InitializeResult:
        if self._proc is not None:
            raise RuntimeError("Codex app-server already started")

        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._stdout_task = asyncio.create_task(self._stdout_loop(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="codex-app-server-stderr")

        init_result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self._client_name,
                    "title": self._client_title,
                    "version": self._client_version,
                },
                "capabilities": {
                    "experimentalApi": self._experimental_api,
                    "optOutNotificationMethods": self._opt_out_notification_methods,
                },
            },
        )
        await self.notify("initialized", {})
        return InitializeResult(
            user_agent=init_result.get("userAgent"),
            codex_home=init_result.get("codexHome"),
            platform_family=init_result.get("platformFamily"),
            platform_os=init_result.get("platformOs"),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server client closed"))
        self._pending.clear()

        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed closing Codex stdin")

        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = await self._next_request_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def respond_result(self, request_id: int | str, result: dict[str, Any]) -> None:
        await self._send({"id": request_id, "result": result})

    async def respond_error(self, request_id: int | str, code: int, message: str, data: Any = None) -> None:
        payload: dict[str, Any] = {"id": request_id, "error": {"code": code, "message": message}}
        if data is not None:
            payload["error"]["data"] = data
        await self._send(payload)

    async def _next_request_id(self) -> int:
        async with self._request_lock:
            self._request_id += 1
            return self._request_id

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Codex app-server is not running")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        logger.debug("codex -> %s", line)
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _stdout_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8").strip()
                if not raw:
                    continue
                logger.debug("codex <- %s", raw)
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid JSON from Codex: %s", raw)
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex stdout loop crashed")
        finally:
            self._fail_pending(RuntimeError("Codex app-server stdout closed"))

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").rstrip()
                if raw:
                    logger.info("codex stderr: %s", raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex stderr loop crashed")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        has_method = "method" in message
        has_id = "id" in message
        if has_method and has_id:
            await self._request_handler(str(message["method"]), message["id"], dict(message.get("params") or {}))
            return
        if has_method:
            await self._notification_handler(str(message["method"]), dict(message.get("params") or {}))
            return
        if has_id:
            future = self._pending.get(int(message["id"]))
            if future is None:
                logger.debug("Dropping unmatched response id=%s", message.get("id"))
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(
                    JsonRpcError(
                        int(error.get("code", -32000)),
                        str(error.get("message", "Unknown JSON-RPC error")),
                        error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result") or {})
            return
        logger.debug("Ignoring unknown JSON-RPC frame: %s", message)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


__all__ = ["CodexAppServerClient", "InitializeResult", "JsonRpcError"]
