"""Progressive 4-Level Skill Manager with Symlink Sync."""
import os
import glob
from typing import Dict, Any, List

class SkillManager:
    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = workspace_dir
        self.l0_scripts_dir = os.path.join(workspace_dir, "skills", "scripts")
        self.l2_skills_dir = os.path.join(workspace_dir, ".agents", "skills")

    def list_l0_scripts(self) -> List[str]:
        if not os.path.exists(self.l0_scripts_dir):
            return []
        return [os.path.basename(f) for f in glob.glob(os.path.join(self.l0_scripts_dir, "*.py"))]

    def list_l2_skills(self) -> List[Dict[str, str]]:
        skills = []
        if not os.path.exists(self.l2_skills_dir):
            return skills
        for folder in os.listdir(self.l2_skills_dir):
            skill_md = os.path.join(self.l2_skills_dir, folder, "SKILL.md")
            if os.path.exists(skill_md):
                skills.append({"name": folder, "path": skill_md})
        return skills
