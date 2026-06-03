from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware import before_model, wrap_tool_call
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

logger = logging.getLogger(__name__)


def _runtime_context(runtime: Any) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    return context if isinstance(context, dict) else {}


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
        return result
    except Exception:
        logger.exception("[monitor_tool] tool=%s status=failed", tool_name)
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
