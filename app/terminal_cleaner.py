"""Terminal stream cleaner, ANSI stripper, approval detector, debounce buffer, and WeChat message splitter."""

import asyncio
import re
from typing import Awaitable, Callable, List, Optional
import pyte


# Regular expressions for stripping ANSI escape sequences
# Matches standard ESC sequences, CSI sequences, and OSC sequences
ANSI_CSI_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_OSC_REGEX = re.compile(r"\x1B\][^\x07\x1B]*?(?:\x07|\x1B\\)")
# Other control characters (excluding \n, \r, \t)
CONTROL_CHARS_REGEX = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]")

# Braille Unicode block \u2800-\u28ff and common ASCII/Unicode progress spinner characters
SPINNER_CHARS = {chr(cp) for cp in range(0x2800, 0x2900)} | set("◐◓◑◒←↖↑↗→↘↓↙")
SPINNER_LINE_REGEX = re.compile(r"^[\u2800-\u28ff◐◓◑◒←↖↑↗→↘↓↙|/\\\-]\s*")

# Patterns indicating interactive approval or confirmation prompts
APPROVAL_PATTERNS = [
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"\[yes/no\]", re.IGNORECASE),
    re.compile(r"\(yes/no\)", re.IGNORECASE),
    re.compile(r"\[y/n/c\]", re.IGNORECASE),
    re.compile(r"\b(approve|reject|allow|deny)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\?\s*$", re.IGNORECASE),
    re.compile(r"\bdo you want to (proceed|continue)\?", re.IGNORECASE),
    re.compile(r"press\s+(enter|return)\s+to\s+continue", re.IGNORECASE),
]


class ANSICleaner:
    """Strips ANSI/VT100 escape codes and filters spinner animation frames."""

    def __init__(self, cols: int = 120, rows: int = 40):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.HistoryScreen(cols, rows, history=5000)
        self.stream = pyte.Stream(self.screen)

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Strip ANSI escape sequences, OSC title codes, and non-printable control characters."""
        if not text:
            return ""
        # Remove OSC sequences first
        text = ANSI_OSC_REGEX.sub("", text)
        # Remove CSI and basic escapes
        text = ANSI_CSI_REGEX.sub("", text)
        # Remove control characters except tab and newline
        text = CONTROL_CHARS_REGEX.sub("", text)
        return text

    @staticmethod
    def filter_spinners(text: str) -> str:
        """Remove braille spinner characters and solitary spinner progress lines."""
        if not text:
            return ""
        lines = text.split("\n")
        cleaned_lines = []
        for line in lines:
            stripped = SPINNER_LINE_REGEX.sub("", line)
            # Remove isolated braille characters
            filtered_chars = [c for c in stripped if c not in SPINNER_CHARS]
            cleaned_line = "".join(filtered_chars)
            cleaned_lines.append(cleaned_line)
        return "\n".join(cleaned_lines)

    def feed_to_screen(self, data: str) -> None:
        """Feed data into pyte virtual screen."""
        self.stream.feed(data)

    def get_screen_lines(self) -> List[str]:
        """Retrieve all active history and display lines from the virtual screen."""
        lines: List[str] = []
        for row in self.screen.history.top:
            line_str = "".join(row[c].data for c in range(self.cols)).rstrip()
            lines.append(line_str)
        for row in self.screen.display:
            lines.append(row.rstrip())
        # Trim trailing empty lines
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def clean_text(self, text: str) -> str:
        """Full pipeline: strip ANSI and remove spinner artifacts."""
        return self.filter_spinners(self.strip_ansi(text))


class IncrementalExtractor:
    """Extracts incremental diffs between screen states to avoid duplicate output."""

    def __init__(self):
        self._last_snapshot: List[str] = []

    def extract_diff(self, current_lines: List[str]) -> str:
        """Compare current_lines with last snapshot and return newly added/changed content."""
        if not current_lines:
            return ""

        # Find common prefix length
        common_len = 0
        min_len = min(len(self._last_snapshot), len(current_lines))
        for i in range(min_len):
            if self._last_snapshot[i] == current_lines[i]:
                common_len += 1
            else:
                break

        # If lines changed at common_len, we emit from common_len onwards
        new_lines = current_lines[common_len:]
        self._last_snapshot = list(current_lines)

        if not new_lines:
            return ""

        # Remove consecutive duplicate lines
        deduped: List[str] = []
        for line in new_lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)

        return "\n".join(deduped)

    def reset(self) -> None:
        """Reset snapshot state."""
        self._last_snapshot.clear()


class ApprovalDetector:
    """Detects interactive approval/confirmation prompts in terminal output."""

    @classmethod
    def is_approval_prompt(cls, text: str) -> bool:
        """Check if text contains any approval patterns, especially near the end."""
        if not text:
            return False

        # Check last 500 characters / last few lines for prompts
        tail = text[-500:] if len(text) > 500 else text
        for pattern in APPROVAL_PATTERNS:
            if pattern.search(tail):
                return True
        return False


class WeChatSplitter:
    """Splits messages exceeding WeChat maximum length, preserving Markdown code fences."""

    def __init__(self, max_length: int = 2000):
        self.max_length = max_length

    def split(self, text: str) -> List[str]:
        """Split text into chunks of at most max_length characters with code-block protection."""
        if not text:
            return []
        if len(text) <= self.max_length:
            return [text]

        chunks: List[str] = []
        current_text = text

        while len(current_text) > self.max_length:
            target_limit = self.max_length

            # Check if split boundary falls inside a code block
            in_code_block, lang = self._check_code_block_status(current_text[:target_limit])

            split_idx = -1
            # Search for best split point before target_limit
            # Leave room for closing code block if needed
            effective_limit = target_limit - 10 if in_code_block else target_limit

            # Preference 1: Paragraph break (\n\n)
            double_nl = current_text.rfind("\n\n", int(effective_limit * 0.4), effective_limit)
            if double_nl != -1:
                split_idx = double_nl + 2
            else:
                # Preference 2: Single line break (\n)
                single_nl = current_text.rfind("\n", int(effective_limit * 0.4), effective_limit)
                if single_nl != -1:
                    split_idx = single_nl + 1
                else:
                    # Preference 3: Sentence boundary or whitespace
                    space_idx = current_text.rfind(" ", int(effective_limit * 0.4), effective_limit)
                    if space_idx != -1:
                        split_idx = space_idx + 1
                    else:
                        # Fallback: Hard cut
                        split_idx = effective_limit

            chunk = current_text[:split_idx]
            rest = current_text[split_idx:]

            # If inside code block, close current chunk and reopen next
            if in_code_block:
                chunk = chunk.rstrip("\n") + "\n```"
                prefix = f"```{lang}\n" if lang else "```\n"
                rest = prefix + rest.lstrip("\n")

            chunks.append(chunk)
            current_text = rest

        if current_text:
            chunks.append(current_text)

        return chunks

    @staticmethod
    def _check_code_block_status(text: str) -> tuple[bool, str]:
        """Determine if text ends inside an unclosed markdown code fence and return the language tag."""
        # Find all triple backticks
        matches = list(re.finditer(r"```([a-zA-Z0-9_\-\+]*)\n?", text))
        if len(matches) % 2 != 0:
            last_match = matches[-1]
            lang = last_match.group(1).strip()
            return True, lang
        return False, ""


class DebounceBuffer:
    """Buffers streaming terminal output, debounces by time or size, and triggers flushes."""

    def __init__(
        self,
        debounce_seconds: float = 1.5,
        max_chunk_chars: int = 1500,
        wechat_max_len: int = 2000,
        on_flush: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.debounce_seconds = debounce_seconds
        self.max_chunk_chars = max_chunk_chars
        self.on_flush = on_flush
        self.splitter = WeChatSplitter(max_length=wechat_max_len)
        self.cleaner = ANSICleaner()
        self.extractor = IncrementalExtractor()
        self._buffer: str = ""
        self._timer_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def feed(self, raw_data: str) -> None:
        """Feed raw terminal output string into buffer with debounce & prompt detection."""
        if not raw_data:
            return

        cleaned = self.cleaner.clean_text(raw_data)
        if not cleaned:
            return

        async with self._lock:
            self._buffer += cleaned
            is_prompt = ApprovalDetector.is_approval_prompt(self._buffer)
            is_overflow = len(self._buffer) >= self.max_chunk_chars

        if is_prompt or is_overflow:
            await self.flush()
        else:
            self._schedule_debounce()

    def _schedule_debounce(self) -> None:
        """Cancel previous timer and schedule a new debounce flush."""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._debounce_worker())

    async def _debounce_worker(self) -> None:
        """Wait for debounce timeout and flush."""
        try:
            await asyncio.sleep(self.debounce_seconds)
            await self.flush()
        except asyncio.CancelledError:
            pass

    async def flush(self) -> None:
        """Flush buffered content to the on_flush callback."""
        # Cancel any pending timer
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        text_to_send: str = ""
        async with self._lock:
            if not self._buffer:
                return
            text_to_send = self._buffer
            self._buffer = ""

        if not text_to_send.strip():
            return

        chunks = self.splitter.split(text_to_send)
        if self.on_flush:
            for chunk in chunks:
                if chunk.strip():
                    await self.on_flush(chunk)

    async def close(self) -> None:
        """Close buffer and flush remaining data."""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        await self.flush()
