"""Claude Code (claude) Native CLI Driver."""
from typing import List

from .base_driver import BaseCLIAgentDriver


class ClaudeDriver(BaseCLIAgentDriver):
    """Native Claude Code CLI driver."""

    supports_json_output = True
    conversation_flag = "--resume"

    def __init__(
        self,
        binary_path: str = "claude",
        skip_flag: str = "--dangerously-skip-permissions",
        default_model: str = "inherit",
        timeout: int = 0,
        max_turns: int = 0,
    ):
        super().__init__(
            "claude",
            binary_path,
            skip_flag,
            default_model,
            timeout,
            max_turns=max_turns,
        )

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        cmd = [self.binary_path, "-p", prompt]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        if self.max_turns and self.max_turns > 0:
            cmd.extend(["--max-turns", str(self.max_turns)])
        return cmd
