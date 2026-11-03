"""Unit tests for terminal_cleaner module."""

import asyncio
import pytest
from app.terminal_cleaner import (
    ANSICleaner,
    IncrementalExtractor,
    ApprovalDetector,
    WeChatSplitter,
    DebounceBuffer,
)


def test_ansi_cleaner_strip():
    # Test ANSI color escapes
    raw = "\x1b[31;1mError:\x1b[0m \x1b[32mSuccess\x1b[0m\nLine 2"
    cleaned = ANSICleaner.strip_ansi(raw)
    assert cleaned == "Error: Success\nLine 2"

    # Test OSC sequence (e.g., window title)
    raw_osc = "\x1b]0;agy-terminal\x07Working on code"
    assert ANSICleaner.strip_ansi(raw_osc) == "Working on code"


def test_ansi_cleaner_filter_spinners():
    # Test braille spinners
    raw = "⠋ Thinking...\n⠙ Still thinking...\nFinished"
    cleaned = ANSICleaner.filter_spinners(raw)
    assert "Thinking..." in cleaned
    assert "⠋" not in cleaned
    assert "Finished" in cleaned


def test_ansi_cleaner_virtual_screen():
    cleaner = ANSICleaner(cols=40, rows=5)
    cleaner.feed_to_screen("Hello World\r\nLine 2\r\n")
    lines = cleaner.get_screen_lines()
    assert lines == ["Hello World", "Line 2"]


def test_incremental_extractor():
    extractor = IncrementalExtractor()

    # Step 1: initial lines
    diff1 = extractor.extract_diff(["Line 1", "Line 2"])
    assert diff1 == "Line 1\nLine 2"

    # Step 2: appending lines
    diff2 = extractor.extract_diff(["Line 1", "Line 2", "Line 3"])
    assert diff2 == "Line 3"

    # Step 3: no change
    diff3 = extractor.extract_diff(["Line 1", "Line 2", "Line 3"])
    assert diff3 == ""

    # Step 4: line modified in-place
    diff4 = extractor.extract_diff(["Line 1", "Line 2", "Line 3 (updated)"])
    assert diff4 == "Line 3 (updated)"


def test_approval_detector():
    assert ApprovalDetector.is_approval_prompt("Execute command `rm -rf`? [y/N]")
    assert ApprovalDetector.is_approval_prompt("Allow tool execution (y/n): ")
    assert ApprovalDetector.is_approval_prompt("Do you approve this change? [Approve/Reject]")
    assert ApprovalDetector.is_approval_prompt("Continue?")
    assert not ApprovalDetector.is_approval_prompt("Normal logs: build succeeded")


def test_wechat_splitter_short_text():
    splitter = WeChatSplitter(max_length=50)
    text = "Short message"
    assert splitter.split(text) == ["Short message"]


def test_wechat_splitter_markdown_code_block():
    splitter = WeChatSplitter(max_length=60)
    code = (
        "Here is the code:\n"
        "```python\n"
        "def hello():\n"
        "    print('world')\n"
        "    return True\n"
        "```\n"
        "Done!"
    )
    chunks = splitter.split(code)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 75  # Allowing for closing code fence tag
    # Chunk 1 should close code fence
    assert "```" in chunks[0]
    # Chunk 2 should re-open code fence if it split inside
    assert "def hello" in chunks[0] or "def hello" in chunks[1]


@pytest.mark.asyncio
async def test_debounce_buffer_timeout():
    flushed_items = []

    async def on_flush(item: str):
        flushed_items.append(item)

    buffer = DebounceBuffer(debounce_seconds=0.1, max_chunk_chars=1000, on_flush=on_flush)

    await buffer.feed("First part ")
    await buffer.feed("Second part")
    assert len(flushed_items) == 0

    await asyncio.sleep(0.2)
    assert len(flushed_items) == 1
    assert "First part Second part" in flushed_items[0]


@pytest.mark.asyncio
async def test_debounce_buffer_prompt_immediate_flush():
    flushed_items = []

    async def on_flush(item: str):
        flushed_items.append(item)

    buffer = DebounceBuffer(debounce_seconds=1.0, max_chunk_chars=1000, on_flush=on_flush)

    # Feeding an approval prompt should flush immediately without waiting 1.0s
    await buffer.feed("Do you want to proceed? [y/n]")
    assert len(flushed_items) == 1
    assert "[y/n]" in flushed_items[0]


@pytest.mark.asyncio
async def test_debounce_buffer_overflow_flush():
    flushed_items = []

    async def on_flush(item: str):
        flushed_items.append(item)

    buffer = DebounceBuffer(debounce_seconds=1.0, max_chunk_chars=20, on_flush=on_flush)

    await buffer.feed("This message is longer than twenty characters!")
    assert len(flushed_items) >= 1
