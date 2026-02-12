"""LiteLLM provider implementation for multi-provider support."""

import json
import os
import time
from typing import Any

import litellm
from litellm import acompletion
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.registry import find_by_model, find_gateway
from nanobot.telemetry.attributes import GenAIAttributes, OperationName
from nanobot.telemetry.metrics import record_operation_metrics


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        
        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)
        
        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)
        
        if api_base:
            litellm.api_base = api_base
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True
    
    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)
    
    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model
        
        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"
        
        return model
    
    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    def _extract_provider(self, model: str) -> str:
        """Extract provider name from model string for telemetry."""
        # Model format: "provider/model" or just "model"
        if "/" in model:
            provider = model.split("/")[0]
            # Map litellm provider names to OTel semantic convention names
            provider_map = {
                "anthropic": "anthropic",
                "openai": "openai",
                "azure": "azure",
                "bedrock": "aws.bedrock",
                "vertex_ai": "vertex_ai",
                "cohere": "cohere",
            }
            return provider_map.get(provider, provider)
        return "unknown"
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = self._resolve_model(model or self.default_model)
        provider = self._extract_provider(model)

        # Get tracer for this provider
        tracer = trace.get_tracer("nanobot.providers.litellm")

        # Start span following GenAI semantic conventions
        with tracer.start_as_current_span(
            f"chat {model}",
            kind=trace.SpanKind.CLIENT,
            attributes={
                GenAIAttributes.OPERATION_NAME: OperationName.CHAT,
                GenAIAttributes.PROVIDER_NAME: provider,
                GenAIAttributes.REQUEST_MODEL: model,
                GenAIAttributes.REQUEST_MAX_TOKENS: max_tokens,
                GenAIAttributes.REQUEST_TEMPERATURE: temperature,
            }
        ) as span:
            start_time = time.time()

            # Add server address if available
            if self.api_base:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(self.api_base)
                    span.set_attribute(GenAIAttributes.SERVER_ADDRESS, parsed.hostname or "")
                    if parsed.port:
                        span.set_attribute(GenAIAttributes.SERVER_PORT, parsed.port)
                except Exception:
                    pass

            # Capture input messages (full content capture enabled by default)
            try:
                span.set_attribute(GenAIAttributes.INPUT_MESSAGES, json.dumps(messages))
                span.set_attribute(GenAIAttributes.INPUT_MESSAGES_LENGTH, len(messages))
            except Exception as e:
                logger.debug(f"Failed to capture input messages: {e}")

            # Capture tool definitions if present
            if tools:
                try:
                    span.set_attribute(GenAIAttributes.TOOL_DEFINITIONS, json.dumps(tools))
                except Exception as e:
                    logger.debug(f"Failed to capture tool definitions: {e}")

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
            self._apply_model_overrides(model, kwargs)

            # Pass api_key directly — more reliable than env vars alone
            if self.api_key:
                kwargs["api_key"] = self.api_key

            # Pass api_base for custom endpoints
            if self.api_base:
                kwargs["api_base"] = self.api_base

            # Pass extra headers (e.g. APP-Code for AiHubMix)
            if self.extra_headers:
                kwargs["extra_headers"] = self.extra_headers

            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            try:
                response = await acompletion(**kwargs)
                duration = time.time() - start_time
                parsed = self._parse_response(response)

                # Add response attributes
                try:
                    span.set_attribute(GenAIAttributes.RESPONSE_MODEL, response.model)
                    span.set_attribute(GenAIAttributes.RESPONSE_ID, response.id or "")
                    span.set_attribute(
                        GenAIAttributes.RESPONSE_FINISH_REASONS,
                        [parsed.finish_reason] if parsed.finish_reason else []
                    )

                    # Add usage attributes
                    if parsed.usage:
                        input_tokens = parsed.usage.get("prompt_tokens", 0)
                        output_tokens = parsed.usage.get("completion_tokens", 0)

                        # Special handling for Anthropic cache tokens
                        if provider == "anthropic" and hasattr(response, "usage"):
                            # Anthropic separates cache tokens
                            cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
                            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0)
                            input_tokens += cache_read + cache_creation

                        span.set_attribute(GenAIAttributes.USAGE_INPUT_TOKENS, input_tokens)
                        span.set_attribute(GenAIAttributes.USAGE_OUTPUT_TOKENS, output_tokens)

                        # Record metrics
                        record_operation_metrics(
                            duration=duration,
                            operation_name=OperationName.CHAT,
                            provider=provider,
                            model=model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )

                    # Capture output messages
                    if parsed.content:
                        try:
                            output_msg = [{"role": "assistant", "content": parsed.content}]
                            span.set_attribute(GenAIAttributes.OUTPUT_MESSAGES, json.dumps(output_msg))
                        except Exception as e:
                            logger.debug(f"Failed to capture output messages: {e}")

                except Exception as e:
                    logger.debug(f"Failed to set response attributes: {e}")

                span.set_status(Status(StatusCode.OK))
                return parsed

            except Exception as e:
                duration = time.time() - start_time
                error_type = type(e).__name__

                # Set error attributes
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute(GenAIAttributes.ERROR_TYPE, error_type)
                span.record_exception(e)

                # Record error metrics
                record_operation_metrics(
                    duration=duration,
                    operation_name=OperationName.CHAT,
                    provider=provider,
                    model=model,
                    error_type=error_type,
                )

                # Return error as content for graceful handling
                return LLMResponse(
                    content=f"Error calling LLM: {str(e)}",
                    finish_reason="error",
                )
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        reasoning_content = getattr(message, "reasoning_content", None)
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
