"""Google Antigravity (agy) Native CLI Driver."""
from typing import List

from .base_driver import BaseCLIAgentDriver


class AgyDriver(BaseCLIAgentDriver):
    """Native Google Antigravity CLI driver."""

    supports_json_output = True
    conversation_flag = "--conversation"
    supported_flags = ("--model", "--mode", "--effort", "--agent", "--add-dir", "--sandbox", "--project")

    def __init__(
        self,
        binary_path: str = "agy",
        skip_flag: str = "--dangerously-skip-permissions",
        default_model: str = "inherit",
        timeout: int = 0,
        max_turns: int = 0,
    ):
        super().__init__(
            "agy",
            binary_path,
            skip_flag,
            default_model,
            timeout,
            max_turns=max_turns,
        )

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        cmd = [self.binary_path]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["-p", prompt])
        return cmd
