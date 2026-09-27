"""agents.json loading with strict, eager validation.

Schema::

    {
      "log_dir": "logs",
      "defaults": {"model": "...", "memory": false, "system_prompt": null|"path.md", "tools": []},
      "types": {"<type>": {<any subset of the four keys, at least one>}}
    }

Every problem raises :class:`ConfigError` (after logging). Nothing is
silently defaulted. Loading also starts the shared runtime so that model
names can be checked against the live model list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .log import enable_console, enable_file, get_logger
from .runtime import Runtime

_log = get_logger()

TYPE_KEYS = ("model", "memory", "system_prompt", "tools")
TOP_KEYS = ("log_dir", "defaults", "types")
DEFAULT_TYPE = "default"


@dataclass(frozen=True)
class AgentType:
    name: str
    model: str
    memory: bool
    system_prompt_path: Path | None
    system_prompt: str
    tools: tuple[str, ...]

    def describe(self) -> str:
        sp = str(self.system_prompt_path) if self.system_prompt_path else "-"
        tools = ",".join(self.tools) if self.tools else "none"
        return f"model={self.model} memory={self.memory} system_prompt={sp} tools={tools}"


@dataclass(frozen=True)
class AgentsConfig:
    path: Path
    log_dir: Path
    log_file: Path
    defaults: AgentType
    types: dict[str, AgentType]

    def get_type(self, name: str) -> AgentType:
        if name == DEFAULT_TYPE:
            return self.defaults
        try:
            return self.types[name]
        except KeyError:
            raise _fail(f"unknown agent type {name!r}; known: {', '.join(self.type_names)}") from None

    @property
    def type_names(self) -> list[str]:
        return [DEFAULT_TYPE, *self.types]


def _fail(msg: str) -> ConfigError:
    _log.error("config error: %s", msg)
    return ConfigError(msg)


def _check_keys(obj: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise _fail(f"{where}: unknown key(s) {unknown}; allowed: {list(allowed)}")


def _parse_type_fields(raw: dict, where: str, base_dir: Path) -> dict[str, Any]:
    """Validate the subset of type keys present in ``raw`` and return typed values."""
    if not isinstance(raw, dict):
        raise _fail(f"{where}: must be an object")
    _check_keys(raw, TYPE_KEYS, where)
    out: dict[str, Any] = {}
    if "model" in raw:
        if not isinstance(raw["model"], str) or not raw["model"].strip():
            raise _fail(f"{where}.model: must be a non-empty string")
        out["model"] = raw["model"].strip()
    if "memory" in raw:
        if not isinstance(raw["memory"], bool):
            raise _fail(f"{where}.memory: must be true or false")
        out["memory"] = raw["memory"]
    if "system_prompt" in raw:
        sp = raw["system_prompt"]
        if sp is None:
            out["system_prompt_path"], out["system_prompt"] = None, ""
        elif isinstance(sp, str) and sp.strip():
            p = (base_dir / sp).resolve()
            if not p.is_file():
                raise _fail(f"{where}.system_prompt: file not found: {p}")
            try:
                out["system_prompt"] = p.read_text(encoding="utf-8")
            except OSError as exc:
                raise _fail(f"{where}.system_prompt: cannot read {p}: {exc}") from exc
            out["system_prompt_path"] = p
        else:
            raise _fail(f"{where}.system_prompt: must be null or a non-empty path string")
    if "tools" in raw:
        t = raw["tools"]
        if t == "none":
            t = []
        if not isinstance(t, list) or not all(isinstance(x, str) and x.strip() for x in t):
            raise _fail(f"{where}.tools: must be a list of tool-name strings (or [] / \"none\")")
        out["tools"] = tuple(dict.fromkeys(x.strip() for x in t))
    return out


def load_agent_config(path: str | Path = "agents.json", *, console: bool = True) -> AgentsConfig:
    """Load, validate and activate ``agents.json``.

    Side effects (by design): opens the log file in ``log_dir``, attaches the
    coloured console handler (unless ``console=False``), starts the shared
    runtime and validates all models against the live model list.
    """
    enable_console(console)
    path = Path(path).resolve()
    if not path.is_file():
        raise _fail(f"config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _fail(f"{path.name}: top level must be an object")
    _check_keys(data, TOP_KEYS, path.name)
    for key in TOP_KEYS:
        if key not in data:
            raise _fail(f"{path.name}: missing required key {key!r}")

    base = path.parent
    if not isinstance(data["log_dir"], str) or not data["log_dir"].strip():
        raise _fail("log_dir: must be a non-empty path string")
    log_dir = (base / data["log_dir"]).resolve()
    try:
        log_file = enable_file(log_dir)
    except OSError as exc:
        raise _fail(f"log_dir: cannot create/write {log_dir}: {exc}") from exc
    _log.info("log file %s", log_file)

    d = _parse_type_fields(data["defaults"], "defaults", base)
    missing = [k for k in TYPE_KEYS if k not in data["defaults"]]
    if missing:
        raise _fail(f"defaults: missing required key(s) {missing}; all of {list(TYPE_KEYS)} are required")
    defaults = AgentType(name=DEFAULT_TYPE, **d)

    if not isinstance(data["types"], dict):
        raise _fail("types: must be an object mapping type name -> overrides")
    types: dict[str, AgentType] = {}
    for name, raw in data["types"].items():
        if not isinstance(name, str) or not name.strip() or " " in name or "," in name:
            raise _fail(f"types: invalid type name {name!r}")
        if name == DEFAULT_TYPE:
            raise _fail(f"types: {DEFAULT_TYPE!r} is reserved (it is `defaults` itself)")
        overrides = _parse_type_fields(raw, f"types.{name}", base)
        if not overrides:
            raise _fail(f"types.{name}: overrides nothing; a type must differ from defaults (use type 'default' instead)")
        types[name] = replace(defaults, name=name, **overrides)

    runtime = Runtime.get()
    runtime.start()
    known = runtime.models
    for t in (defaults, *types.values()):
        if t.model not in known:
            raise _fail(f"{'defaults' if t.name == DEFAULT_TYPE else 'types.' + t.name}.model: {t.model!r} is not available; choose one of: {', '.join(known)}")

    cfg = AgentsConfig(path=path, log_dir=log_dir, log_file=log_file, defaults=defaults, types=types)
    _log.info("config %s OK  types=%d  [%s]", path.name, len(cfg.type_names), ", ".join(cfg.type_names))
    for t in (defaults, *types.values()):
        _log.info("type %-12s %s", t.name, t.describe())
    return cfg
