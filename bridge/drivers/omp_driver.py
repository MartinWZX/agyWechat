"""Oh-My-Pi (omp) Driver."""
from typing import List
from .base_driver import BaseCLIAgentDriver

class OmpDriver(BaseCLIAgentDriver):
    def __init__(self, binary_path: str = "omp", skip_flag: str = "--dangerously-skip-permissions --yes", default_model: str = "default", timeout: int = 60):
        super().__init__("omp", binary_path, skip_flag, default_model, timeout)

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        # Native binary invocation: omp run --dangerously-skip-permissions --yes -p "<prompt>"
        cmd = [self.binary_path, "run"]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["-p", prompt])
        return cmd
