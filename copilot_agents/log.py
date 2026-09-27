"""Logging for copilot_agents.

Three levels are used: CHAT (all traffic to/from the model), INFO (lifecycle)
and ERROR (failures). Every record carries an ``agent`` extra ("name/type" or
"runtime"). Output goes to a coloured terminal (rich) and always to a plain
text file inside the configured ``log_dir``.

Streaming progress (token-by-token growth of think/respond) is *not* a log
record; it is delivered to a pluggable :class:`StreamSink` so the terminal can
show it live while the file only gets the consolidated record per turn.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.live import Live
from rich.text import Text

CHAT = 25
logging.addLevelName(CHAT, "CHAT")

LOGGER_NAME = "copilot_agents"
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False

RUNTIME = "runtime"
_INDENT = " " * 40

_LEVEL_STYLE = {
    "CHAT": "cyan",
    "INFO": "green",
    "ERROR": "bold red",
    "WARNING": "yellow",
    "DEBUG": "dim",
}
_TAG_STYLE = {
    "invoke:": "bold white",
    "think:": "italic dim",
    "tool:": "yellow",
    "respond:": "bold green",
}


class AgentLogAdapter(logging.LoggerAdapter):
    """Injects the ``agent`` extra so formatters can print name/type."""

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("agent", self.extra["agent"])
        return msg, kwargs

    def chat(self, msg: str, *args, **kwargs) -> None:
        self.log(CHAT, msg, *args, **kwargs)


def get_logger(agent: str = RUNTIME) -> AgentLogAdapter:
    return AgentLogAdapter(logger, {"agent": agent})


class _PlainFormatter(logging.Formatter):
    """`ts LEVEL agent message` with continuation lines indented."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        agent = getattr(record, "agent", RUNTIME)
        head = f"{ts} {record.levelname:<5} {agent:<16} "
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        lines = msg.split("\n")
        return head + ("\n" + _INDENT).join(lines)


class RichConsoleHandler(logging.Handler):
    """Coloured terminal output. Shares its Console with the live stream sink."""

    def __init__(self, console: Console | None = None) -> None:
        super().__init__()
        self.console = console or Console(stderr=True, highlight=False)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = format_rich(record)
            with self._lock:
                self.console.print(text, soft_wrap=True)
        except Exception:
            self.handleError(record)


def format_rich(record: logging.LogRecord) -> Text:
    ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
    agent = getattr(record, "agent", RUNTIME)
    level = record.levelname
    text = Text()
    text.append(ts + " ", style="dim")
    text.append(f"{level:<5} ", style=_LEVEL_STYLE.get(level, ""))
    text.append(f"{agent:<16} ", style="magenta")
    msg = record.getMessage()
    if record.exc_info:
        msg += "\n" + logging.Formatter().formatException(record.exc_info)
    first, _, rest = msg.partition("\n")
    tag, _, body = first.partition(" ")
    if level == "CHAT" and tag in _TAG_STYLE:
        text.append(tag + " ", style=_TAG_STYLE[tag])
        text.append(body)
    else:
        text.append(first, style="bold red" if level == "ERROR" else "")
    if rest:
        for line in rest.split("\n"):
            text.append("\n" + " " * 36 + line)
    return text


class StreamSink(Protocol):
    """Receives live streaming progress for an in-flight turn."""

    def on_delta(self, agent: str, kind: str, text: str, tokens: int) -> None: ...

    def on_turn_end(self, agent: str) -> None: ...


class NullSink:
    def on_delta(self, agent: str, kind: str, text: str, tokens: int) -> None:
        pass

    def on_turn_end(self, agent: str) -> None:
        pass


class RichLiveSink:
    """In-place growing think/respond line in a plain terminal (library mode)."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live: Live | None = None
        self._lock = threading.Lock()

    def on_delta(self, agent: str, kind: str, text: str, tokens: int) -> None:
        with self._lock:
            render = Text()
            render.append(f"{agent:<16} ", style="magenta")
            render.append(kind + ": ", style=_TAG_STYLE.get(kind + ":", ""))
            tail = text[-400:].replace("\n", " ")
            render.append(("…" if len(text) > 400 else "") + tail)
            render.append(f"  tokens={tokens}", style="dim")
            if self._live is None:
                self._live = Live(render, console=self._console, transient=True, refresh_per_second=12)
                self._live.start()
            else:
                self._live.update(render)

    def on_turn_end(self, agent: str) -> None:
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None


class _SdkForwarder(logging.Handler):
    """Forward the SDK's own warnings/errors into our log as ERROR records."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
        logger.error("sdk %s: %s", record.name, msg, extra={"agent": "sdk"})


_sdk_logger = logging.getLogger("copilot")
_sdk_logger.addHandler(_SdkForwarder(level=logging.ERROR))
_sdk_logger.propagate = False

_sink: StreamSink = NullSink()
_console_handler: RichConsoleHandler | None = None
_file_handler: logging.FileHandler | None = None
_file_path: Path | None = None


def set_stream_sink(sink: StreamSink) -> None:
    global _sink
    _sink = sink


def stream_sink() -> StreamSink:
    return _sink


def enable_console(enabled: bool = True) -> None:
    """Attach (or detach) the coloured terminal handler and live sink."""
    global _console_handler, _sink
    if enabled and _console_handler is None:
        _console_handler = RichConsoleHandler()
        logger.addHandler(_console_handler)
        if sys.stderr.isatty():
            _sink = RichLiveSink(_console_handler.console)
    elif not enabled and _console_handler is not None:
        logger.removeHandler(_console_handler)
        _console_handler = None
        if isinstance(_sink, RichLiveSink):
            _sink = NullSink()


def enable_file(log_dir: Path) -> Path:
    """Create the per-run plain-text log file inside ``log_dir``. Idempotent."""
    global _file_handler, _file_path
    if _file_handler is not None:
        return _file_path  # type: ignore[return-value]
    log_dir.mkdir(parents=True, exist_ok=True)
    _file_path = log_dir / f"agents-{datetime.now():%Y%m%d_%H%M%S}.log"
    # FileHandler flushes after every record; no fsync games.
    _file_handler = logging.FileHandler(_file_path, encoding="utf-8")
    _file_handler.setFormatter(_PlainFormatter())
    logger.addHandler(_file_handler)
    return _file_path


def log_file_path() -> Path | None:
    return _file_path
