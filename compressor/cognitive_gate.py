import re
"""Cognitive Gate, Pre-Flight Provenance Sanity Gate & Context Compressor."""
import hashlib
import json
from enum import Enum
from typing import Dict, Any, List, Tuple, Optional

class InvocationReason(Enum):
    INFO_SYNTHESIS = "INFO_SYNTHESIS"
    INFO_REASONING = "INFO_REASONING"
    INFO_JUDGMENT = "INFO_JUDGMENT"

class PreInferenceDataValidator:
    @staticmethod
    def validate_source_data(data: Dict[str, Any]) -> Tuple[bool, str]:
        if not data:
            return False, "Data is empty or null."
        if data.get("http_status") and data.get("http_status") != 200:
            return False, f"Upstream HTTP error status: {data.get('http_status')}"
        if data.get("is_empty_or_corrupt"):
            return False, "Data flagged as corrupt or empty."
        return True, "Data sanity check passed."

    @staticmethod
    def validate_wechat_voice(recognition_text: Optional[str], min_chars: int = 2) -> Tuple[bool, str]:
        if not recognition_text or len(recognition_text.strip()) < min_chars:
            return False, "WeChat voice recognition result is empty or too short."
        # Check if recognition text is only meaningless garbled noises
        clean = recognition_text.strip()
        if re.match(r"^[啊哦呃嗯呀吧哈？!！。，\s]+$", clean):
            return False, f"Voice transcript contains only filler interjections: '{clean}'"
        return True, "Voice recognition text sanity check passed."

class CognitiveGate:
    def __init__(self, data_validator: PreInferenceDataValidator = None):
        self.validator = data_validator or PreInferenceDataValidator()

    def build_anchor_manifest(self, raw_data_items: Dict[str, Any]) -> Dict[str, Any]:
        manifest = {"anchors": {}, "manifest_hash": ""}
        combined_str = ""
        for idx, (k, v) in enumerate(raw_data_items.items(), 1):
            anchor_id = f"ANCHOR_{idx}_{k}"
            manifest["anchors"][anchor_id] = v
            combined_str += f"{anchor_id}:{json.dumps(v, sort_keys=True)};"
        manifest["manifest_hash"] = hashlib.sha256(combined_str.encode("utf-8")).hexdigest()
        return manifest

    def compress_context(self, task_instruction: str, manifest: Dict[str, Any]) -> str:
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
        compressed_prompt = f"""[TASK_INSTRUCTION]
{task_instruction.strip()}

[DATA_ANCHOR_MANIFEST]
{manifest_text}

[MANDATORY_OUTPUT_CONTRACT]
You MUST respond strictly using the following 4 tags:
<facts_anchor>
(List exact ANCHOR_ID and raw values cited)
</facts_anchor>
<causal_chain>
(Step-by-step logic citing ANCHOR_IDs without contradiction)
</causal_chain>
<execution_manifest>
{{"files_created": [], "files_modified": [], "commands_executed": []}}
</execution_manifest>
<final_conclusion>
(User-facing concise summary)
</final_conclusion>
"""
        return compressed_prompt
