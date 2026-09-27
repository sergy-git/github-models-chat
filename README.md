# copilot_agents

Sync, config-driven agents on top of the official [GitHub Copilot SDK](https://github.com/github/copilot-sdk)
(`github-copilot-sdk`), plus a Textual terminal client for manual testing.

## Requirements

- Python 3.11+ (3.12 used here)
- A GitHub Copilot subscription and a logged-in Copilot CLI (`copilot login`).
  No tokens or `.env` — the SDK uses the CLI's stored login.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m copilot download-runtime   # optional; done lazily otherwise
```

## Configuration — `agents.json`

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
- `defaults` — all four keys required. Type `default` is `defaults` itself.
- `types` — each type overrides at least one key (a type overriding nothing is an error).
- `model` — validated against the live model list (`/list models`).
- `memory` — `true`: the session (conversation history) stays open until `close_session()`;
  `false`: a fresh session per `invoke()`, closed right after the answer.
- `system_prompt` — path relative to `agents.json`; used in *replace* mode (none of Copilot's
  built-in instructions). `null` = empty system prompt.
- `tools` — built-in CLI tool names passed as `available_tools`; `[]` = no tools.
  Tool calls are auto-approved and logged.

Every configuration problem is logged and raised at load time. Nothing is silently defaulted.

## Library

```python
from copilot_agents import load_agent_config, Agent

cfg = load_agent_config("agents.json")           # starts runtime, validates everything
rev = Agent(cfg, type="reviewer", name="rev1")
answer = rev.invoke("Review this.", attachments=["main.py", "diagram.png"], timeout=60)
rev.close_session()   # drop history, keep agent
rev.stop()            # abort a running turn (from another thread)
rev.delete()          # close + remove from registry
```

- `invoke()` returns the final assistant text. On timeout the run is aborted and
  `InvokeTimeout` is raised; on any other failure an `AgentError` subclass is raised.
  Every error is logged first.
- Text attachments are inlined into the prompt as `--- file: NAME ---` blocks; images
  (`.png .jpg .jpeg .gif .webp .bmp`) go as SDK image attachments.
- Different agents may run in parallel (one runtime, one session each); the same agent
  twice concurrently raises `AgentBusy`.

### Log

Three levels: `CHAT` (`invoke:` / `think:` / `tool:` / `respond:`, full text, files, and the
turn's cost in AI credits on the `respond:` row), `INFO` (lifecycle, quota, models) and `ERROR`.
Coloured on the terminal with live token growth; plain text in `log_dir/agents-<ts>.log`.

## CLI

```bash
./chat.sh [agents.json]          # or: .venv/bin/python chat_client.py [agents.json]
```

Upper pane: live log. Lower pane: command console. Enter sends, Shift+Enter / Alt+Enter
inserts a newline (input grows to 10 rows), Tab completes, Up/Down history, Ctrl+C stops the
running turn.

```
/create <name> [type = <type>]
/invoke <name>, message = <text>[, files = {a.py, img.png}]
/stop <name>      /close <name>      /delete <name>
/list agents | models | types        /info <name>
/log chat | info | error             /clear     /help      /exit
```

One `/invoke` at a time; a second one is refused with “wait until … finished”.
