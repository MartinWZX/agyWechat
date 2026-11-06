"""Progressive 4-Level Skill Manager with Symlink Sync & Precipitation."""
import glob
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from verifier.hallucination_guard import ShadowDryRunValidator, ShadowDryRunError


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
        """Save a deterministic Level 0 pure-code Python script."""
        script_path = self.l0_scripts_dir / filename
        script_path.parent.mkdir(parents=True, exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code.strip() + "\n")
        return script_path

    async def execute_l0_script(self, script_path: Union[str, Path], **kwargs) -> Any:
        """Execute a Level 0 script dynamically by running its 'run' function."""
        path_obj = Path(script_path)
        if not path_obj.exists():
            raise FileNotFoundError(f"L0 script '{path_obj}' does not exist.")

        with open(path_obj, "r", encoding="utf-8") as f:
            code = f.read()

        scope: Dict[str, Any] = {"__builtins__": __builtins__}
        exec(code, scope)

        run_func = scope.get("run")
        if not run_func or not callable(run_func):
            raise AttributeError(f"L0 script '{path_obj.name}' must define a callable 'run(**kwargs)' entrypoint.")

        return run_func(**kwargs)

    def list_l0_scripts(self) -> List[str]:
        """List all Level 0 pure-code scripts in skills/scripts/."""
        if not self.l0_scripts_dir.exists():
            return []
        return [p.name for p in self.l0_scripts_dir.glob("*.py") if p.name != "__init__.py"]

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
        target_dir = self.l2_skills_dir / skill_name
        target_dir.mkdir(parents=True, exist_ok=True)

        md_path = target_dir / "SKILL.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(skill_md_content.strip() + "\n")

        if script_code:
            try:
                ShadowDryRunValidator.validate_script(script_code)
            except ShadowDryRunError as e:
                return False, f"Skill script dry-run failed: {e}"

            script_filename = f"{skill_name}.py"
            self.save_l0_script(script_filename, script_code)

        return True, f"Skill '{skill_name}' successfully precipitated at version {version}."

    def sync_global_agent_rules(self):
        """Synchronize AGENTS.md rules with GEMINI.md and CLAUDE.md."""
        agents_md = self.workspace_root / "AGENTS.md"
        if not agents_md.exists():
            return

        gemini_md = self.workspace_root / "GEMINI.md"
        claude_md = self.workspace_root / "CLAUDE.md"

        # Mirror contents cleanly
        content = agents_md.read_text(encoding="utf-8")
        gemini_md.write_text(content, encoding="utf-8")
        claude_md.write_text(content, encoding="utf-8")
