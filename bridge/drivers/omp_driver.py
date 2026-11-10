"""Oh-My-Pi (omp) Driver."""
from typing import Iterable, List, Optional
from .base_driver import BaseCLIAgentDriver


class OmpDriver(BaseCLIAgentDriver):
    def __init__(
        self,
        binary_path: str = "omp",
        skip_flag: str = "--dangerously-skip-permissions --yes",
        default_model: str = "default",
        timeout: int = 60,
        max_turns: int = 3,
        lock_file: Optional[str] = None,
        env_passthrough: Optional[Iterable[str]] = None,
    ):
        super().__init__(
            "omp", binary_path, skip_flag, default_model, timeout,
            max_turns=max_turns, lock_file=lock_file, env_passthrough=env_passthrough,
        )

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        cmd = [self.binary_path, "run"]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["-p", prompt])
        return cmd
