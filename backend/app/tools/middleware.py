from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_core.messages import ToolMessage

try:
    from langchain.agents.middleware import before_model, wrap_tool_call
    from langchain.tools.tool_node import ToolCallRequest
except Exception:  # pragma: no cover - supports fallback mode with partial LangChain installs
    ToolCallRequest = Any

    def before_model(func):
        return func

    def wrap_tool_call(func):
        return func

try:
    from langgraph.runtime import Runtime
except Exception:  # pragma: no cover - optional typing only
    Runtime = Any

try:
    from langgraph.types import Command
except Exception:  # pragma: no cover - optional typing only
    Command = Any

logger = logging.getLogger(__name__)

# Maximum consecutive tool errors before the agent should stop retrying.
MAX_CONSECUTIVE_TOOL_ERRORS = 3



def _runtime_context(runtime: Any) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    return context if isinstance(context, dict) else {}


def _tool_error_counters(request: ToolCallRequest) -> dict[str, int]:
    runtime = getattr(request, "runtime", None)
    context = _runtime_context(runtime)
    if not isinstance(context, dict):
        return {}
    counters = context.setdefault("consecutive_tool_errors", {})
    return counters if isinstance(counters, dict) else {}


def _message_value(message: Any, key: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def tool_call_names_from_state(state: Any) -> list[str]:
    messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    tool_names: list[str] = []
    for message in messages:
        role = _message_value(message, "role")
        message_type = _message_value(message, "type")
        name = _message_value(message, "name")
        if (role == "tool" or message_type == "tool") and name:
            tool_names.append(str(name))
    return tool_names


@wrap_tool_call
def monitor_tool(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    tool_name = str(request.tool_call.get("name", "unknown"))
    tool_args = request.tool_call.get("args", {})
    logger.info("[monitor_tool] executing tool=%s args=%s", tool_name, tool_args)
    try:
        if hasattr(request, "runtime") and request.runtime is not None:
            context = getattr(request.runtime, "context", None)
            if isinstance(context, dict):
                tool_calls = context.setdefault("tool_calls", [])
                tool_calls.append(tool_name)
        result = handler(request)
        logger.info("[monitor_tool] tool=%s status=success", tool_name)
        # Reset consecutive error counter on success
        _tool_error_counters(request).pop(tool_name, None)
        return result
    except Exception as exc:
        counters = _tool_error_counters(request)
        error_count = counters.get(tool_name, 0) + 1
        counters[tool_name] = error_count
        if error_count >= MAX_CONSECUTIVE_TOOL_ERRORS:
            logger.warning(
                "[monitor_tool] tool=%s has failed %d times consecutively, stopping retries",
                tool_name,
                error_count,
            )
            counters[tool_name] = 0
            return ToolMessage(
                content=(
                    f"Tool '{tool_name}' failed {error_count} times with the same error pattern. "
                    f"Do NOT retry this tool. Proceed with the data you already have or generate a "
                    f"response without calling this tool again."
                ),
                tool_call_id=request.tool_call.get("id", ""),
            )
        logger.warning("[monitor_tool] tool=%s status=failed (%d/%d): %s", tool_name, error_count, MAX_CONSECUTIVE_TOOL_ERRORS, exc)
        raise



@before_model
def log_before_model(state: Any, runtime: Runtime) -> None:
    context_tool_calls = list(_runtime_context(runtime).get("tool_calls", [])) if runtime is not None else []
    message_tool_calls = tool_call_names_from_state(state)
    tool_calls = [*context_tool_calls, *message_tool_calls]
    logger.info(
        "[log_before_model] message_count=%s tool_called=%s tool_calls=%s",
        len(state.get("messages", [])) if isinstance(state, dict) else len(getattr(state, "messages", [])),
        bool(tool_calls),
        tool_calls,
    )
    return None
