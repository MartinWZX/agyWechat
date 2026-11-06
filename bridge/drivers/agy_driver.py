"""Google Antigravity (agy) Driver."""
from typing import List
from .base_driver import BaseCLIAgentDriver

class AgyDriver(BaseCLIAgentDriver):
    def __init__(self, binary_path: str = "agy", skip_flag: str = "--dangerously-skip-permissions", default_model: str = "flash", timeout: int = 60):
        super().__init__("agy", binary_path, skip_flag, default_model, timeout)

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        # Native binary invocation: agy --dangerously-skip-permissions -p "<prompt>"
        cmd = [self.binary_path]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["-p", prompt])
        return cmd
