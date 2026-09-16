"""Persistent bridge to an external MCP server started over stdio.

Keeps ONE long-lived server process and MCP session: the server is spawned
lazily on first tool use and reused for every later call (restarting once if
the session dies). All MCP traffic runs on a dedicated background event-loop
thread so callers stay synchronous.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from typing import Any, Callable, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 120.0
DEFAULT_CALL_TIMEOUT = 120.0


class ExternalMCPError(RuntimeError):
    """Raised when the external MCP server cannot be started or a call fails."""


class ExternalMCPService:
    """Calls tools exposed by a configured external MCP server."""

    def __init__(
        self,
        command: str = "duckduckgo-mcp-server",
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.command = command
        self.args = list(args) if args is not None else []
        self.env: dict[str, str] = {"DDG_REF_URL_THRESHOLD": "0", **(env or {})}
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[asyncio.Task] = None
        self._session: Optional[ClientSession] = None
        self._session_ready: Optional[asyncio.Event] = None
        self._shutdown: Optional[asyncio.Event] = None
        self._worker_error: Optional[str] = None

    # ── public API ─────────────────────────────────────────────────────────

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Ensure a live session, call one tool, and return its text content.

        A transport-level failure restarts the server once and retries the
        call; a second failure raises ``ExternalMCPError``. Tool-level errors
        (``isError``) are returned as text, not retried.
        """
        with self._lock:
            self._ensure_loop()
            last_error: Optional[Exception] = None
            for attempt in (1, 2):
                try:
                    result = self._submit(
                        lambda: self._call_on_loop(name, arguments),
                        timeout=self.call_timeout,
                    )
                    return _extract_text(name, result)
                except Exception as exc:  # noqa: BLE001 — retry once after restart
                    last_error = exc
                    logger.warning(
                        "External MCP tool '%s' failed (attempt %d/2): %s — restarting session",
                        name, attempt, exc,
                    )
                    self._submit(self._teardown_worker, timeout=30.0)
            raise ExternalMCPError(
                f"External MCP tool '{name}' failed: {last_error}"
            ) from last_error

    def close(self) -> None:
        """Stop the server session and the background event loop (idempotent)."""
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                return
            try:
                self._submit(self._teardown_worker, timeout=30.0)
            except Exception as exc:  # noqa: BLE001 — best effort
                logger.debug("External MCP close error: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
            self._thread = None

    # ── internals (loop thread) ────────────────────────────────────────────

    def _ensure_loop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="external-mcp-loop", daemon=True
        )
        self._thread.start()

    def _submit(self, coro_factory: Callable[[], Any], timeout: float) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result(timeout=timeout)

    async def _call_on_loop(self, name: str, arguments: dict[str, Any]):
        session = await self._ensure_session()
        return await session.call_tool(name, arguments)

    async def _ensure_session(self) -> ClientSession:
        if self._worker is None or self._worker.done():
            await self._start_worker()
        assert self._session_ready is not None
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=self.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise ExternalMCPError(
                f"External MCP server '{self.command}' did not start within "
                f"{self.connect_timeout}s"
            ) from exc
        if self._session is None:
            raise ExternalMCPError(
                f"External MCP server '{self.command}' failed to start: "
                f"{self._worker_error or 'unknown error'}"
            )
        return self._session

    async def _start_worker(self) -> None:
        environment = os.environ.copy()
        environment.update(self.env)
        # Keep the MCP subprocess from writing diagnostics into the REPL.
        environment.setdefault("LOG_LEVEL", "ERROR")
        environment.setdefault("PYTHONWARNINGS", "ignore")
        server = StdioServerParameters(
            command=self.command, args=self.args, env=environment
        )
        self._session_ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._worker_error = None
        self._worker = asyncio.create_task(
            self._serve(server, self._session_ready, self._shutdown)
        )

    async def _serve(
        self, server: StdioServerParameters, ready: asyncio.Event, shutdown: asyncio.Event
    ) -> None:
        """Owns the stdio + session context for one server lifetime.

        The context managers must be entered and exited in the SAME task
        (anyio task-group rule), hence the shutdown-event pattern instead of
        per-call ``async with``.
        """
        try:
            async with stdio_client(
                server, errlog=subprocess.DEVNULL
            ) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                ready.set()
                logger.info("External MCP server '%s' started", self.command)
                await shutdown.wait()
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            # Missing binary: retrying is pointless, so fail with a fix hint.
            self._worker_error = (
                f"executable not found: '{self.command}'. Install it "
                "(`uv tool install \"duckduckgo-mcp-server[browser]==0.7.0\"`), "
                "or point tools.mcp.search_command at it (in Docker that is "
                "/opt/mcp/ddg/bin/duckduckgo-mcp-server, already on PATH)."
            )
            logger.warning(
                "External MCP server '%s' not found on PATH", self.command
            )
        except BaseException as exc:  # noqa: BLE001 — surface to the caller task
            self._worker_error = f"{type(exc).__name__}: {exc}"
            logger.warning("External MCP server '%s' stopped: %s", self.command, exc)
        finally:
            self._session = None
            ready.set()  # unblock waiters; _ensure_session sees session=None

    async def _teardown_worker(self) -> None:
        worker, self._worker = self._worker, None
        self._session = None
        if worker is None:
            return
        if self._shutdown is not None:
            self._shutdown.set()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker.cancel()


def _extract_text(name: str, result: Any) -> str:
    content: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            content.append(text)
    if content:
        return "\n".join(content)
    if getattr(result, "isError", False):
        return f"External MCP tool '{name}' returned an error without details."
    return "External MCP tool returned no text."
