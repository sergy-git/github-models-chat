"""Error hierarchy. Every error is logged before it is raised."""


class AgentError(Exception):
    """Base class for all copilot_agents errors."""


class ConfigError(AgentError):
    """agents.json is invalid, incomplete or references missing files/models."""


class RuntimeError_(AgentError):
    """The Copilot runtime could not be started or is unusable."""


class InvokeError(AgentError):
    """A turn failed (session error, no assistant message, abort)."""


class InvokeTimeout(InvokeError):
    """A turn exceeded its timeout; the run was aborted."""


class AgentBusy(AgentError):
    """invoke() called while a previous turn on the same agent is still running."""


class AgentDeleted(AgentError):
    """Operation on an agent that has been deleted."""
