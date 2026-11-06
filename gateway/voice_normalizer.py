"""WeChat Native Voice Recognition Extractor & Phonetic Normalizer."""
import re
import xml.etree.ElementTree as ET
from typing import Dict, Any, Tuple, Optional

class VoiceTextNormalizer:
    def __init__(self, config: Dict[str, Any] = None):
        cfg = config or {}
        vn_cfg = cfg.get("voice_normalization", {})
        self.fillers = vn_cfg.get("spoken_fillers", [
            "呃", "嗯", "那个", "请问", "帮我看一下", "帮我", "查一下", "你好", "哎"
        ])
        raw_homophones = vn_cfg.get("homophone_corrections", {
            "丰控看板": "#风控看板",
            "封控看板": "#风控看板",
            "蜂控看板": "#风控看板",
            "风控看板": "#风控看板",
            "幻觉审计": "#幻觉审计",
            "环境审计": "#幻觉审计",
            "查看状态": "#状态",
            "系统状态": "#状态",
            "切换引擎派": "@pi",
            "切换引擎克劳德": "@claude",
            "切换引擎阿基": "@agy",
            "用派执行": "@pi",
            "爱特派": "@pi",
            "爱特克劳德": "@claude",
            "爱特阿基": "@agy",
        })
        # Sort keys by length descending to match longest phrases first
        self.homophones = dict(sorted(raw_homophones.items(), key=lambda x: len(x[0]), reverse=True))

    def extract_recognition_from_xml(self, xml_payload: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        try:
            root = ET.fromstring(xml_payload.strip())
            rec_node = root.find("Recognition")
            media_node = root.find("MediaId")
            format_node = root.find("Format")
            rec_text = rec_node.text.strip() if rec_node is not None and rec_node.text else None
            media_id = media_node.text.strip() if media_node is not None and media_node.text else None
            fmt = format_node.text.strip() if format_node is not None and format_node.text else None
            return rec_text, media_id, fmt
        except Exception:
            return None, None, None

    def extract_recognition_from_json(self, json_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        rec_text = json_data.get("voice_recognition_text") or json_data.get("Recognition")
        media_id = json_data.get("MediaId") or json_data.get("media_id")
        fmt = json_data.get("Format") or json_data.get("format") or "amr"
        if rec_text:
            rec_text = str(rec_text).strip()
        return rec_text, media_id, fmt

    def fallback_local_asr(self, media_id: str, fmt: str) -> str:
        return f"[LOCAL_ASR_FALLBACK:{media_id}]"

    def normalize(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        text = raw_text.strip()
        # 1. Strip spoken conversational fillers at beginning
        changed = True
        while changed:
            changed = False
            for filler in self.fillers:
                if text.startswith(filler):
                    text = text[len(filler):].strip()
                    changed = True

        # 2. Phonetic homophone dictionary replacement using single-pass regex
        # Build pattern escaping keys
        pattern = re.compile("|".join(re.escape(k) for k in self.homophones.keys()))
        def replace_func(match):
            return self.homophones[match.group(0)]
        
        text = pattern.sub(replace_func, text)
        # Deduplicate accidental leading double ##
        while text.startswith("##"):
            text = text[1:]
        return text.strip()

    def format_voice_echo(self, normalized_text: str) -> str:
        return f'[🎙️ 语音识别: "{normalized_text}"]'
