"""GenAI metrics instrumentation."""

from typing import Any

from opentelemetry import metrics
from loguru import logger

from nanobot.telemetry.attributes import GenAIAttributes, TokenType


# Get meter for GenAI metrics
_meter = metrics.get_meter("nanobot.genai")

# Create metric instruments following GenAI semantic conventions
_operation_duration = _meter.create_histogram(
    name="gen_ai.client.operation.duration",
    unit="s",
    description="Duration of GenAI client operations",
)

_token_usage = _meter.create_histogram(
    name="gen_ai.client.token.usage",
    unit="{token}",
    description="Number of tokens used in GenAI operations",
)


def record_operation_metrics(
    duration: float,
    operation_name: str,
    provider: str,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error_type: str | None = None,
) -> None:
    """
    Record metrics for a GenAI operation.

    Args:
        duration: Operation duration in seconds.
        operation_name: Operation name (e.g., "chat").
        provider: Provider name (e.g., "anthropic", "openai").
        model: Model identifier.
        input_tokens: Number of input tokens used.
        output_tokens: Number of output tokens generated.
        error_type: Error type if operation failed.
    """
    # Build base attributes
    attrs = {
        GenAIAttributes.OPERATION_NAME: operation_name,
        GenAIAttributes.PROVIDER_NAME: provider,
        GenAIAttributes.REQUEST_MODEL: model,
    }

    if error_type:
        attrs[GenAIAttributes.ERROR_TYPE] = error_type

    # Record duration
    try:
        _operation_duration.record(duration, attributes=attrs)
    except Exception as e:
        logger.debug(f"Failed to record operation duration: {e}")

    # Record token usage (input)
    if input_tokens is not None:
        try:
            _token_usage.record(
                input_tokens,
                attributes={
                    **attrs,
                    GenAIAttributes.TOKEN_TYPE: TokenType.INPUT,
                }
            )
        except Exception as e:
            logger.debug(f"Failed to record input token usage: {e}")

    # Record token usage (output)
    if output_tokens is not None:
        try:
            _token_usage.record(
                output_tokens,
                attributes={
                    **attrs,
                    GenAIAttributes.TOKEN_TYPE: TokenType.OUTPUT,
                }
            )
        except Exception as e:
            logger.debug(f"Failed to record output token usage: {e}")
