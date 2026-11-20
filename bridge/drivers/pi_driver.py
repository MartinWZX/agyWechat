"""Pi Coding Agent (pi) Native CLI Driver."""
from typing import List

from .base_driver import BaseCLIAgentDriver


class PiDriver(BaseCLIAgentDriver):
    """Native Pi Coding Agent CLI driver."""

    supports_json_output = False
    conversation_flag = ""
    supported_flags = ("--model",)

    def __init__(
        self,
        binary_path: str = "pi",
        skip_flag: str = "--no-confirm",
        default_model: str = "inherit",
        timeout: int = 0,
        max_turns: int = 0,
    ):
        super().__init__(
            "pi",
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
        return cmd
