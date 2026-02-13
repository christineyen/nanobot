"""Tests for ContextBuilder.build_messages() empty-content sanitisation."""

import struct
import zlib
from pathlib import Path

from nanobot.agent.context import ContextBuilder


def _make_png(path: Path) -> Path:
    """Write a minimal valid 1×1 white PNG to *path* and return it."""
    # IHDR: 1×1 px, 8-bit RGB
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = _png_chunk(b"IHDR", ihdr_data)
    # IDAT: single row, filter byte 0 + 3 bytes RGB
    raw_row = b"\x00\xff\xff\xff"
    idat = _png_chunk(b"IDAT", zlib.compress(raw_row))
    iend = _png_chunk(b"IEND", b"")

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend)
    return path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    body = chunk_type + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


# -- tests -------------------------------------------------------------------


def test_empty_current_message_replaced(tmp_path: Path) -> None:
    """An empty current message should become '[empty message]'."""
    cb = ContextBuilder(tmp_path)
    msgs = cb.build_messages(history=[], current_message="")

    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "[empty message]"


def test_empty_message_with_media_attachment(tmp_path: Path) -> None:
    """An empty current message *with* an image should still be sanitised.

    ``_build_user_content`` collapses a single media block back to the raw
    text (an optimisation for the common case), so the outer guard in
    ``build_messages`` replaces the empty string with ``[empty message]``.
    The important thing is Anthropic never sees ``content: ""``.
    """
    png = _make_png(tmp_path / "photo.png")
    cb = ContextBuilder(tmp_path)
    msgs = cb.build_messages(history=[], current_message="", media=[str(png)])

    user_msg = [m for m in msgs if m["role"] == "user"][-1]
    assert user_msg["content"], "Content must not be empty"
    assert user_msg["content"] == "[empty message]"


def test_empty_content_in_history_sanitized(tmp_path: Path) -> None:
    """Empty-string and None content in history entries must be replaced."""
    cb = ContextBuilder(tmp_path)
    history = [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "I see an image"},
        {"role": "user", "content": None},
        {"role": "assistant", "content": "Interesting"},
    ]
    msgs = cb.build_messages(history=history, current_message="hello")

    # Skip system prompt (index 0)
    non_system = [m for m in msgs if m["role"] != "system"]

    # Every message must have truthy content
    for m in non_system:
        assert m["content"], f"Empty content found in {m['role']} message"

    # The two originally-empty user messages should be replaced
    user_msgs = [m for m in non_system if m["role"] == "user"]
    assert user_msgs[0]["content"] == "[empty message]"
    assert user_msgs[1]["content"] == "[empty message]"
    # The final user message is our actual input
    assert user_msgs[2]["content"] == "hello"
