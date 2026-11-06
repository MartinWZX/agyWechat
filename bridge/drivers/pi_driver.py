"""Pi Coding Agent (pi) Driver."""
from typing import List
from .base_driver import BaseCLIAgentDriver

class PiDriver(BaseCLIAgentDriver):
    def __init__(self, binary_path: str = "pi", skip_flag: str = "--no-confirm", default_model: str = "light", timeout: int = 45):
        super().__init__("pi", binary_path, skip_flag, default_model, timeout)

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        # Native binary invocation: pi -p "<prompt>" --no-confirm
        cmd = [self.binary_path, "-p", prompt]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        return cmd
