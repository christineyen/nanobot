# Nanobot OpenTelemetry Instrumentation

Phase 1 ✅: LLM Operation Tracing
Phase 2 ✅: Tool Execution Tracing

## Overview

Nanobot uses OpenTelemetry to instrument LLM and agent operations following the [GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai) (experimental).

## What's Instrumented

### LLM Operations
- **Spans**: Every `chat()` call to LLMs creates a span named `chat {model}`
- **Attributes**: Request model, provider, max tokens, temperature, usage, finish reasons
- **Metrics**: Operation duration and token usage (input/output separately)
- **Content Capture**: Inputs, outputs, and tool definitions are captured by default

### Tool Execution
- **Spans**: Every tool call creates a span named `execute_tool {tool_name}`
- **Attributes**: Tool name, call ID, type (function), arguments, results
- **Nesting**: Tool spans are nested under the LLM span that requested them
- **Error Tracking**: Captures tool execution failures with exception details

### Auto-Instrumentation
- **HTTPX**: All HTTP requests (including LLM API calls) are automatically traced
- **Asyncio**: Async task execution is instrumented

## Configuration

### Honeycomb Setup (Recommended)

```bash
# Set these environment variables
export OTEL_EXPORTER_OTLP_ENDPOINT="https://api.honeycomb.io"
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=YOUR_API_KEY"
export ENVIRONMENT="production"  # Optional

# Then run nanobot
nanobot gateway
```

Or use the helper script:
```bash
source examples/honeycomb-setup.sh
nanobot gateway
```

### Other OTLP Backends

Works with any OTLP-compatible backend:

```bash
# Jaeger
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"

# Grafana Tempo
export OTEL_EXPORTER_OTLP_ENDPOINT="https://tempo.example.com"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64-creds>"

# Self-hosted OTel Collector
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
```

## What You Can See

### Traces

#### LLM Operations

Every LLM operation creates a trace with:

**Span Name:** `chat anthropic/claude-3-5-sonnet-20241022`

**Attributes:**
- `gen_ai.operation.name` = "chat"
- `gen_ai.provider.name` = "anthropic"
- `gen_ai.request.model` = "anthropic/claude-3-5-sonnet-20241022"
- `gen_ai.response.model` = "claude-3-5-sonnet-20241022"
- `gen_ai.request.max_tokens` = 4096
- `gen_ai.request.temperature` = 0.7
- `gen_ai.usage.input_tokens` = 150
- `gen_ai.usage.output_tokens` = 75
- `gen_ai.response.finish_reasons` = ["stop"]
- `gen_ai.input.messages` = (full message array)
- `gen_ai.output.messages` = (full response)
- `gen_ai.tool.definitions` = (if tools provided)

#### Tool Executions

Nested under LLM spans:

**Span Name:** `execute_tool read_file`

**Attributes:**
- `gen_ai.operation.name` = "execute_tool"
- `gen_ai.tool.name` = "read_file"
- `gen_ai.tool.type` = "function"
- `gen_ai.tool.call.id` = "call_abc123"
- `gen_ai.tool.call.arguments` = `{"path": "/path/to/file"}`
- `gen_ai.tool.call.result` = (tool output, truncated to 1000 chars)
- `error.type` = (if tool failed)

#### Nested Trace Example

```
chat claude-3-5-sonnet (2.5s)
  ├─ execute_tool read_file (0.05s)
  │   └─ httpx GET /api (0.02s)  [auto-instrumented]
  ├─ execute_tool web_search (0.8s)
  │   └─ httpx POST https://api.search.brave.com (0.75s)
  └─ chat continues... (1.65s)
```

**Metrics:**
- `gen_ai.client.operation.duration` - Histogram of operation latencies
- `gen_ai.client.token.usage` - Histogram of token counts (separate for input/output)

### Example Honeycomb Queries

#### LLM Queries

```
# Average latency by model
VISUALIZE P50(gen_ai.client.operation.duration), P95(gen_ai.client.operation.duration)
WHERE gen_ai.operation.name = "chat"
GROUP BY gen_ai.request.model

# Token usage over time
VISUALIZE SUM(gen_ai.client.token.usage)
WHERE gen_ai.token.type = "output"
GROUP BY gen_ai.provider.name

# LLM error rates
VISUALIZE COUNT
WHERE gen_ai.operation.name = "chat" AND error.type EXISTS
GROUP BY gen_ai.provider.name, error.type
```

#### Tool Execution Queries

```
# Most frequently used tools
VISUALIZE COUNT
WHERE gen_ai.operation.name = "execute_tool"
GROUP BY gen_ai.tool.name

# Tool execution latency
VISUALIZE P50(duration_ms), P95(duration_ms), P99(duration_ms)
WHERE gen_ai.operation.name = "execute_tool"
GROUP BY gen_ai.tool.name

# Tool failure rates
VISUALIZE COUNT
WHERE gen_ai.operation.name = "execute_tool" AND error.type EXISTS
GROUP BY gen_ai.tool.name, error.type

# Tools per LLM request
VISUALIZE COUNT(gen_ai.tool.name)
GROUP BY trace.trace_id

# Slowest tool calls
VISUALIZE duration_ms, gen_ai.tool.name, gen_ai.tool.call.arguments
WHERE gen_ai.operation.name = "execute_tool"
ORDER BY duration_ms DESC
LIMIT 20
```

## Privacy & Content Capture

**Current Status:** All inputs and outputs are captured by default for maximum learning.

**Future Plans:** Add opt-out configuration for production use:
```yaml
telemetry:
  capture_inputs: false   # Disable input message capture
  capture_outputs: false  # Disable output message capture
```

## Provider-Specific Handling

### Anthropic
Special handling for cache tokens:
```python
input_tokens = (
    response.usage.input_tokens +
    response.usage.cache_read_input_tokens +
    response.usage.cache_creation_input_tokens
)
```

### OpenAI
Standard token counting (no special handling needed).

## Implementation Details

### Spans
- **Kind:** `CLIENT` (we're a client calling external LLM APIs)
- **Status:** `OK` for successful calls, `ERROR` for exceptions
- **Naming:** Follows OTel convention: `{operation} {model}`

### Metrics
- **Histogram Buckets (Duration):** `[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]` seconds
- **Histogram Buckets (Tokens):** `[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]` tokens
- **Export Interval:** 30 seconds

### Error Handling
Telemetry is defensive - failures in instrumentation never crash the application:
```python
try:
    span.set_attribute(key, value)
except Exception as e:
    logger.debug(f"Telemetry error: {e}")
    # Continue normal operation
```

## Troubleshooting

### No Data in Honeycomb

1. **Check environment variables:**
   ```bash
   echo $OTEL_EXPORTER_OTLP_ENDPOINT
   echo $OTEL_EXPORTER_OTLP_HEADERS
   ```

2. **Check nanobot logs:**
   ```
   Tracing initialized: https://api.honeycomb.io/v1/traces
   Metrics initialized: https://api.honeycomb.io/v1/metrics
   OpenTelemetry initialization complete
   ```

3. **Verify Honeycomb API key:**
   - Go to https://ui.honeycomb.io/account
   - Check that your API key is valid
   - Ensure it has write permissions

4. **Check for errors:**
   ```bash
   nanobot gateway --verbose
   ```

### Spans Not Showing Up

- Wait 30-60 seconds for metric export
- Check that spans are being created (look for span creation logs)
- Verify OTLP exporter is configured correctly

### High Memory Usage

If telemetry causes high memory:
1. Reduce metric export frequency (edit `export_interval_millis` in `provider.py`)
2. Disable content capture (coming in future phase)
3. Temporarily disable telemetry by unsetting `OTEL_EXPORTER_OTLP_ENDPOINT`

## What's Next

### Phase 3: Privacy Controls
- Configuration to disable input/output capture
- Redaction hooks for PII
- External storage pattern for large content

### Phase 4: End-to-End Tracing
- Message processing spans
- Channel operation spans
- Full request lifecycle visibility

## References

- [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai)
- [OTel Python SDK](https://opentelemetry.io/docs/instrumentation/python/)
- [Honeycomb Documentation](https://docs.honeycomb.io/)
- [OTLP Specification](https://opentelemetry.io/docs/specs/otlp/)
