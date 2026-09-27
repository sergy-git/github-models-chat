"""copilot_agents — sync, config-driven agents on top of the GitHub Copilot SDK.

    from copilot_agents import load_agent_config, Agent

    cfg = load_agent_config("agents.json")
    rev = Agent(cfg, type="reviewer", name="rev1")
    print(rev.invoke("Review this.", attachments=["main.py"]))
    rev.close_session()
"""

from .agent import Agent
from .config import DEFAULT_TYPE, AgentsConfig, AgentType, load_agent_config
from .errors import (
    AgentBusy,
    AgentDeleted,
    AgentError,
    ConfigError,
    InvokeError,
    InvokeTimeout,
    RuntimeError_,
)
from .log import CHAT, get_logger, log_file_path, set_stream_sink
from .runtime import Runtime

__all__ = [
    "Agent",
    "AgentsConfig",
    "AgentType",
    "DEFAULT_TYPE",
    "load_agent_config",
    "AgentError",
    "ConfigError",
    "RuntimeError_",
    "InvokeError",
    "InvokeTimeout",
    "AgentBusy",
    "AgentDeleted",
    "Runtime",
    "CHAT",
    "get_logger",
    "log_file_path",
    "set_stream_sink",
]
