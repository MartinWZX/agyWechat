"""Pi Coding Agent (pi) Driver."""
from typing import Iterable, List, Optional
from .base_driver import BaseCLIAgentDriver


class PiDriver(BaseCLIAgentDriver):
    def __init__(
        self,
        binary_path: str = "pi",
        skip_flag: str = "--no-confirm",
        default_model: str = "light",
        timeout: int = 45,
        max_turns: int = 3,
        lock_file: Optional[str] = None,
        env_passthrough: Optional[Iterable[str]] = None,
    ):
        super().__init__(
            "pi", binary_path, skip_flag, default_model, timeout,
            max_turns=max_turns, lock_file=lock_file, env_passthrough=env_passthrough,
        )

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        cmd = [self.binary_path, "-p", prompt]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        return cmd
