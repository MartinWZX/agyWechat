"""Progressive 4-Level Skill Manager with Out-of-Process Sandbox Execution & Symlink Sync."""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from sandbox.runner import SandboxExecutionError, run_file_async
from verifier.hallucination_guard import ShadowDryRunError, ShadowDryRunValidator


class SkillManager:
    """Manages Level 0-3 progressive skill hierarchy and multi-agent spec synchronization."""

    def __init__(
        self,
        workspace_root: Optional[Union[str, Path]] = None,
        workspace_dir: Optional[str] = None,
        settings: Any = None
    ):
        base_dir = workspace_root or workspace_dir or getattr(settings, "WORKSPACE_ROOT", ".")
        self.workspace_root = Path(base_dir).resolve()
        self.settings = settings

        self.l0_scripts_dir = self.workspace_root / "skills" / "scripts"
        self.l2_skills_dir = self.workspace_root / ".agents" / "skills"

        self.l0_scripts_dir.mkdir(parents=True, exist_ok=True)
        self.l2_skills_dir.mkdir(parents=True, exist_ok=True)

    def save_l0_script(self, filename: str, code: str) -> Path:
        """Validate and save a deterministic Level 0 pure-code Python script."""
        ShadowDryRunValidator.validate_script(code)
        script_path = self.l0_scripts_dir / filename
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(code.strip() + "\n", encoding="utf-8")
        return script_path

    async def execute_l0_script(self, script_path: Union[str, Path], **kwargs: Any) -> Any:
        """Execute a Level 0 script in the isolated out-of-process sandbox via its `run` entrypoint."""
        path_obj = Path(script_path)
        if not path_obj.exists():
            raise FileNotFoundError(f"L0 script '{path_obj}' does not exist.")

        res = await run_file_async(str(path_obj), entrypoint="run", kwargs=kwargs, timeout=15.0)
        if not res.ok:
            raise SandboxExecutionError(
                f"L0 script '{path_obj.name}' failed in sandbox: {res.error_summary()}"
            )
        return res.result

    def list_l0_scripts(self) -> List[str]:
        """List all Level 0 pure-code scripts in skills/scripts/."""
        if not self.l0_scripts_dir.exists():
            return []
        return sorted(p.name for p in self.l0_scripts_dir.glob("*.py") if p.name != "__init__.py")

    def list_l2_skills(self) -> List[Dict[str, str]]:
        """List Level 2/3 skill summaries from .agents/skills/."""
        skills: List[Dict[str, str]] = []
        if not self.l2_skills_dir.exists():
            return skills
        for folder in self.l2_skills_dir.iterdir():
            if folder.is_dir():
                skill_md = folder / "SKILL.md"
                if skill_md.exists():
                    skills.append({"name": folder.name, "path": str(skill_md)})
        return sorted(skills, key=lambda x: x["name"])

    def apply_skill_patch(
        self,
        skill_name: str,
        version: str,
        skill_md_content: str,
        script_code: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Precipitate or upgrade a skill into .agents/skills/<skill_name>/SKILL.md."""
        if script_code:
            try:
                ShadowDryRunValidator.validate_script(script_code)
            except ShadowDryRunError as e:
                return False, f"Skill script dry-run failed: {e}"
            self.save_l0_script(f"{skill_name}.py", script_code)

        target_dir = self.l2_skills_dir / skill_name
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path = target_dir / "SKILL.md"
        md_path.write_text(skill_md_content.strip() + "\n", encoding="utf-8")

        return True, f"Skill '{skill_name}' successfully precipitated at version {version}."

    def sync_global_agent_rules(self) -> None:
        """Synchronize AGENTS.md rules with GEMINI.md and CLAUDE.md."""
        agents_md = self.workspace_root / "AGENTS.md"
        if not agents_md.exists():
            return
        content = agents_md.read_text(encoding="utf-8")
        (self.workspace_root / "GEMINI.md").write_text(content, encoding="utf-8")
        (self.workspace_root / "CLAUDE.md").write_text(content, encoding="utf-8")
