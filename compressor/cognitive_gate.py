"""Cognitive Gate, Pre-Flight Provenance Sanity Gate & Context Compressor."""
import hashlib
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class InvocationReason(str, Enum):
    """Permitted reasons for waking up probabilistic LLM CLIs."""
    INFO_SYNTHESIS = "INFO_SYNTHESIS"
    INFO_REASONING = "INFO_REASONING"
    INFO_JUDGMENT = "INFO_JUDGMENT"


class PreFlightGate:
    """Hardcoded pre-flight gate ensuring deterministic tasks are physically blocked from LLM CLIs."""

    DETERMINISTIC_PREFIXES = [
        "#状态", "状态", "查看状态", "系统状态",
        "#风控看板", "风控看板", "#风控",
        "#幻觉审计", "幻觉审计",
        "#help", "帮助", "使用说明"
    ]

    @classmethod
    def validate_admission(
        cls,
        reason: InvocationReason,
        user_query: str,
        data_payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """Validate if task genuinely requires cognitive reasoning or is strictly deterministic."""
        clean_query = user_query.strip()
        for prefix in cls.DETERMINISTIC_PREFIXES:
            if clean_query == prefix or clean_query.startswith(prefix + " "):
                return False, f"Task '{clean_query}' is strictly deterministic (L0 Code). Bypassing LLM."

        if not isinstance(reason, InvocationReason):
            return False, f"Invalid InvocationReason: {reason}."

        return True, f"Admission approved for cognitive reasoning: {reason.value}."


class PreInferenceDataValidator:
    """Zero-trust validation for upstream data schemas and WeChat voice transcripts."""

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
        clean = recognition_text.strip()
        if re.match(r"^[啊哦呃嗯呀吧哈？!！。，\s]+$", clean):
            return False, f"Voice transcript contains only filler interjections: '{clean}'"
        return True, "Voice recognition text sanity check passed."


class ContextCompressor:
    """Token-minimized prompt assembly with strict anti-hallucination and auto-crystallization contracts."""

    @staticmethod
    def assemble_minimal_prompt(
        reason: InvocationReason,
        task_instruction: str,
        data_payload: Dict[str, Any],
        tools_summary: Optional[str] = None
    ) -> str:
        anchor_data = {}
        for idx, (k, v) in enumerate(data_payload.items(), 1):
            anchor_data[f"ANCHOR_{idx}_{k}"] = v

        manifest_json = json.dumps(anchor_data, ensure_ascii=False, indent=2)

        prompt = f"""[TASK_REASON: {reason.value}]

[TASK_INSTRUCTION]
{task_instruction.strip()}

[DATA_ANCHOR_MANIFEST]
{manifest_json}

[AVAILABLE_TOOLS]
{tools_summary or "Standard deterministic tools: read_file, write_file, system_metrics"}

[MANDATORY_OUTPUT_CONTRACT]
You MUST respond using strictly the following 4 tags:
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

[RSI_AUTO_CRYSTALLIZATION]
If this task represents a recurring query or routine data check, append:
<crystallize_script name="..." trigger_regex="..." voice_homophones="...">
# 100% deterministic Python code with run() entrypoint
</crystallize_script>
"""
        return prompt


class CognitiveGate:
    """Unified gate providing anchor manifest creation and context compression."""

    def __init__(self, data_validator: Optional[PreInferenceDataValidator] = None):
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
