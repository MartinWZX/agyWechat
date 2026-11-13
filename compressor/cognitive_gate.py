"""Cognitive Gate, Pre-Flight Provenance Sanity Gate & Context Builder/Compressor."""
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
        "/status",
        "/security", "/defense", "/dashboard",
        "/audit",
        "/rules", "/evolution", "/rsi",
        "/engines",
        "/help",
    ]

    @classmethod
    def validate_admission(
        cls,
        reason: InvocationReason,
        user_query: str,
        data_payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Validate if task genuinely requires cognitive reasoning or is strictly deterministic."""
        raw_query = user_query.strip()
        clean_query = raw_query.lower()
        for prefix in cls.DETERMINISTIC_PREFIXES:
            if clean_query == prefix or clean_query.startswith(prefix + " "):
                return False, f"Task '{raw_query}' is strictly deterministic (L0 Code). Bypassing LLM."

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
        tools_summary: Optional[str] = None,
    ) -> str:
        anchor_data = {f"ANCHOR_{idx}_{k}": v for idx, (k, v) in enumerate(data_payload.items(), 1)}
        manifest_json = json.dumps(anchor_data, ensure_ascii=False, indent=2)
        return (
            f"[TASK_REASON: {reason.value}]\n\n"
            f"[TASK_INSTRUCTION]\n{task_instruction.strip()}\n\n"
            f"[DATA_ANCHOR_MANIFEST]\n{manifest_json}\n\n"
            f"[AVAILABLE_TOOLS]\n"
            f"{tools_summary or 'Standard deterministic tools: read_file, write_file, system_metrics'}\n\n"
            f"[MANDATORY_OUTPUT_CONTRACT]\n"
            f"You MUST respond using strictly the following 4 tags:\n"
            f"<facts_anchor>\n(List exact ANCHOR_ID and raw values cited)\n</facts_anchor>\n"
            f"<causal_chain>\n(Step-by-step logic citing ANCHOR_IDs without contradiction)\n</causal_chain>\n"
            f"<execution_manifest>\n"
            f'{{"files_created": [], "files_modified": [], "commands_executed": []}}\n'
            f"</execution_manifest>\n"
            f"<final_conclusion>\n(User-facing concise summary)\n</final_conclusion>\n\n"
            f"[RSI_AUTO_CRYSTALLIZATION]\n"
            f"If this task represents a recurring query or routine data check, append:\n"
            f'<crystallize_script name="..." trigger_regex="..." voice_homophones="...">\n'
            f"# 100% deterministic Python code with run() entrypoint\n"
            f"</crystallize_script>\n"
        )


class CognitiveGate:
    """Unified gate providing anchor manifest creation, memory injection, and context compression."""

    def __init__(self, data_validator: Optional[PreInferenceDataValidator] = None):
        self.validator = data_validator or PreInferenceDataValidator()

    def build_anchor_manifest(self, raw_data_items: Dict[str, Any]) -> Dict[str, Any]:
        manifest: Dict[str, Any] = {"anchors": {}, "manifest_hash": ""}
        combined_str = ""
        for idx, (k, v) in enumerate(raw_data_items.items(), 1):
            anchor_id = f"ANCHOR_{idx}_{k}"
            manifest["anchors"][anchor_id] = v
            combined_str += f"{anchor_id}:{json.dumps(v, sort_keys=True, ensure_ascii=False)};"
        manifest["manifest_hash"] = hashlib.sha256(combined_str.encode("utf-8")).hexdigest()
        return manifest

    def compress_context(
        self,
        task_instruction: str,
        manifest: Dict[str, Any],
        session_history: Optional[List[Dict[str, str]]] = None,
        episodic_memories: Optional[List[Dict[str, Any]]] = None,
        tools_summary: Optional[str] = None,
    ) -> str:
        stripped_task = task_instruction.strip()
        sections: List[str] = [
            stripped_task if stripped_task.startswith("/") else f"[TASK_INSTRUCTION]\n{stripped_task}"
        ]

        if session_history:
            trimmed = [
                f"- Q: {item['q'][:160]} -> A: {item['a'][:200]}"
                for item in session_history[-3:]
                if item.get("q")
            ]
            if trimmed:
                sections.append("[RECENT_SESSION_WINDOW]\n" + "\n".join(trimmed))

        if episodic_memories:
            mem_lines = [
                f"- [{m.get('key', 'mem')}]: {str(m.get('content', ''))[:200]}"
                for m in episodic_memories[:3]
            ]
            if mem_lines:
                sections.append("[RECALLED_EPISODIC_MEMORY]\n" + "\n".join(mem_lines))

        if tools_summary:
            sections.append(f"[AVAILABLE_TOOLS]\n{tools_summary}")

        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
        sections.append(f"[DATA_ANCHOR_MANIFEST]\n{manifest_text}")
        sections.append(
            "[MANDATORY_OUTPUT_CONTRACT]\n"
            "You MUST respond strictly using the following 4 tags:\n"
            "<facts_anchor>\n(List exact ANCHOR_ID and raw values cited)\n</facts_anchor>\n"
            "<causal_chain>\n(Step-by-step logic citing ANCHOR_IDs without contradiction)\n</causal_chain>\n"
            "<execution_manifest>\n"
            '{"files_created": [], "files_modified": [], "commands_executed": []}\n'
            "</execution_manifest>\n"
            "<final_conclusion>\n(User-facing concise summary)\n</final_conclusion>"
        )
        return "\n\n".join(sections) + "\n"

    def compress_coding_context(
        self,
        task_instruction: str,
        workspace_dir: str = "",
        session_history: Optional[List[Dict[str, str]]] = None,
        has_native_session: bool = False,
    ) -> str:
        """Build clean interactive coding prompt preserving leading /slash and native thread state."""
        stripped_task = task_instruction.strip()
        if stripped_task.startswith("/") and (has_native_session or not session_history):
            return stripped_task

        sections: List[str] = [stripped_task]
        if not has_native_session and session_history:
            trimmed = [
                f"- Q: {item['q'][:160]} -> A: {item['a'][:200]}"
                for item in session_history[-3:]
                if item.get("q")
            ]
            if trimmed:
                sections.append("[RECENT_SESSION_WINDOW]\n" + "\n".join(trimmed))
        if workspace_dir and not stripped_task.startswith("/"):
            sections.append(f"[WORKSPACE_DIR]\n{workspace_dir}")
        return "\n\n".join(sections) + "\n"
