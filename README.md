# copilot_agents

A small Python class, `Agent`, for using GitHub Copilot models from your own code. You describe
agent *types* once in `agents.json` (model, system prompt, tools, memory), then create named
agents and call `agent.invoke("...")` like an ordinary function. It is built on the official
[GitHub Copilot SDK](https://github.com/github/copilot-sdk) (`github-copilot-sdk`) and hides the
async runtime, sessions, streaming and permissions behind a synchronous API.

```python
from copilot_agents import load_agent_config, Agent

cfg = load_agent_config("agents.json")
rev = Agent(cfg, type="reviewer", name="rev1")
print(rev.invoke("Review this.", attachments=["main.py", "diagram.png"], timeout=60))
```

## Requirements

- Python 3.11+ (3.12 used here)
- A GitHub Copilot subscription and a logged-in Copilot CLI (`copilot login`).
  No tokens or `.env` — the SDK uses the CLI's stored login.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m copilot download-runtime   # optional; done lazily otherwise
```

## The API

### `load_agent_config(path="agents.json") -> AgentsConfig`

Reads and validates the config, opens the log file, starts the shared Copilot runtime (one per
process) and checks every model against the live model list. Any problem is logged and raised
as `ConfigError` here, not later at call time. Nothing is silently defaulted.

### `Agent(config, type="default", name=None)`

Creates a named agent of a configured type. Names are unique per process; the type's model,
system prompt, tools and memory mode are fixed at creation. Different agents can run in
parallel (one runtime, one session each).

| Method | Purpose |
| --- | --- |
| `invoke(message, attachments=(), timeout=60.0)` | Send a message (plus optional files/images) and return the final assistant text. `timeout=None` waits forever. |
| `close_session()` | Drop the conversation history; the agent (name, type, prompt) stays usable. |
| `stop()` | Abort the in-flight turn, typically called from another thread. The session stays valid. |
| `delete()` | Close the session and remove the agent from the registry. |
| `Agent.get(name)` / `Agent.registry()` | Look up a live agent by name / snapshot of all agents. |

Properties: `session_open`, `busy`, `deleted`, `turns`, `total_credits` (AI credits spent so far).

#### `invoke()` details

- Returns the final assistant text only; reasoning, tool calls and cost go to the log.
- Text attachments are inlined into the prompt as `--- file: NAME ---` blocks; images
  (`.png .jpg .jpeg .gif .webp .bmp`) are sent as SDK image attachments.
- Memory: with `memory: true` the session (history) stays open across `invoke()` calls until
  `close_session()`; with `memory: false` every `invoke()` gets a fresh session that is closed
  right after the answer.
- Tools: built-in Copilot CLI tools listed in the type (e.g. `view`, `grep`, `glob`) are
  available to the model and auto-approved. Every call is logged.
- Calling `invoke()` on an agent that is already running a turn raises `AgentBusy`.

#### Errors

All errors derive from `AgentError` and are logged before being raised.

| Error | Meaning |
| --- | --- |
| `ConfigError` | invalid or incomplete `agents.json`, missing files, unknown model |
| `InvokeError` | turn failed (session error, abort, no assistant message, bad attachment) |
| `InvokeTimeout` | subclass of `InvokeError`; timeout hit, the run was aborted |
| `AgentBusy` | `invoke()` / `close_session()` / `delete()` during a running turn |
| `AgentDeleted` | operation on a deleted agent |
| `RuntimeError_` | the Copilot runtime could not be started |

### Configuration — `agents.json`

```json
{
  "log_dir": "logs",
  "defaults": { "model": "gpt-5-mini", "memory": false, "system_prompt": null, "tools": [] },
  "types": {
    "assistant": { "system_prompt": "prompts/assistant.md", "memory": true },
    "explorer":  { "system_prompt": "prompts/explorer.md", "memory": true, "tools": ["view", "grep", "glob"] }
  }
}
```

- `log_dir` — required; a plain-text log file per run is always written there.
- `defaults` — `model`, `memory`, `system_prompt`, `tools` are required. Type `default` is
  `defaults` itself.
- `types` — each type overrides at least one key (a type overriding nothing is an error).
- `model` — validated against the live model list.
- `memory` — see `invoke()` details above.
- `system_prompt` — path relative to `agents.json`; used in *replace* mode (none of Copilot's
  built-in instructions). `null` = empty system prompt.
- `tools` — built-in CLI tool names; `[]` = no tools.
- `reasoning_effort`, `context_tier` — optional everywhere; checked against what the model
  supports. `context_tier` is `null`, `"default"` or `"long_context"`.

### Log

Three levels: `CHAT` (`invoke:` / `think:` / `tool:` / `respond:`, full text, files, and the
turn's cost in AI credits on the `respond:` row), `INFO` (lifecycle, quota, models) and `ERROR`.
Coloured on the terminal with live token growth; plain text in `log_dir/agents-<ts>.log`.
Use `get_logger()` / `log_file_path()` to hook into it from your code.

## Test terminal (`chat_client.py`) — a debugging tool, not a chat app

A secondary tool for when you want to define an agent, run a few manual chats and see exactly
what happens: reasoning, tool calls, token counts, cost, errors. It drives the same `Agent`
class as the library and is meant for inspecting behaviour, not for comfortable day-to-day
chatting.

```bash
./chat.sh [agents.json]          # or: .venv/bin/python chat_client.py [agents.json]
```

Upper pane: live log. Lower pane: command console. Enter sends, Shift+Enter / Alt+Enter
inserts a newline, Tab completes, Up/Down history, Ctrl+C stops the running turn.

```
/create <name> [type = <type>]
/invoke <name>, message = <text>[, files = {a.py, img.png}]
/stop <name>      /close <name>      /delete <name>
/list agents | models | types        /info <name>
/log chat | info | error             /clear     /help      /exit
```

One `/invoke` at a time; a second one is refused with “wait until … finished”.
