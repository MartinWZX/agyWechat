"""Terminal stream cleaner, ANSI stripper, and WeChat message splitter."""
import re
from typing import List, Optional, Tuple

ANSI_CSI_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_OSC_REGEX = re.compile(r"\x1B\][^\x07\x1B]*?(?:\x07|\x1B\\)")
CONTROL_CHARS_REGEX = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]")

SPINNER_CHARS = {chr(cp) for cp in range(0x2800, 0x2900)} | set("◐◓◑◒←↖↑↗→↘↓↙")
SPINNER_LINE_REGEX = re.compile(r"^[\u2800-\u28ff◐◓◑◒←↖↑↗→↘↓↙|/\\\-]\s*")


class ANSICleaner:
    """Strips ANSI/VT100 escape sequences and spinner artifacts from CLI output."""

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Strip ANSI escape sequences, OSC title codes, and non-printable control characters."""
        if not text:
            return ""
        text = ANSI_OSC_REGEX.sub("", text)
        text = ANSI_CSI_REGEX.sub("", text)
        return CONTROL_CHARS_REGEX.sub("", text)

    @staticmethod
    def filter_spinners(text: str) -> str:
        """Remove braille spinner characters and solitary spinner progress prefixes."""
        if not text:
            return ""
        cleaned_lines: List[str] = []
        for line in text.split("\n"):
            stripped = SPINNER_LINE_REGEX.sub("", line)
            cleaned_line = "".join(c for c in stripped if c not in SPINNER_CHARS)
            cleaned_lines.append(cleaned_line)
        return "\n".join(cleaned_lines)

    @staticmethod
    def resolve_carriage_returns(text: str) -> str:
        """Resolve standalone carriage returns (\\r without \\n) as in-place line overwrites."""
        if not text:
            return ""
        text = text.replace("\r\n", "\n")
        resolved: List[str] = []
        for line in text.split("\n"):
            if "\r" in line:
                parts = [p for p in line.split("\r") if p.strip()]
                resolved.append(parts[-1] if parts else "")
            else:
                resolved.append(line)
        return "\n".join(resolved)

    @staticmethod
    def dedup_consecutive_lines(text: str) -> str:
        """Collapse repeated progress lines while preserving markdown code blocks."""
        if not text:
            return ""
        lines = text.split("\n")
        deduped: List[str] = []
        last_non_empty: Optional[str] = None
        in_code_block = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                deduped.append(line)
                last_non_empty = None
                continue

            if in_code_block:
                deduped.append(line)
                continue

            if stripped:
                if last_non_empty and stripped.lower() == last_non_empty.lower():
                    continue
                last_non_empty = stripped
                deduped.append(line)
            else:
                if deduped and deduped[-1].strip():
                    deduped.append("")

        return "\n".join(deduped).strip()

    def clean_text(self, text: str) -> str:
        """Strip ANSI codes, resolve carriage returns, filter spinners, and collapse duplicate lines."""
        stripped = self.strip_ansi(text)
        resolved = self.resolve_carriage_returns(stripped)
        filtered = self.filter_spinners(resolved)
        return self.dedup_consecutive_lines(filtered)


class WeChatSplitter:
    """Splits messages exceeding WeChat maximum length, preserving Markdown code fences."""

    def __init__(self, max_length: int = 2000):
        self.max_length = max_length

    def split(self, text: str) -> List[str]:
        """Split text into chunks of at most max_length characters with code-fence protection."""
        if not text:
            return []
        if len(text) <= self.max_length:
            return [text]

        chunks: List[str] = []
        current_text = text

        while len(current_text) > self.max_length:
            target_limit = self.max_length
            in_code_block, lang = self._check_code_block_status(current_text[:target_limit])
            effective_limit = target_limit - 10 if in_code_block else target_limit

            double_nl = current_text.rfind("\n\n", int(effective_limit * 0.4), effective_limit)
            if double_nl != -1:
                split_idx = double_nl + 2
            else:
                single_nl = current_text.rfind("\n", int(effective_limit * 0.4), effective_limit)
                if single_nl != -1:
                    split_idx = single_nl + 1
                else:
                    space_idx = current_text.rfind(" ", int(effective_limit * 0.4), effective_limit)
                    if space_idx != -1:
                        split_idx = space_idx + 1
                    else:
                        split_idx = effective_limit

            chunk = current_text[:split_idx]
            rest = current_text[split_idx:]

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
    def _check_code_block_status(text: str) -> Tuple[bool, str]:
        """Determine if text ends inside an unclosed markdown code fence and return the language tag."""
        matches = list(re.finditer(r"```([a-zA-Z0-9_\-\+]*)\n?", text))
        if len(matches) % 2 != 0:
            last_match = matches[-1]
            lang = last_match.group(1).strip()
            return True, lang
        return False, ""
