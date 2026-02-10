"""Span creation helpers for GenAI operations."""

import json
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from loguru import logger

from nanobot.telemetry.attributes import GenAIAttributes, OperationName, ToolType


@contextmanager
def tool_execution_span(
    tool_name: str,
    tool_call_id: str | None = None,
    arguments: dict[str, Any] | None = None,
):
    """
    Create a span for tool execution following GenAI semantic conventions.

    Args:
        tool_name: Name of the tool being executed (e.g., "read_file", "web_search")
        tool_call_id: Unique ID for this tool call from the LLM response
        arguments: Tool arguments/parameters

    Yields:
        ToolSpan: A context manager for the tool execution span

    Example:
        with tool_execution_span("read_file", tool_call_id="call_123", arguments={"path": "..."}) as span:
            result = await execute_tool(...)
            span.set_result(result)
    """
    tracer = trace.get_tracer("nanobot.agent")

    # Create span following GenAI conventions
    with tracer.start_as_current_span(
        f"execute_tool {tool_name}",
        kind=trace.SpanKind.INTERNAL,
        attributes={
            GenAIAttributes.OPERATION_NAME: OperationName.EXECUTE_TOOL,
            GenAIAttributes.TOOL_NAME: tool_name,
            GenAIAttributes.TOOL_TYPE: ToolType.FUNCTION,
        }
    ) as span:
        # Add tool call ID if available
        if tool_call_id:
            span.set_attribute(GenAIAttributes.TOOL_CALL_ID, tool_call_id)

        # Capture tool arguments (full capture by default)
        if arguments:
            try:
                span.set_attribute(
                    GenAIAttributes.TOOL_CALL_ARGUMENTS,
                    json.dumps(arguments, ensure_ascii=False)
                )
            except Exception as e:
                logger.debug(f"Failed to capture tool arguments: {e}")

        # Create wrapper to allow result capture
        span_wrapper = ToolSpanWrapper(span)

        try:
            yield span_wrapper
            # If no exception, mark as successful
            if not span_wrapper._status_set:
                span.set_status(Status(StatusCode.OK))
        except Exception as e:
            # Capture error information
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute(GenAIAttributes.ERROR_TYPE, type(e).__name__)
            span.record_exception(e)
            raise


class ToolSpanWrapper:
    """Wrapper around a span to provide tool-specific methods."""

    def __init__(self, span):
        self.span = span
        self._status_set = False

    def set_result(self, result: str, truncate_at: int = 1000):
        """
        Set the tool execution result.

        Args:
            result: Tool output/result string
            truncate_at: Maximum length to capture (default 1000 chars)
        """
        if not result:
            return

        try:
            # Truncate large results to avoid overwhelming telemetry
            truncated = result[:truncate_at]
            if len(result) > truncate_at:
                truncated += f"... (truncated {len(result) - truncate_at} chars)"

            self.span.set_attribute(GenAIAttributes.TOOL_CALL_RESULT, truncated)
        except Exception as e:
            logger.debug(f"Failed to capture tool result: {e}")

    def set_attribute(self, key: str, value: Any):
        """Set a custom attribute on the span."""
        try:
            self.span.set_attribute(key, value)
        except Exception as e:
            logger.debug(f"Failed to set span attribute {key}: {e}")

    def set_status(self, status: Status):
        """Set the span status."""
        self.span.set_status(status)
        self._status_set = True
