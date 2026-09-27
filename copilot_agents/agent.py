"""The Agent class: a named instance of a configured agent type.

Synchronous public API; every SDK call runs on the shared runtime loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from copilot.session import PermissionHandler
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantReasoningData,
    AssistantReasoningDeltaData,
    AssistantUsageData,
    PermissionRequestedData,
    SessionErrorData,
    SessionIdleData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
)

from .config import DEFAULT_TYPE, AgentsConfig, AgentType
from .errors import AgentBusy, AgentDeleted, AgentError, InvokeError, InvokeTimeout
from .log import get_logger, stream_sink
from .runtime import Runtime

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_ARGS_MAX = 300
_RESULT_MAX = 300


@dataclass
class _Turn:
    """Mutable per-turn state filled by the session event handler."""

    agent: str
    log: Any
    think: str = ""
    think_deltas: int = 0
    answer: str = ""
    answer_deltas: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    premium: float = 0.0
    model_calls: int = 0
    tool_names: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    aborted: bool = False

    def on_event(self, event) -> None:
        d = event.data
        sink = stream_sink()
        if isinstance(d, AssistantReasoningDeltaData):
            self.think += d.delta_content or ""
            self.think_deltas += 1
            sink.on_delta(self.agent, "think", self.think, self.think_deltas)
        elif isinstance(d, AssistantMessageDeltaData):
            self.answer += d.delta_content or ""
            self.answer_deltas += 1
            sink.on_delta(self.agent, "respond", self.answer, self.answer_deltas)
        elif isinstance(d, AssistantReasoningData):
            if d.content and d.content.strip():
                self.log.chat("think: %s", d.content.strip())
            self.think, self.think_deltas = "", 0
        elif isinstance(d, AssistantMessageData):
            # Final answer is logged by invoke() (with cost); interim ones here.
            reqs = getattr(d, "tool_requests", None)
            if reqs and d.content and d.content.strip():
                self.log.chat("think: (interim) %s", d.content.strip())
            self.answer, self.answer_deltas = "", 0
        elif isinstance(d, ToolExecutionStartData):
            self.tool_names[d.tool_call_id] = d.tool_name
            self.log.chat("tool: %s  args=%s", d.tool_name, _short(_dumps(d.arguments), _ARGS_MAX))
        elif isinstance(d, ToolExecutionCompleteData):
            name = self.tool_names.get(d.tool_call_id, d.tool_call_id)
            if d.success:
                out = d.result.content if d.result is not None else ""
                self.log.chat("tool: %s  ok  result=%s", name, _short(out, _RESULT_MAX))
            else:
                err = d.error.message if d.error is not None else "unknown error"
                self.log.chat("tool: %s  FAILED  %s", name, _short(err, _RESULT_MAX))
        elif isinstance(d, PermissionRequestedData):
            kind = type(d.permission_request).__name__.replace("PermissionRequest", "") or "?"
            self.log.chat("tool: permission approved  kind=%s", kind)
        elif isinstance(d, AssistantUsageData):
            self.model_calls += 1
            self.input_tokens += d.input_tokens or 0
            self.output_tokens += d.output_tokens or 0
            self.reasoning_tokens += d.reasoning_tokens or 0
            self.premium += d.cost or 0.0
        elif isinstance(d, SessionErrorData):
            self.error = f"{d.error_type}: {d.message}"
        elif isinstance(d, SessionIdleData):
            if d.aborted:
                self.aborted = True


def _dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _short(text: str, n: int) -> str:
    text = (text or "").replace("\n", "\\n")
    return text if len(text) <= n else text[: n - 1] + "…"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Agent:
    """A named agent instance built from a type in ``agents.json``."""

    _registry: dict[str, "Agent"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, config: AgentsConfig, type: str = DEFAULT_TYPE, name: str | None = None) -> None:
        self.type: AgentType = config.get_type(type)
        self.name = (name or type).strip()
        self.config = config
        self.log = get_logger(f"{self.name}/{self.type.name}")
        if not self.name or " " in self.name or "," in self.name:
            raise self._fail(AgentError, f"invalid agent name {self.name!r}")
        self._runtime = Runtime.get()
        self._session = None
        self._session_nano_aiu = 0.0
        self._busy = threading.Lock()
        self._deleted = False
        self._turn: _Turn | None = None
        self.total_credits = 0.0
        self.turns = 0
        with Agent._registry_lock:
            if self.name in Agent._registry:
                raise self._fail(AgentError, f"agent name {self.name!r} already exists")
            Agent._registry[self.name] = self
        self.log.info("created  %s", self.type.describe())

    # ------------------------------------------------------------- registry
    @classmethod
    def registry(cls) -> dict[str, "Agent"]:
        with cls._registry_lock:
            return dict(cls._registry)

    @classmethod
    def get(cls, name: str) -> "Agent":
        with cls._registry_lock:
            agent = cls._registry.get(name)
        if agent is None:
            raise AgentError(f"no agent named {name!r}")
        return agent

    # ------------------------------------------------------------- state
    @property
    def session_open(self) -> bool:
        return self._session is not None

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    @property
    def deleted(self) -> bool:
        return self._deleted

    def _fail(self, cls: type[AgentError], msg: str) -> AgentError:
        self.log.error(msg)
        return cls(msg)

    def _check_alive(self) -> None:
        if self._deleted:
            raise self._fail(AgentDeleted, "agent has been deleted")

    # ------------------------------------------------------------- public API
    def invoke(self, message: str, attachments: Sequence[str | Path] = (), timeout: float | None = 60.0) -> str:
        """Send ``message`` (plus optional files/images) and return the assistant's final text.

        Raises on any failure after logging it. ``timeout=None`` waits forever.
        """
        self._check_alive()
        if not isinstance(message, str) or not message.strip():
            raise self._fail(InvokeError, "invoke: message must be a non-empty string")
        if not self._busy.acquire(blocking=False):
            raise self._fail(AgentBusy, "invoke: agent is busy with a previous turn (use stop())")
        try:
            prompt, sdk_attachments, files_desc = self._prepare(message, attachments)
            self.log.chat("invoke: %s%s", message.strip(), f"\nfiles: {files_desc}" if files_desc else "")
            return self._runtime.run(self._ainvoke(prompt, sdk_attachments, timeout))
        finally:
            self._busy.release()

    def close_session(self) -> None:
        """Drop the conversation history; the agent (name, type, prompt) stays."""
        self._check_alive()
        if self.busy:
            raise self._fail(AgentBusy, "close_session: turn in progress; stop() first")
        self._runtime.run(self._aclose_session("closed by request"))

    def stop(self) -> None:
        """Abort the in-flight turn (if any). The session stays valid."""
        self._check_alive()
        session = self._session
        if session is None or not self.busy:
            self.log.info("stop: nothing running")
            return
        self.log.info("stop requested")
        self._runtime.run(session.abort(), timeout=15)

    def delete(self) -> None:
        """Close the session and remove the agent from the registry."""
        if self._deleted:
            return
        if self.busy:
            raise self._fail(AgentBusy, "delete: turn in progress; stop() first")
        if self._session is not None:
            self._runtime.run(self._aclose_session("closed on delete"))
        with Agent._registry_lock:
            Agent._registry.pop(self.name, None)
        self._deleted = True
        self.log.info("deleted  turns=%d credits=%.4f", self.turns, self.total_credits)

    # ------------------------------------------------------------- internals
    def _prepare(self, message: str, attachments: Iterable[str | Path]):
        parts = [message.strip()]
        sdk_attachments: list[dict[str, str]] = []
        descs: list[str] = []
        for raw in attachments:
            p = Path(raw).expanduser()
            if not p.is_file():
                raise self._fail(InvokeError, f"invoke: attachment not found: {p}")
            p = p.resolve()
            if p.suffix.lower() in IMAGE_EXTS:
                sdk_attachments.append({"type": "file", "path": str(p)})
                descs.append(f"{p.name} (image, {_human_size(p.stat().st_size)})")
            else:
                try:
                    content = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise self._fail(InvokeError, f"invoke: cannot read attachment as text: {p}: {exc}") from exc
                parts.append(f"--- file: {p.name} ---\n{content}\n--- end file: {p.name} ---")
                descs.append(f"{p.name} ({_human_size(len(content.encode('utf-8')))})")
        return "\n\n".join(parts), sdk_attachments, ", ".join(descs)

    async def _open_session(self):
        t = self.type
        try:
            session = await self._runtime.client.create_session(
                model=t.model,
                streaming=True,
                on_permission_request=PermissionHandler.approve_all,
                system_message={"mode": "replace", "content": t.system_prompt},
                available_tools=list(t.tools),
                memory={"enabled": False},
                skip_custom_instructions=True,
                enable_skills=False,
                infinite_sessions={"enabled": t.memory},
            )
        except Exception as exc:
            raise self._fail(InvokeError, f"session open failed: {exc}") from exc
        self._session = session
        self._session_nano_aiu = 0.0
        self.log.info("session opened  id=%s model=%s memory=%s tools=%s", session.session_id, t.model, t.memory, ",".join(t.tools) or "none")
        return session

    async def _aclose_session(self, reason: str) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            await session.disconnect()
            self.log.info("session closed  %s", reason)
        except Exception as exc:
            raise self._fail(InvokeError, f"session close failed: {exc}") from exc

    async def _ainvoke(self, prompt: str, attachments: list[dict[str, str]], timeout: float | None) -> str:
        session = self._session or await self._open_session()
        turn = _Turn(agent=self.name, log=self.log)
        self._turn = turn
        unsubscribe = session.on(turn.on_event)
        started = time.perf_counter()
        try:
            try:
                final = await session.send_and_wait(prompt, attachments=attachments or None, timeout=timeout)  # type: ignore[arg-type]
            except asyncio.TimeoutError:
                try:
                    await session.abort()
                finally:
                    await self._after_failure()
                raise self._fail(InvokeTimeout, f"turn timed out after {timeout}s; aborted")
            except AgentError:
                raise
            except Exception as exc:
                await self._after_failure()
                raise self._fail(InvokeError, f"turn failed: {exc}") from exc
        finally:
            unsubscribe()
            stream_sink().on_turn_end(self.name)
            self._turn = None
        elapsed = time.perf_counter() - started

        if turn.error:
            await self._after_failure()
            raise self._fail(InvokeError, f"session error: {turn.error}")
        if turn.aborted:
            await self._after_failure()
            raise self._fail(InvokeError, f"turn aborted after {elapsed:.1f}s")
        text = final.data.content if final is not None and isinstance(final.data, AssistantMessageData) else None
        if text is None:
            await self._after_failure()
            raise self._fail(InvokeError, "turn ended without an assistant message")

        credits = await self._turn_credits(session)
        self.turns += 1
        self.total_credits += credits
        self.log.chat(
            "respond: %s\ncost=%.4f credits  premium=%.2f  tokens in=%d out=%d reasoning=%d  calls=%d  time=%.1fs",
            text.strip(), credits, turn.premium, turn.input_tokens, turn.output_tokens,
            turn.reasoning_tokens, turn.model_calls, elapsed,
        )
        if not self.type.memory:
            await self._aclose_session("closed (no memory)")
        return text

    async def _turn_credits(self, session) -> float:
        try:
            metrics = await session.rpc.usage.get_metrics()
        except Exception as exc:
            self.log.info("usage metrics unavailable: %s", exc)
            return 0.0
        total = metrics.total_nano_aiu or 0.0
        delta = max(0.0, total - self._session_nano_aiu)
        self._session_nano_aiu = total
        return delta / 1e9

    async def _after_failure(self) -> None:
        """A failed turn never leaves a memory-less session dangling."""
        if not self.type.memory and self._session is not None:
            try:
                await self._aclose_session("closed after failure")
            except AgentError:
                pass
