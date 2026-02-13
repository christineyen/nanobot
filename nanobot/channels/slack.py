"""Slack channel implementation using Socket Mode."""

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient
from slackify_markdown import slackify_markdown

from opentelemetry import trace
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from pydantic import Field

from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base
from nanobot.telemetry.attributes import MessagingAttributes, NanobotAttributes


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"
    allow_from: list[str] = Field(default_factory=list)


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"
    webhook_path: str = "/slack/events"
    bot_token: str = ""
    app_token: str = ""
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    done_emoji: str = "white_check_mark"
    allow_from: list[str] = Field(default_factory=list)
    group_policy: str = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode."""

    name = "slack"
    display_name = "Slack"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return SlackConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = SlackConfig.model_validate(config)
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
            logger.error("Unsupported Slack mode: {}", self.config.mode)
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
            logger.info("Slack bot connected as {}", self._bot_user_id)
        except Exception as e:
            logger.warning("Slack auth_test failed: {}", e)

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
                logger.warning("Slack socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._web_client:
            logger.warning("Slack client not running")
            return

        from opentelemetry import trace
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
                # Slack DMs don't use threads; channel/group replies may keep thread_ts.
                thread_ts_param = thread_ts if thread_ts and channel_type != "im" else None

                # Slack rejects empty text payloads. Keep media-only messages media-only,
                # but send a single blank message when the bot has no text or files to send.
                if msg.content or not (msg.media or []):
                    slack_content = self._to_mrkdwn(msg.content) if msg.content else " "

                    # Slack has a 3000 character limit per text block.
                    # Split long messages into multiple blocks for readability.
                    MAX_BLOCK_LENGTH = 3000

                    if len(slack_content) <= MAX_BLOCK_LENGTH:
                        await self._web_client.chat_postMessage(
                            channel=msg.chat_id,
                            text=slack_content,
                            thread_ts=thread_ts_param,
                        )
                    else:
                        blocks = self._split_to_blocks(slack_content, MAX_BLOCK_LENGTH)
                        await self._web_client.chat_postMessage(
                            channel=msg.chat_id,
                            text=slack_content[:MAX_BLOCK_LENGTH],  # fallback text
                            blocks=blocks,
                            thread_ts=thread_ts_param,
                        )

                for media_path in msg.media or []:
                    try:
                        await self._web_client.files_upload_v2(
                            channel=msg.chat_id,
                            file=media_path,
                            thread_ts=thread_ts_param,
                        )
                    except Exception as e:
                        logger.error("Failed to upload file {}: {}", media_path, e)

                # Update reaction emoji when the final (non-progress) response is sent
                if not (msg.metadata or {}).get("_progress"):
                    event = slack_meta.get("event", {})
                    await self._update_react_emoji(msg.chat_id, event.get("ts"))

            except Exception as e:
                logger.error("Error sending Slack message: {}", e)

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
        # Allow file_share subtype through so we can handle image/file uploads
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

        thread_ts = event.get("thread_ts")
        if self.config.reply_in_thread and not thread_ts:
            thread_ts = event.get("ts")

        # Download any attached files (images)
        media_paths = await self._download_slack_files(files) if files else []

        # Add :eyes: reaction to the triggering message (best-effort)
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug("Slack reactions_add failed: {}", e)

        # Thread-scoped session key for channel/group messages
        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts and channel_type != "im" else None

        try:
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
                    },
                },
                session_key=session_key,
            )
        except Exception:
            logger.exception("Error handling Slack message from {}", sender_id)

    async def _update_react_emoji(self, chat_id: str, ts: str | None) -> None:
        """Remove the in-progress reaction and optionally add a done reaction."""
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_remove(
                channel=chat_id,
                name=self.config.react_emoji,
                timestamp=ts,
            )
        except Exception as e:
            logger.debug("Slack reactions_remove failed: {}", e)
        if self.config.done_emoji:
            try:
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.done_emoji,
                    timestamp=ts,
                )
            except Exception as e:
                logger.debug("Slack done reaction failed: {}", e)

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

    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """Convert Markdown to Slack mrkdwn, including tables."""
        if not text:
            return ""
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text))

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """Fix markdown artifacts that slackify_markdown misses."""
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """Convert a Markdown table to a Slack-readable list."""
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)

    @staticmethod
    def _split_to_blocks(text: str, max_length: int = 3000) -> list[dict]:
        """Split long text into Slack section blocks respecting the character limit."""
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_length = 0

        for para in text.split("\n\n"):
            para_len = len(para) + 2  # account for \n\n separator

            if para_len > max_length:
                if current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk, current_length = [], 0

                # Split oversized paragraph by lines
                line_chunk: list[str] = []
                line_length = 0
                for line in para.split("\n"):
                    if line_length + len(line) + 1 > max_length:
                        if line_chunk:
                            chunks.append("\n".join(line_chunk))
                            line_chunk, line_length = [], 0
                        if len(line) > max_length:
                            chunks.append(line[: max_length - 3] + "...")
                        else:
                            line_chunk.append(line)
                            line_length = len(line)
                    else:
                        line_chunk.append(line)
                        line_length += len(line) + 1
                if line_chunk:
                    chunks.append("\n".join(line_chunk))
            elif current_length + para_len > max_length:
                chunks.append("\n\n".join(current_chunk))
                current_chunk, current_length = [para], para_len
            else:
                current_chunk.append(para)
                current_length += para_len

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
            for chunk in chunks[:50]
        ]
        if len(chunks) > 50:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_Message truncated ({len(chunks) - 50} blocks omitted)_"}],
            })
        return blocks

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
