"""Claude Code (claude) Driver."""
from typing import List
from .base_driver import BaseCLIAgentDriver

class ClaudeDriver(BaseCLIAgentDriver):
    def __init__(self, binary_path: str = "claude", skip_flag: str = "--dangerously-skip-permissions", default_model: str = "haiku", timeout: int = 60):
        super().__init__("claude", binary_path, skip_flag, default_model, timeout)

    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        # Native binary invocation: claude -p "<prompt>" --dangerously-skip-permissions --max-turns 3 --output-format json
        cmd = [self.binary_path, "-p", prompt]
        if self.skip_flag:
            cmd.extend(self.skip_flag.split())
        cmd.extend(["--max-turns", "3"])
        return cmd
