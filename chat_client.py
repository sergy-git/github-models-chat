#!/usr/bin/env python
"""Textual CLI for manual testing of copilot_agents.

    python chat_client.py [agents.json]

Upper pane: live log (same records as the log file, coloured).
Lower pane: command console. Every input is a /command; see /help.
"""

from __future__ import annotations

import glob as globmod
import logging
import os
import re
import sys
import threading
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import RichLog, Static, TextArea

from copilot_agents import (
    Agent,
    AgentBusy,
    AgentError,
    AgentsConfig,
    Runtime,
    load_agent_config,
    set_stream_sink,
)
from copilot_agents import log as aglog

COMMANDS = {
    "create": "/create <name> [type = <type>]           create an agent (type defaults to 'default')",
    "invoke": "/invoke <name>, message = <text>[, files = {a, b}]   send a message",
    "stop": "/stop <name>                               abort the running turn",
    "close": "/close <name>                              close session, keep agent",
    "delete": "/delete <name>                             close session and remove agent",
    "list": "/list agents | models | types",
    "info": "/info <name>",
    "log": "/log chat | info | error                    log pane filter: chat = CHAT+ERROR, info = everything (file is always full)",
    "clear": "/clear                                     clear the console pane",
    "help": "/help",
    "exit": "/exit",
}
KV_KEYS = ("message", "files", "type")
MAX_INPUT_ROWS = 10


# ----------------------------------------------------------------- parsing
class ParseError(Exception):
    pass


def parse_command(raw: str) -> tuple[str, list[str], dict[str, str]]:
    """``/cmd pos... key = value, key = value`` -> (cmd, positionals, kv)."""
    raw = raw.strip()
    if not raw.startswith("/"):
        raise ParseError("commands start with '/'; type /help")
    head, _, rest = raw[1:].partition(" ")
    cmd = head.strip().lower()
    if cmd not in COMMANDS:
        raise ParseError(f"unknown command /{cmd}; type /help")
    key_re = re.compile(r"(?:^|[,\s])\s*(" + "|".join(KV_KEYS) + r")\s*=", re.S)
    matches = list(key_re.finditer(rest))
    pos_text = rest[: matches[0].start()] if matches else rest
    positionals = [p for p in re.split(r"[,\s]+", pos_text.strip()) if p]
    kv: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        value = rest[m.end():end].strip()
        if m.group(1) in kv:
            raise ParseError(f"duplicate key {m.group(1)!r}")
        kv[m.group(1)] = value
    return cmd, positionals, kv


def parse_files(value: str) -> list[str]:
    v = value.strip()
    if v.startswith("{") and v.endswith("}"):
        v = v[1:-1]
    return [p.strip().strip("\"'") for p in v.split(",") if p.strip()]


# ----------------------------------------------------------------- widgets
class CommandInput(TextArea):
    """Bottom input: Enter submits, Shift/Alt+Enter inserts a newline, Tab completes."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Complete(Message):
        pass

    class History(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.Complete())
            return
        if key in ("up", "down") and self.document.line_count == 1:
            event.stop()
            event.prevent_default()
            self.post_message(self.History(-1 if key == "up" else 1))
            return
        await super()._on_key(event)

    def set_text(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)


class PaneLogHandler(logging.Handler):
    """Routes copilot_agents log records into the log pane from any thread."""

    def __init__(self, app: "ChatApp") -> None:
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.app.from_any_thread(self.app.write_log, aglog.format_rich(record))
        except Exception:
            self.handleError(record)


class PaneStreamSink:
    """Live in-place think/respond growth shown in the strip under the log."""

    def __init__(self, app: "ChatApp") -> None:
        self.app = app
        self._lines: dict[str, Text] = {}
        self._lock = threading.Lock()

    def on_delta(self, agent: str, kind: str, text: str, tokens: int) -> None:
        t = Text()
        t.append(f"{agent:<16} ", style="magenta")
        t.append(kind + ": ", style="italic dim" if kind == "think" else "bold green")
        tail = text[-300:].replace("\n", " ")
        t.append(("…" if len(text) > 300 else "") + tail)
        t.append(f"  tokens≈{tokens}", style="dim")
        with self._lock:
            self._lines[agent] = t
            render = Text("\n").join(self._lines.values())
        self.app.from_any_thread(self.app.update_live, render)

    def on_turn_end(self, agent: str) -> None:
        with self._lock:
            self._lines.pop(agent, None)
            render = Text("\n").join(self._lines.values())
        self.app.from_any_thread(self.app.update_live, render)


# ----------------------------------------------------------------- app
class ChatApp(App):
    CSS = """
    #log { height: 1fr; border: round $primary; padding: 0 1; }
    #live { height: auto; max-height: 4; padding: 0 1; color: $text-muted; }
    #console { height: 12; border: round $secondary; padding: 0 1; }
    #input { height: auto; max-height: 12; border: tall $accent; }
    """
    BINDINGS = [("ctrl+c", "stop_all", "Stop running turn")]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.cfg: AgentsConfig | None = None
        self.invoking: str | None = None
        self.history: list[str] = []
        self.history_pos = 0
        self.pane_handler = PaneLogHandler(self)
        self._app_thread = threading.current_thread()

    # ---- composition
    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="log", wrap=True, highlight=False, markup=False)
            yield Static("", id="live")
            yield RichLog(id="console", wrap=True, highlight=False, markup=True)
            yield CommandInput(id="input", soft_wrap=True, tab_behavior="focus")

    def on_mount(self) -> None:
        self.title = "copilot_agents"
        aglog.logger.addHandler(self.pane_handler)
        set_stream_sink(PaneStreamSink(self))
        self.query_one("#input", CommandInput).focus()
        self.console_print(f"[bold]copilot_agents CLI[/]  config: {self.config_path}   /help for commands")
        self.run_worker(self._load_config, thread=True, exclusive=False, name="load")

    def _load_config(self) -> None:
        try:
            cfg = load_agent_config(self.config_path, console=False)
        except AgentError as exc:
            self.from_any_thread(self.console_print, f"[red]config failed:[/] {exc}   (fix agents.json, /exit and restart)")
            return
        self.cfg = cfg
        self.from_any_thread(self.console_print, f"[green]ready[/]  types: {', '.join(cfg.type_names)}   log file: {cfg.log_file}")

    # ---- thread helpers
    def from_any_thread(self, fn, *args) -> None:
        if threading.current_thread() is self._app_thread:
            fn(*args)
        else:
            self.call_from_thread(fn, *args)

    def write_log(self, text: Text) -> None:
        self.query_one("#log", RichLog).write(text)

    def update_live(self, text: Text) -> None:
        self.query_one("#live", Static).update(text)

    def console_print(self, renderable) -> None:
        self.query_one("#console", RichLog).write(renderable)

    # ---- input events
    def on_command_input_submitted(self, msg: CommandInput.Submitted) -> None:
        text = msg.text.strip()
        inp = self.query_one("#input", CommandInput)
        inp.set_text("")
        if not text:
            return
        self.history.append(text)
        self.history_pos = len(self.history)
        self.console_print(Text("> " + text.replace("\n", "\n  "), style="bold"))
        try:
            cmd, pos, kv = parse_command(text)
        except ParseError as exc:
            self.console_print(f"[red]{exc}[/]")
            return
        handler = getattr(self, f"cmd_{cmd}")
        try:
            handler(pos, kv)
        except (ParseError, AgentError) as exc:
            self.console_print(f"[red]{exc}[/]")

    def on_command_input_history(self, msg: CommandInput.History) -> None:
        if not self.history:
            return
        self.history_pos = max(0, min(len(self.history), self.history_pos + msg.direction))
        text = self.history[self.history_pos] if self.history_pos < len(self.history) else ""
        self.query_one("#input", CommandInput).set_text(text)

    def on_command_input_complete(self, _: CommandInput.Complete) -> None:
        inp = self.query_one("#input", CommandInput)
        text = inp.text
        prefix, candidates = self._completions(text)
        if not candidates:
            return
        if len(candidates) == 1:
            inp.set_text(text[: len(text) - len(prefix)] + candidates[0] + " ")
            return
        common = os.path.commonprefix(candidates)
        if len(common) > len(prefix):
            inp.set_text(text[: len(text) - len(prefix)] + common)
        self.console_print("  ".join(candidates))

    def _completions(self, text: str) -> tuple[str, list[str]]:
        """Return (partial token being completed, matching candidates)."""
        if not text.startswith("/"):
            return text, []
        if " " not in text:
            part = text[1:]
            return part, [c for c in COMMANDS if c.startswith(part)]
        cmd = text[1:].split(" ", 1)[0].lower()
        # inside files = { ... }
        m = re.search(r"files\s*=\s*\{([^}]*)$", text, re.S)
        if m:
            part = m.group(1).split(",")[-1].strip()
            hits = sorted(globmod.glob(part + "*"))
            hits = [h + "/" if os.path.isdir(h) else h for h in hits]
            return part, hits
        m = re.search(r"type\s*=\s*(\S*)$", text)
        if m and self.cfg:
            part = m.group(1)
            return part, [t for t in self.cfg.type_names if t.startswith(part)]
        tokens = re.split(r"[\s,]+", text)
        part = tokens[-1] if not text.endswith((" ", ",")) else ""
        if cmd == "list":
            return part, [x for x in ("agents", "models", "types") if x.startswith(part)]
        if cmd == "log":
            return part, [x for x in ("chat", "info", "error") if x.startswith(part)]
        if cmd in ("invoke", "stop", "close", "delete", "info") and len(tokens) <= 2 + (part == ""):
            return part, [n for n in Agent.registry() if n.startswith(part)]
        if cmd == "invoke" and "message" not in text:
            return part, ["message = "] if "message = ".startswith(part) else []
        return part, []

    # ---- commands
    def _need_cfg(self) -> AgentsConfig:
        if self.cfg is None:
            raise ParseError("config not loaded")
        return self.cfg

    def _agent(self, pos: list[str]) -> Agent:
        if not pos:
            raise ParseError("agent name required")
        return Agent.get(pos[0])

    def cmd_help(self, pos, kv) -> None:
        for line in COMMANDS.values():
            self.console_print("  " + line)
        self.console_print("  Enter = send, Shift+Enter / Alt+Enter = newline, Tab = complete, Up/Down = history, Ctrl+C = stop turn")

    def cmd_create(self, pos, kv) -> None:
        cfg = self._need_cfg()
        if not pos:
            raise ParseError("usage: /create <name> [type = <type>]")
        type_name = kv.get("type") or (pos[1] if len(pos) > 1 else "default")
        agent = Agent(cfg, type_name, name=pos[0])
        self.console_print(f"created [bold]{agent.name}[/] ({agent.type.name})")

    def cmd_invoke(self, pos, kv) -> None:
        self._need_cfg()
        agent = self._agent(pos)
        message = kv.get("message")
        if not message:
            raise ParseError("usage: /invoke <name>, message = <text>[, files = {a, b}]")
        files = parse_files(kv["files"]) if "files" in kv else []
        if self.invoking is not None:
            self.console_print(f"[yellow]wait until {self.invoking} finished[/] (or /stop {self.invoking})")
            return
        self.invoking = agent.name

        def run() -> None:
            try:
                agent.invoke(message, attachments=files, timeout=None)
                self.from_any_thread(self.console_print, f"[green]{agent.name} finished[/]")
            except AgentError as exc:
                self.from_any_thread(self.console_print, f"[red]{agent.name} failed:[/] {exc}")
            finally:
                self.invoking = None

        self.run_worker(run, thread=True, exclusive=False, name=f"invoke-{agent.name}")

    def cmd_stop(self, pos, kv) -> None:
        agent = self._agent(pos)
        self.run_worker(lambda: self._safe(agent.stop), thread=True, exclusive=False, name="stop")

    def cmd_close(self, pos, kv) -> None:
        agent = self._agent(pos)
        self.run_worker(lambda: self._safe(agent.close_session), thread=True, exclusive=False, name="close")

    def cmd_delete(self, pos, kv) -> None:
        agent = self._agent(pos)
        self.run_worker(lambda: self._safe(agent.delete), thread=True, exclusive=False, name="delete")

    def _safe(self, fn) -> None:
        try:
            fn()
        except AgentError as exc:
            self.from_any_thread(self.console_print, f"[red]{exc}[/]")

    def cmd_list(self, pos, kv) -> None:
        what = pos[0] if pos else "agents"
        if what == "agents":
            t = Table("name", "type", "model", "session", "busy", "turns", "credits", box=None, pad_edge=False)
            for a in Agent.registry().values():
                t.add_row(a.name, a.type.name, a.type.model, "open" if a.session_open else "-", "yes" if a.busy else "-", str(a.turns), f"{a.total_credits:.4f}")
            self.console_print(t if t.row_count else "no agents; /create <name> [type = <type>]")
        elif what == "models":
            t = Table("id", "name", "multiplier", box=None, pad_edge=False)
            for m in Runtime.get().models.values():
                mult = m.billing.multiplier if m.billing and m.billing.multiplier is not None else "-"
                t.add_row(m.id, m.name, str(mult))
            self.console_print(t)
        elif what == "types":
            cfg = self._need_cfg()
            t = Table("type", "model", "memory", "system_prompt", "tools", box=None, pad_edge=False)
            for name in cfg.type_names:
                ty = cfg.get_type(name)
                t.add_row(name, ty.model, str(ty.memory), ty.system_prompt_path.name if ty.system_prompt_path else "-", ",".join(ty.tools) or "none")
            self.console_print(t)
        else:
            raise ParseError("usage: /list agents | models | types")

    def cmd_info(self, pos, kv) -> None:
        a = self._agent(pos)
        self.console_print(
            f"[bold]{a.name}[/]  type={a.type.name}  {a.type.describe()}\n"
            f"  session={'open' if a.session_open else 'closed'}  busy={a.busy}  turns={a.turns}  credits={a.total_credits:.4f}"
        )

    def cmd_log(self, pos, kv) -> None:
        level = (pos[0] if pos else "").lower()
        mapping = {"chat": aglog.CHAT, "info": logging.INFO, "error": logging.ERROR}
        if level not in mapping:
            raise ParseError("usage: /log chat | info | error")
        self.pane_handler.setLevel(mapping[level])
        self.console_print(f"log pane level: {level}")

    def cmd_clear(self, pos, kv) -> None:
        self.query_one("#console", RichLog).clear()

    def cmd_exit(self, pos, kv) -> None:
        if self.invoking is not None:
            raise ParseError(f"{self.invoking} is still running; /stop {self.invoking} first")
        self.console_print("closing sessions…")

        def shutdown() -> None:
            for a in list(Agent.registry().values()):
                self._safe(a.delete)
            Runtime.get().shutdown()
            self.from_any_thread(self.exit)

        self.run_worker(shutdown, thread=True, exclusive=False, name="exit")

    def action_stop_all(self) -> None:
        if self.invoking is None:
            self.console_print("nothing running (use /exit to quit)")
            return
        self.cmd_stop([self.invoking], {})


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "agents.json")
    ChatApp(path).run()


if __name__ == "__main__":
    main()
