"""Shared Copilot runtime: one CopilotClient on a background asyncio loop.

All ``Agent`` instances share this runtime. Public API is synchronous; the
coroutine plumbing is hidden behind :meth:`Runtime.run`.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, TypeVar

from copilot import CopilotClient
from copilot.rpc import AccountGetQuotaRequest

from .errors import AgentError, RuntimeError_
from .log import get_logger

T = TypeVar("T")
_log = get_logger()


class Runtime:
    _instance: "Runtime | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="copilot-runtime", daemon=True)
        self._client: CopilotClient | None = None
        self._models: dict[str, Any] = {}
        self._login: str | None = None
        self._start_lock = threading.Lock()

    # ----------------------------------------------------------------- lifecycle
    @classmethod
    def get(cls) -> "Runtime":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = Runtime()
            return cls._instance

    @property
    def started(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        """Start the CLI runtime, verify auth, cache models, log quota. Idempotent."""
        with self._start_lock:
            if self._client is not None:
                return
            if not self._thread.is_alive():
                self._thread.start()
            try:
                self._client = self.run(self._async_start())
            except AgentError:
                raise
            except Exception as exc:
                _log.error("runtime failed to start: %s", exc)
                raise RuntimeError_(f"runtime failed to start: {exc}") from exc
            # The SDK writes over stdio via the default executor, which the
            # interpreter shuts down before atexit hooks; threading's hook runs earlier.
            register = getattr(threading, "_register_atexit", atexit.register)
            register(self.shutdown)

    async def _async_start(self) -> CopilotClient:
        client = CopilotClient(log_level="error")
        await client.start()
        auth = await client.get_auth_status()
        if not auth.isAuthenticated:
            await client.stop()
            msg = f"Copilot CLI is not authenticated ({auth.statusMessage or 'no login'}); run `copilot login`"
            _log.error(msg)
            raise RuntimeError_(msg)
        self._login = auth.login
        _log.info("runtime started  user=%s auth=%s", auth.login, auth.authType)

        models = await client.list_models()
        self._models = {m.id: m for m in models}
        _log.info("models available: %d  [%s]", len(models), ", ".join(self._models))

        try:
            quota = await client.rpc.account.get_quota(AccountGetQuotaRequest())
            prem = quota.quota_snapshots.get("premium_interactions")
            if prem is not None:
                _log.info(
                    "quota premium_interactions  used=%d/%d  remaining=%.1f%%  resets=%s",
                    prem.used_requests, prem.entitlement_requests, prem.remaining_percentage, prem.reset_date,
                )
        except Exception as exc:  # quota is informational only
            _log.info("quota unavailable: %s", exc)
        return client

    def shutdown(self) -> None:
        """Stop the CLI runtime and the loop thread. Safe to call more than once."""
        client, self._client = self._client, None
        if client is not None:
            try:
                self.run(client.stop(), timeout=15)
                _log.info("runtime stopped")
            except Exception as exc:
                _log.error("runtime stop failed: %s", exc)
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    # ----------------------------------------------------------------- access
    @property
    def client(self) -> CopilotClient:
        if self._client is None:
            raise RuntimeError_("runtime not started; call load_agent_config() first")
        return self._client

    @property
    def login(self) -> str | None:
        return self._login

    @property
    def models(self) -> dict[str, Any]:
        return self._models

    def run(self, coro: Awaitable[T], timeout: float | None = None) -> T:
        """Run a coroutine on the runtime loop from any thread and block for the result."""
        if threading.current_thread() is self._thread:
            raise RuntimeError_("Runtime.run() called from the runtime loop thread")
        fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(timeout)
        except BaseException:
            fut.cancel()
            raise

    def submit(self, coro: Awaitable[T]) -> Future[T]:
        """Schedule a coroutine on the runtime loop without waiting."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
