"""Slack channel implementation using Socket Mode."""

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from opentelemetry import trace
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import SlackConfig
from nanobot.telemetry.attributes import MessagingAttributes, NanobotAttributes


def markdown_to_slack(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn format.

    Conversions:
    - **bold** → *bold*
    - *italic* or _italic_ → _italic_
    - [link](url) → <url|link>
    - # Headers → *Header* (bold text)
    - - bullets → * bullets
    - ~~strikethrough~~ → ~strikethrough~
    """
    if not text:
        return text

    # Convert headers (# Header) to bold (*Header*)
    # Match 1-6 # symbols at start of line followed by space and text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert links: [text](url) → <url|text>
    # Note: This handles simple links. Complex nested cases might need more work.
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)

    # Convert strikethrough: ~~text~~ → ~text~
    text = re.sub(r'~~([^~]+)~~', r'~\1~', text)

    # Convert bold: **text** or __text__ → *text*
    # We need to be careful not to confuse with italic markers
    # Process bold before italic to handle ** before *
    text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', text)
    text = re.sub(r'__([^_]+)__', r'*\1*', text)

    # Convert italic: *text* or _text_ (single) → _text_
    # But we need to skip already-converted bold (which now uses single *)
    # This is tricky - we'll convert remaining single * and _ to _
    # We need to avoid re-converting our bold markers

    # For italic, we need to be more careful. Let's use a different approach:
    # First, protect already-converted bold markers by temporarily replacing them
    bold_pattern = re.compile(r'\*([^*\n]+)\*')
    bold_matches = []

    def save_bold(match):
        bold_matches.append(match.group(0))
        return f"__BOLD_{len(bold_matches)-1}__"

    text = bold_pattern.sub(save_bold, text)

    # Now convert remaining single * to _ for italic
    text = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)', r'_\1_', text)

    # Restore bold markers
    for i, bold_text in enumerate(bold_matches):
        text = text.replace(f"__BOLD_{i}__", bold_text)

    # Convert bullet points: - at start of line or after whitespace → *
    text = re.sub(r'^(\s*)- ', r'\1* ', text, flags=re.MULTILINE)

    return text


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode."""

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None

    async def start(self) -> None:
        """Start the Slack Socket Mode client."""
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error(f"Unsupported Slack mode: {self.config.mode}")
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # Resolve bot user ID for mention handling
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info(f"Slack bot connected as {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Slack auth_test failed: {e}")

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Slack client."""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning(f"Slack socket close failed: {e}")
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._web_client:
            logger.warning("Slack client not running")
            return

        tracer = trace.get_tracer("nanobot.channels.slack")
        with tracer.start_as_current_span(
            "slack send",
            kind=trace.SpanKind.INTERNAL,
            attributes={
                MessagingAttributes.SYSTEM: "slack",
                MessagingAttributes.OPERATION: "send",
                MessagingAttributes.DESTINATION_NAME: msg.chat_id,
            },
        ):
            try:
                slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
                thread_ts = slack_meta.get("thread_ts")
                channel_type = slack_meta.get("channel_type")
                # Only reply in thread for channel/group messages; DMs don't use threads
                use_thread = thread_ts and channel_type != "im"

                content = msg.content or ""
                # Convert standard markdown to Slack mrkdwn format
                slack_content = markdown_to_slack(content)

                # Slack has a 3000 character limit per text block
                # Split long messages into multiple blocks
                MAX_BLOCK_LENGTH = 3000
                blocks = []

                if len(slack_content) <= MAX_BLOCK_LENGTH:
                    # Short message - single block
                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": slack_content
                        }
                    })
                else:
                    # Long message - split into chunks
                    # Try to split at paragraph breaks for better readability
                    chunks = []
                    current_chunk = []
                    current_length = 0

                    # Split by paragraphs (double newline)
                    paragraphs = slack_content.split('\n\n')

                    for para in paragraphs:
                        para_length = len(para) + 2  # +2 for the \n\n we'll add back

                        # If single paragraph is too long, split it further
                        if para_length > MAX_BLOCK_LENGTH:
                            # Save current chunk if any
                            if current_chunk:
                                chunks.append('\n\n'.join(current_chunk))
                                current_chunk = []
                                current_length = 0

                            # Split long paragraph by sentences or lines
                            lines = para.split('\n')
                            line_chunk = []
                            line_length = 0

                            for line in lines:
                                if line_length + len(line) + 1 > MAX_BLOCK_LENGTH:
                                    if line_chunk:
                                        chunks.append('\n'.join(line_chunk))
                                        line_chunk = []
                                        line_length = 0

                                    # If single line is still too long, hard truncate
                                    if len(line) > MAX_BLOCK_LENGTH:
                                        chunks.append(line[:MAX_BLOCK_LENGTH - 3] + "...")
                                    else:
                                        line_chunk.append(line)
                                        line_length = len(line)
                                else:
                                    line_chunk.append(line)
                                    line_length += len(line) + 1

                            if line_chunk:
                                chunks.append('\n'.join(line_chunk))
                        else:
                            # Paragraph fits, check if it fits in current chunk
                            if current_length + para_length > MAX_BLOCK_LENGTH:
                                # Save current chunk and start new one
                                chunks.append('\n\n'.join(current_chunk))
                                current_chunk = [para]
                                current_length = para_length
                            else:
                                current_chunk.append(para)
                                current_length += para_length

                    # Add remaining chunk
                    if current_chunk:
                        chunks.append('\n\n'.join(current_chunk))

                    # Convert chunks to blocks (max 50 blocks per message)
                    for i, chunk in enumerate(chunks[:50]):  # Slack limit: 50 blocks
                        blocks.append({
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": chunk
                            }
                        })

                    # If we had to truncate (more than 50 chunks), add a note
                    if len(chunks) > 50:
                        blocks.append({
                            "type": "context",
                            "elements": [{
                                "type": "mrkdwn",
                                "text": f"_Message truncated ({len(chunks) - 50} blocks omitted)_"
                            }]
                        })

                await self._web_client.chat_postMessage(
                    channel=msg.chat_id,
                    text=slack_content,  # Fallback text for notifications
                    blocks=blocks,
                    thread_ts=thread_ts if use_thread else None,
                )
            except Exception as e:
                logger.error(f"Error sending Slack message: {e}")

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.type != "events_api":
            return

        # Acknowledge right away
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # Handle app mentions or plain messages
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        # Ignore bot/system messages (any subtype = not a normal user message)
        if event.get("subtype") and event.get("subtype") != "file_share":
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # Avoid double-processing: Slack sends both `message` and `app_mention`
        # for mentions in channels. Prefer `app_mention` UNLESS there are files
        # attached, since only the message event carries file attachments.
        text = event.get("text") or ""
        files = event.get("files", [])
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text and not files:
            return

        # Debug: log basic event shape
        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        text = self._strip_bot_mention(text)

        thread_ts = event.get("thread_ts") or event.get("ts")

        # Download any attached files (images)
        media_paths = await self._download_slack_files(files) if files else []

        # Add :eyes: reaction to the triggering message (best-effort)
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name="eyes",
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug(f"Slack reactions_add failed: {e}")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            media=media_paths,
            metadata={
                "slack": {
                    "event": event,
                    "thread_ts": thread_ts,
                    "channel_type": channel_type,
                }
            },
        )

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        # Group / channel messages
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    async def _download_slack_files(self, files: list[dict[str, Any]]) -> list[str]:
        """
        Download Slack files to temporary locations and return local paths.

        Only downloads image files for vision model support.

        Args:
            files: List of file objects from Slack event.

        Returns:
            List of local file paths.
        """
        if not files or not self._web_client:
            return []

        tracer = trace.get_tracer("nanobot.channels.slack")
        with tracer.start_as_current_span(
            "slack download_files",
            kind=trace.SpanKind.INTERNAL,
            attributes={
                MessagingAttributes.SYSTEM: "slack",
                MessagingAttributes.OPERATION: "receive",
            },
        ) as span:
            local_paths = []

            _supported = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}

            for file_obj in files:
                mimetype = file_obj.get("mimetype", "")
                if mimetype not in _supported:
                    logger.debug(f"Skipping unsupported file type: {mimetype}")
                    continue

                url_private = file_obj.get("url_private_download") or file_obj.get("url_private")
                if not url_private:
                    logger.warning("No download URL for Slack file")
                    continue

                try:
                    # Use Slack SDK to download file with proper authentication
                    # The SDK handles auth and redirects correctly
                    file_id = file_obj.get("id")
                    if not file_id:
                        logger.warning("No file ID in Slack file object")
                        continue

                    # Download file using Slack API
                    file_info = await self._web_client.files_info(file=file_id)
                    if not file_info.get("ok"):
                        logger.error(f"Failed to get file info: {file_info.get('error')}")
                        continue

                    # Get the URL to download
                    file_data = file_info.get("file", {})
                    download_url = file_data.get("url_private_download") or file_data.get("url_private")

                    if not download_url:
                        logger.warning("No download URL in file info")
                        continue

                    # Download using the web client's session which handles auth
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            download_url,
                            headers={"Authorization": f"Bearer {self.config.bot_token}"},
                            follow_redirects=True,
                            timeout=30.0
                        )
                        response.raise_for_status()

                        # Check file size (Anthropic has 5MB limit)
                        content = response.content
                        file_size = len(content)
                        if file_size > 5 * 1024 * 1024:
                            logger.warning(f"Slack image too large: {file_size} bytes (max 5MB), skipping")
                            continue

                        # Log first few bytes to verify it's a valid image
                        header = content[:20].hex() if len(content) >= 20 else content.hex()
                        logger.debug(f"Image header bytes: {header}")

                        # If it looks like HTML, log the error
                        if content.startswith(b"<!DOCTYPE") or content.startswith(b"<html"):
                            error_text = content[:500].decode("utf-8", errors="ignore")
                            logger.error(f"Received HTML instead of image: {error_text}")
                            continue

                        # Save to temp file with proper extension
                        filetype = file_obj.get("filetype", "png")
                        suffix = f".{filetype}"

                        logger.info(f"Downloading Slack image: mimetype={mimetype}, filetype={filetype}, size={file_size}")

                        with tempfile.NamedTemporaryFile(
                            mode="wb",
                            suffix=suffix,
                            delete=False
                        ) as tmp:
                            bytes_written = tmp.write(content)
                            tmp.flush()  # Explicitly flush to disk
                            local_path = tmp.name
                            logger.info(f"Saved Slack image to {local_path} ({bytes_written} bytes written)")

                        # Verify the file was written correctly
                        if Path(local_path).stat().st_size != file_size:
                            logger.error(f"File size mismatch: expected {file_size}, got {Path(local_path).stat().st_size}")
                            continue

                        local_paths.append(local_path)

                except Exception as e:
                    logger.error(f"Failed to download Slack file: {e}")
                    continue

            span.set_attribute(NanobotAttributes.FILES_COUNT, len(local_paths))
            return local_paths
