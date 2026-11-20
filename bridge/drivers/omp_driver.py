"""Oh-My-Pi (omp) Native CLI Driver."""
from typing import List

from .base_driver import BaseCLIAgentDriver


class OmpDriver(BaseCLIAgentDriver):
    """Native Oh-My-Pi CLI driver."""

    supports_json_output = False
    conversation_flag = ""

    def __init__(
        self,
        binary_path: str = "omp",
        skip_flag: str = "--dangerously-skip-permissions --yes",
        default_model: str = "inherit",
        timeout: int = 0,
        max_turns: int = 0,
    ):
        super().__init__(
            "omp",
            binary_path,
            skip_flag,
            default_model,
            timeout,
            max_turns=max_turns,
        )

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        cmd = [self.binary_path, "run"]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["-p", prompt])
        return cmd
