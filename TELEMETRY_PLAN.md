# Nanobot OpenTelemetry Instrumentation Plan

Based on [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai)

## Overview

This plan follows the official OTel GenAI semantic conventions (currently in development/experimental status) to instrument nanobot for observability with Honeycomb.io.

## Signal Strategy

Following the four signal types defined in OTel GenAI:

1. **Spans** - Primary signal for tracing LLM operations (inference, embeddings, retrieval, tool execution)
2. **Metrics** - Quantitative measurements (token usage, latency, throughput)
3. **Events** - Optional detailed capture of inputs/outputs (opt-in for privacy)
4. **Logs** - Existing loguru integration (keep as-is, correlate with traces)

## Architecture

```
nanobot/
  telemetry/
    __init__.py          # Public API
    provider.py          # OTel SDK setup (tracer/meter providers)
    spans.py             # Span creation helpers
    metrics.py           # Metric instruments
    events.py            # Event recording (opt-in)
    attributes.py        # Semantic convention constants
```

## Key Design Principles

### 1. Convention Compliance
- Use official semantic convention names exactly (e.g., `gen_ai.operation.name`, not custom names)
- Follow recommended attribute sets for each operation type
- Implement provider-specific conventions (Anthropic, OpenAI, etc.)

### 2. Privacy-First
- **No sensitive data by default** - inputs/outputs are opt-in only
- Respect `OTEL_SEMCONV_STABILITY_OPT_IN` for gradual migration
- Support content capture hooks for external storage

### 3. Minimal Performance Impact
- Lazy initialization
- Async-safe span creation
- No-op when telemetry disabled
- Defensive programming (never fail the app)

### 4. Backend-Agnostic with Honeycomb Optimization
- Use standard OTLP exporters (gRPC or HTTP)
- Configure via standard env vars (`OTEL_EXPORTER_OTLP_*`)
- Provide Honeycomb helpers but don't hard-code them

## Instrumentation Points

### 1. LLM Operations (High Priority)

**Location:** `nanobot/providers/litellm_provider.py:chat()`

**Span Type:** Inference
**Span Name:** `chat {model}`
**Span Kind:** `CLIENT`

**Required Attributes:**
- `gen_ai.operation.name` = "chat"
- `gen_ai.provider.name` = derived from model (e.g., "anthropic", "openai")

**Recommended Attributes:**
- `gen_ai.request.model` - Requested model (e.g., "claude-3-5-sonnet")
- `gen_ai.response.model` - Actual model used (may differ)
- `gen_ai.request.max_tokens` - Token limit
- `gen_ai.request.temperature` - Sampling parameter
- `gen_ai.usage.input_tokens` - Prompt tokens (with Anthropic cache handling)
- `gen_ai.usage.output_tokens` - Completion tokens
- `gen_ai.response.finish_reasons` - Why generation stopped
- `server.address` - API endpoint host
- `error.type` - Exception class or API error code

**Opt-In Attributes (Privacy Sensitive):**
- `gen_ai.system_instructions` - System prompt
- `gen_ai.input.messages` - User messages (JSON array)
- `gen_ai.output.messages` - Assistant responses
- `gen_ai.tool.definitions` - Available tools
- `gen_ai.tool.call.arguments` - Tool invocation params
- `gen_ai.tool.call.result` - Tool outputs

**Metrics to Record:**
- `gen_ai.client.operation.duration` (histogram, seconds)
- `gen_ai.client.token.usage` (histogram, tokens) - record 3 times:
  - With `gen_ai.token.type = "input"`
  - With `gen_ai.token.type = "output"`
  - With `gen_ai.token.type = "total"` (optional)

**Special Handling for Anthropic:**
```python
# Anthropic cache tokens
input_tokens = (
    response.usage.input_tokens +
    response.usage.cache_read_input_tokens +
    response.usage.cache_creation_input_tokens
)
```

**Implementation Approach:**
```python
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("nanobot.providers.litellm")

async def chat(self, messages, tools=None, model=None, ...):
    resolved_model = self._resolve_model(model or self.default_model)
    provider = self._extract_provider(resolved_model)

    with tracer.start_as_current_span(
        f"chat {resolved_model}",
        kind=trace.SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": resolved_model,
            "gen_ai.request.max_tokens": max_tokens,
            "gen_ai.request.temperature": temperature,
        }
    ) as span:
        start_time = time.time()

        try:
            response = await acompletion(**kwargs)
            duration = time.time() - start_time

            # Add response attributes
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.completion_tokens)
            span.set_attribute("gen_ai.response.finish_reasons", [response.finish_reason])

            # Record metrics
            record_metrics(duration, response.usage, provider, resolved_model)

            span.set_status(Status(StatusCode.OK))
            return self._parse_response(response)

        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute("error.type", type(e).__name__)
            span.record_exception(e)

            # Still record duration metric with error label
            record_metrics(time.time() - start_time, None, provider, resolved_model, error=e)
            raise
```

### 2. Tool Execution (Medium Priority)

**Location:** `nanobot/agent/loop.py` (tool execution loop)

**Span Type:** Execute Tool
**Span Name:** `execute_tool {tool_name}`
**Span Kind:** `INTERNAL`

**Required Attributes:**
- `gen_ai.operation.name` = "execute_tool"
- `gen_ai.tool.name` - Tool identifier (e.g., "read_file", "web_search")

**Recommended Attributes:**
- `gen_ai.tool.type` = "function" (nanobot uses function tools)
- `gen_ai.tool.call.id` - Unique call identifier from LLM response
- `error.type` - If tool execution fails

**Opt-In Attributes:**
- `gen_ai.tool.call.arguments` - JSON string of tool parameters
- `gen_ai.tool.call.result` - Tool output (may be large)

**Implementation:**
```python
for tool_call in response.tool_calls:
    with tracer.start_as_current_span(
        f"execute_tool {tool_call.name}",
        kind=trace.SpanKind.INTERNAL,
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_call.name,
            "gen_ai.tool.type": "function",
            "gen_ai.tool.call.id": tool_call.id,
        }
    ) as span:
        try:
            result = await self.tools.execute(tool_call.name, tool_call.arguments)
            span.set_status(Status(StatusCode.OK))

            # Opt-in: record arguments/result
            if should_capture_tool_content():
                span.set_attribute("gen_ai.tool.call.arguments", json.dumps(tool_call.arguments))
                span.set_attribute("gen_ai.tool.call.result", result[:1000])  # Truncate
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute("error.type", type(e).__name__)
            span.record_exception(e)
            raise
```

### 3. Message Processing (Low Priority)

**Location:** `nanobot/agent/loop.py:_process_message()`

**Span Type:** Custom (not in GenAI conventions, but useful)
**Span Name:** `process_message`
**Span Kind:** `INTERNAL`

**Attributes:**
- `nanobot.channel` - Message source (slack, telegram, etc.)
- `nanobot.sender_id` - User identifier
- `nanobot.message.length` - Character count
- `nanobot.message.has_media` - Boolean
- `nanobot.iterations` - Agent loop iteration count
- `nanobot.session.key` - Session identifier

**Purpose:** Provides end-to-end tracing from message receipt to response, showing how many LLM calls and tool executions occurred.

### 4. Channel Operations (Optional)

**Locations:**
- `nanobot/channels/slack.py:send()`
- `nanobot/channels/slack.py:_download_slack_files()`

**Attributes:**
- `messaging.system` = "slack" (standard OTel messaging convention)
- `messaging.operation` = "send" or "receive"
- `messaging.destination.name` - Channel/chat ID

**Note:** Use standard OTel messaging conventions, not GenAI conventions.

## Metrics Instrumentation

### Required Metric: `gen_ai.client.operation.duration`

**Type:** Histogram
**Unit:** `s` (seconds)
**Buckets:** `[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]`

**Attributes:**
- `gen_ai.operation.name`
- `gen_ai.provider.name`
- `gen_ai.request.model`
- `error.type` (if failed)

### Recommended Metric: `gen_ai.client.token.usage`

**Type:** Histogram
**Unit:** `{token}`
**Buckets:** Exponential (TBD based on typical usage)

**Attributes:**
- `gen_ai.operation.name`
- `gen_ai.provider.name`
- `gen_ai.request.model`
- `gen_ai.token.type` = "input" | "output"

**Record 2-3 times per operation:**
1. Input tokens with `type=input`
2. Output tokens with `type=output`
3. Optional: Total tokens with `type=total`

### Optional Server Metrics (if hosting LLM)

These apply if nanobot ever hosts its own model server:
- `gen_ai.server.request.duration`
- `gen_ai.server.time_to_first_token`
- `gen_ai.server.time_per_output_token`

**Current Status:** Not applicable (nanobot is a client only)

## Configuration Design

### Standard OTel Environment Variables

```bash
# Service identification
export OTEL_SERVICE_NAME="nanobot"
export OTEL_SERVICE_VERSION="0.1.4"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=production"

# OTLP exporter configuration
export OTEL_EXPORTER_OTLP_ENDPOINT="https://api.honeycomb.io"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"  # or grpc

# Honeycomb authentication
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=YOUR_API_KEY,x-honeycomb-dataset=nanobot"

# Or separate trace/metric endpoints
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://api.honeycomb.io/v1/traces"
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="https://api.honeycomb.io/v1/metrics"

# Content capture (opt-in)
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="false"  # Default: disabled
```

### Config File (nanobot-specific)

```yaml
telemetry:
  enabled: true

  # Content capture settings
  capture_inputs: false      # gen_ai.input.messages
  capture_outputs: false     # gen_ai.output.messages
  capture_system_prompts: false  # gen_ai.system_instructions
  capture_tool_arguments: false  # gen_ai.tool.call.arguments
  capture_tool_results: false    # gen_ai.tool.call.result

  # Sampling (optional)
  trace_sample_rate: 1.0     # 100% by default

  # Custom attributes (optional)
  resource_attributes:
    deployment.environment: "production"
    service.namespace: "ai-agents"
```

### Programmatic Initialization

```python
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION

def init_telemetry(config):
    """Initialize OTel with configuration."""
    if not config.telemetry.enabled:
        return

    # Create resource
    resource = Resource.create({
        SERVICE_NAME: "nanobot",
        SERVICE_VERSION: __version__,
        **config.telemetry.resource_attributes,
    })

    # Setup traces
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    trace.set_tracer_provider(trace_provider)

    # Setup metrics
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(),
        export_interval_millis=30000,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
    )
    metrics.set_meter_provider(meter_provider)
```

## Privacy & Security Considerations

### Content Capture Strategy

**Default:** Capture NO sensitive content (inputs, outputs, tool args/results)

**Opt-In Mechanisms:**
1. Environment variable: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`
2. Config file: `telemetry.capture_inputs = true`
3. Per-request context (advanced): Set span attribute before operation

**External Storage Pattern:**
```python
# Instead of setting span attribute directly:
# span.set_attribute("gen_ai.input.messages", messages)

# Upload to external storage and reference:
content_id = await upload_to_s3(messages)
span.set_attribute("gen_ai.input.messages.ref", f"s3://bucket/{content_id}")
```

### PII Redaction

Implement hooks for custom redaction:
```python
def redact_pii(content: str) -> str:
    """User-provided redaction function."""
    # Redact emails, phone numbers, API keys, etc.
    return redacted_content

# Use in instrumentation
if should_capture_content():
    safe_content = redact_pii(content)
    span.set_attribute("gen_ai.input.messages", safe_content)
```

## Error Handling

### Defensive Programming

Telemetry must **never** break the application:

```python
def safe_set_attribute(span, key, value):
    """Set attribute with error handling."""
    try:
        span.set_attribute(key, value)
    except Exception as e:
        logger.debug(f"Failed to set telemetry attribute {key}: {e}")

def safe_record_metric(metric, value, attributes):
    """Record metric with error handling."""
    try:
        metric.record(value, attributes=attributes)
    except Exception as e:
        logger.debug(f"Failed to record metric: {e}")
```

### No-Op When Disabled

```python
class NoOpTracer:
    """Tracer that does nothing when telemetry disabled."""
    def start_as_current_span(self, name, **kwargs):
        return nullcontext()

def get_tracer(name: str):
    if telemetry_enabled():
        return trace.get_tracer(name)
    return NoOpTracer()
```

## Migration & Versioning

### Convention Version Management

```python
# Check which conventions to use
semconv_version = os.getenv("OTEL_SEMCONV_STABILITY_OPT_IN", "")

if "gen_ai_latest_experimental" in semconv_version:
    # Use latest conventions
    OPERATION_NAME_ATTR = "gen_ai.operation.name"
else:
    # Use stable/older conventions (if different)
    OPERATION_NAME_ATTR = "gen_ai.operation.name"
```

**Current Status:** All GenAI conventions are experimental, so we can adopt latest.

### Backward Compatibility

Since nanobot doesn't have existing telemetry, we can start fresh with current conventions. Document the version used:

```python
# nanobot/telemetry/attributes.py
"""
Semantic conventions based on OpenTelemetry GenAI v1.37.0 (experimental)
https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai
"""
```

## Testing Strategy

### Unit Tests
- Test span creation with correct attributes
- Verify metric recording
- Test error handling (telemetry failures don't crash app)
- Test privacy controls (content capture on/off)

### Integration Tests
- End-to-end tracing of message → LLM → tool → response
- Verify spans are properly nested
- Check metric values are reasonable

### Observability Validation
- Use Honeycomb or Jaeger to visualize traces
- Verify all expected attributes present
- Check metric distributions make sense

## Rollout Plan

### Phase 1: Core LLM Tracing (Week 1)
- [ ] Setup OTel SDK initialization
- [ ] Instrument `litellm_provider.py:chat()`
- [ ] Add required attributes only
- [ ] Implement metrics (duration + token usage)
- [ ] Test with Honeycomb

### Phase 2: Tool Execution (Week 2)
- [ ] Instrument tool execution spans
- [ ] Add tool-specific attributes
- [ ] Verify nested spans (LLM → tool → LLM)

### Phase 3: Privacy Controls (Week 3)
- [ ] Implement content capture opt-in
- [ ] Add configuration options
- [ ] Test redaction hooks

### Phase 4: Polish & Documentation (Week 4)
- [ ] Add message processing spans
- [ ] Add channel operation spans
- [ ] Write user documentation
- [ ] Create example queries/dashboards

## Success Metrics

After implementation, we should be able to:

1. **Trace end-to-end message processing** - See full flow from Slack message to LLM to tool to response
2. **Measure LLM latency** - P50, P95, P99 by model and provider
3. **Track token usage** - Input/output tokens by operation and model
4. **Identify errors** - Group by error type, provider, model
5. **Analyze tool usage** - Which tools are called most, their success rates
6. **Monitor costs** - Estimate costs using token counts and provider pricing

## Example Honeycomb Queries

```
# LLM latency by model
VISUALIZE P95(gen_ai.client.operation.duration)
WHERE gen_ai.operation.name = "chat"
GROUP BY gen_ai.request.model

# Token usage over time
VISUALIZE SUM(gen_ai.client.token.usage)
WHERE gen_ai.token.type = "output"
GROUP BY gen_ai.provider.name

# Error rate by provider
VISUALIZE COUNT WHERE error.type EXISTS
GROUP BY gen_ai.provider.name, error.type

# Tool execution patterns
VISUALIZE COUNT
WHERE gen_ai.operation.name = "execute_tool"
GROUP BY gen_ai.tool.name

# End-to-end latency
VISUALIZE HEATMAP(duration_ms)
WHERE name = "process_message"
GROUP BY nanobot.channel
```

## References

- [OTel GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai)
- [OTel Python SDK](https://opentelemetry.io/docs/instrumentation/python/)
- [Honeycomb OTLP Ingest](https://docs.honeycomb.io/send-data/opentelemetry/python/)
- [GenAI Spans Spec](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-spans.md)
- [GenAI Metrics Spec](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-metrics.md)
- [Anthropic Conventions](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/anthropic.md)
