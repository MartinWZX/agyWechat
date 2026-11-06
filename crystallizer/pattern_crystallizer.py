"""Interaction Pattern Mining & Shadow-Dry-Run Crystallizer."""
import re
import os
import time
import hashlib
from typing import Dict, Any, Tuple, Optional
from memory.core_state_store import CoreStateStore
from verifier.hallucination_guard import ShadowDryRunValidator

class PatternCrystallizer:
    def __init__(self, db_path: str = "memory/agent_core.db", workspace_dir: str = "."):
        self.store = CoreStateStore(db_path)
        self.workspace_dir = workspace_dir
        self.scripts_dir = os.path.join(workspace_dir, "skills", "scripts")

    def extract_fingerprint(self, text: str) -> str:
        # Strip numbers, spaces, punctuation to extract structural sentence skeleton
        skeleton = re.sub(r"[0-9\s+\-_,.!?:：，。？！]", "", text)
        return hashlib.sha256(skeleton.encode("utf-8")).hexdigest()[:16]

    def record_interaction(self, text: str) -> Tuple[bool, int]:
        fp = self.extract_fingerprint(text)
        now = time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT occurrence_count FROM interaction_patterns WHERE fingerprint = ?;", (fp,))
            row = c.fetchone()
            if row:
                count = row["occurrence_count"] + 1
                c.execute("UPDATE interaction_patterns SET occurrence_count = ?, last_seen = ? WHERE fingerprint = ?;", (count, now, fp))
            else:
                count = 1
                c.execute("INSERT INTO interaction_patterns (fingerprint, raw_sample, occurrence_count, last_seen, crystallized) VALUES (?, ?, 1, ?, 0);", (fp, text, now))
            conn.commit()
            return (count >= 3), count

    def crystallize_from_response(self, text_response: str) -> Optional[str]:
        # Extract <crystallize_script name="..." ...>...</crystallize_script>
        match = re.search(r'<crystallize_script\s+name="([^"]+)"(?:\s+trigger_regex="([^"]*)")?(?:\s+voice_homophones="([^"]*)")?>\s*(.*?)\s*</crystallize_script>', text_response, re.DOTALL)
        if not match:
            return None
        
        script_name = match.group(1).strip()
        trigger_regex = match.group(2) or ""
        voice_homophones = match.group(3) or ""
        script_code = match.group(4).strip()

        # Shadow dry-run validation
        try:
            ShadowDryRunValidator.validate_script(script_code)
        except Exception as e:
            self.store.log_hallucination("crystallize", "all", "SHADOW_DRY_RUN_FAILED", str(e))
            return None

        # Write to skills/scripts/<script_name>.py
        os.makedirs(self.scripts_dir, exist_ok=True)
        script_path = os.path.join(self.scripts_dir, f"{script_name}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_code)

        # Register in crystallized_rules
        with self.store._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO crystallized_rules (rule_name, pattern_regex, voice_homophones, script_path, hit_count, created_at)
            VALUES (?, ?, ?, ?, 0, ?);
            """, (script_name, trigger_regex, voice_homophones, script_path, time.time()))
            conn.commit()

        return script_path
