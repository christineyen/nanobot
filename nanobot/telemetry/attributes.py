"""OpenTelemetry GenAI Semantic Convention attribute names.

Based on:
https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-spans.md
"""


class GenAIAttributes:
    """Semantic convention attribute names for GenAI operations."""

    # Operation attributes
    OPERATION_NAME = "gen_ai.operation.name"
    PROVIDER_NAME = "gen_ai.provider.name"

    # Request attributes
    REQUEST_MODEL = "gen_ai.request.model"
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    REQUEST_TOP_P = "gen_ai.request.top_p"
    REQUEST_TOP_K = "gen_ai.request.top_k"
    REQUEST_FREQUENCY_PENALTY = "gen_ai.request.frequency_penalty"
    REQUEST_PRESENCE_PENALTY = "gen_ai.request.presence_penalty"
    REQUEST_STOP_SEQUENCES = "gen_ai.request.stop_sequences"

    # Response attributes
    RESPONSE_MODEL = "gen_ai.response.model"
    RESPONSE_ID = "gen_ai.response.id"
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

    # Usage attributes
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

    # Token type for metrics
    TOKEN_TYPE = "gen_ai.token.type"

    # Content attributes (opt-in, sensitive)
    SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
    INPUT_MESSAGES = "gen_ai.input.messages"
    INPUT_MESSAGES_LENGTH = "gen_ai.input.messages.length"
    OUTPUT_MESSAGES = "gen_ai.output.messages"

    # Tool attributes
    TOOL_NAME = "gen_ai.tool.name"
    TOOL_TYPE = "gen_ai.tool.type"
    TOOL_CALL_ID = "gen_ai.tool.call.id"
    TOOL_DEFINITIONS = "gen_ai.tool.definitions"
    TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
    TOOL_CALL_RESULT = "gen_ai.tool.call.result"

    # Server attributes
    SERVER_ADDRESS = "server.address"
    SERVER_PORT = "server.port"

    # Error attributes
    ERROR_TYPE = "error.type"


class TokenType:
    """Token type values for gen_ai.token.type attribute."""

    INPUT = "input"
    OUTPUT = "output"
    TOTAL = "total"  # Optional


class OperationName:
    """Standard operation names for gen_ai.operation.name."""

    CHAT = "chat"
    TEXT_COMPLETION = "text_completion"
    EMBEDDINGS = "embeddings"
    EXECUTE_TOOL = "execute_tool"


class ToolType:
    """Tool type values for gen_ai.tool.type attribute."""

    FUNCTION = "function"
    EXTENSION = "extension"
    DATASTORE = "datastore"


class NanobotAttributes:
    """Nanobot-specific span attributes (not part of OTel GenAI conventions)."""

    CHANNEL = "nanobot.channel"
    SENDER_ID = "nanobot.sender_id"
    SESSION_KEY = "nanobot.session.key"
    MESSAGE_LENGTH = "nanobot.message.length"
    MESSAGE_HAS_MEDIA = "nanobot.message.has_media"
    ITERATIONS = "nanobot.iterations"
    RESPONSE_LENGTH = "nanobot.response.length"
    FILES_COUNT = "nanobot.files.count"


class MessagingAttributes:
    """OTel messaging semantic convention attributes."""

    SYSTEM = "messaging.system"
    OPERATION = "messaging.operation"
    DESTINATION_NAME = "messaging.destination.name"
