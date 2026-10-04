#!/usr/bin/env python
"""List available Copilot models and write the chosen one into agents.json.

    python select_model.py [agents.json]

Three prompts: pick a model, then (if it supports them) a reasoning effort
and a context tier. Each follow-up prompt marks a default option that plain
Enter selects. The picked model/reasoning_effort/context_tier are written
into agents.json's defaults; nothing else reads the last two fields yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from copilot_agents.log import enable_console
from copilot_agents.runtime import Runtime


def _price_per_million(price: float | None, batch_size: int | None) -> str:
    if price is None:
        return "-"
    per_million = price * 1_000_000 / batch_size if batch_size else price
    return f"{per_million:.0f}"


def _cost_cols(tp: Any, batch_size: int | None = None) -> str:
    """Format in/out/cache-read price columns for one pricing tier (or blanks if none).

    ``batch_size`` lives only on the default tier; pass it explicitly for the
    long-context tier, which shares the same batch size but has no field of its own.
    """
    if tp is None:
        return f"{'-':>8} {'-':>8} {'-':>8}"
    batch_size = batch_size if batch_size is not None else getattr(tp, "batch_size", None)
    in_p = _price_per_million(tp.input_price, batch_size)
    out_p = _price_per_million(tp.output_price, batch_size)
    cache_p = _price_per_million(tp.cache_read_price, batch_size)
    return f"{in_p:>8} {out_p:>8} {cache_p:>8}"


def _ask(prompt: str, options: list[tuple[str, str]], default_idx: int) -> int:
    """Print numbered (label, cost-cols) options, default marked; Enter picks it."""
    for i, (label, cost_cols) in enumerate(options):
        mark = "  (default)" if i == default_idx else ""
        print(f"  {i}  {label:<28} {cost_cols}{mark}")
    raw = input(f"{prompt} [enter = default]: ").strip()
    if raw == "":
        return default_idx
    try:
        idx = int(raw)
        if not (0 <= idx < len(options)):
            raise ValueError
    except ValueError:
        print(f"invalid selection: {raw!r}", file=sys.stderr)
        raise SystemExit(1) from None
    return idx


def _ask_reasoning_effort(m: Any) -> str | None:
    efforts = m.supported_reasoning_efforts or []
    if not efforts:
        return None
    blank = f"{'':>8} {'':>8} {'':>8}"
    options = [("(model default, no override)", blank)] + [(effort, blank) for effort in efforts]
    default_idx = 0
    if m.default_reasoning_effort in efforts:
        default_idx = efforts.index(m.default_reasoning_effort) + 1
    print("reasoning effort:")
    idx = _ask("select reasoning effort", options, default_idx)
    return None if idx == 0 else efforts[idx - 1]


def _ask_context_tier(m: Any) -> str | None:
    tp = m.billing.token_prices if m.billing else None
    if tp is None or tp.long_context is None:
        return None
    options = [("default", _cost_cols(tp)), ("long_context", _cost_cols(tp.long_context, tp.batch_size))]
    print("context tier:")
    idx = _ask("select context tier", options, 0)
    return None if idx == 0 else "long_context"


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "agents.json")
    if not path.is_file():
        print(f"config file not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    enable_console(True)
    runtime = Runtime.get()
    runtime.start()
    models = sorted(runtime.models.values(), key=lambda m: m.id)
    if not models:
        print("no models available", file=sys.stderr)
        raise SystemExit(1)

    print(f"{'':>4} {'id':<24} {'name':<24} {'in/1M':>8} {'out/1M':>8} {'cache/1M':>8}")
    for i, m in enumerate(models, 1):
        tp = m.billing.token_prices if m.billing else None
        print(f"{i:>4} {m.id:<24} {m.name:<24} {_cost_cols(tp)}")

    raw = input(f"select model [1-{len(models)}]: ").strip()
    try:
        idx = int(raw)
        if not (1 <= idx <= len(models)):
            raise ValueError
    except ValueError:
        print(f"invalid selection: {raw!r}", file=sys.stderr)
        raise SystemExit(1) from None
    m = models[idx - 1]

    reasoning_effort = _ask_reasoning_effort(m)
    context_tier = _ask_context_tier(m)

    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.setdefault("defaults", {})
    defaults.pop("reasoning_effort", None)
    defaults.pop("context_tier", None)
    defaults["model"] = m.id
    if reasoning_effort is not None:
        defaults["reasoning_effort"] = reasoning_effort
    if context_tier is not None:
        defaults["context_tier"] = context_tier
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    extra = {k: v for k, v in (("reasoning_effort", reasoning_effort), ("context_tier", context_tier)) if v is not None}
    print(f"defaults.model = {m.id!r}" + (f"  {extra}" if extra else "") + f" written to {path}")


if __name__ == "__main__":
    main()
