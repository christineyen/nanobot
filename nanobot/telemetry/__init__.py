"""OpenTelemetry instrumentation for nanobot.

Based on OpenTelemetry GenAI Semantic Conventions v1.37.0 (experimental)
https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai
"""

from nanobot.telemetry.provider import init_telemetry, shutdown_telemetry
from nanobot.telemetry.attributes import GenAIAttributes
from nanobot.telemetry.spans import tool_execution_span

__all__ = [
    "init_telemetry",
    "shutdown_telemetry",
    "GenAIAttributes",
    "tool_execution_span",
]
