"""Pure Multi-CLI Hub for Direct Passthrough to Four Native AI Engines (`agy`, `pi`, `claude`, `omp`)."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from config import get_engine_binary, resolve_workspace_root
from gateway.terminal_cleaner import ANSICleaner
from .drivers.agy_driver import AgyDriver
from .drivers.base_driver import BaseCLIAgentDriver, extract_engine_json_result, resolve_binary
from .drivers.claude_driver import ClaudeDriver
from .drivers.omp_driver import OmpDriver
from .drivers.pi_driver import PiDriver


class MultiCLIHub:
    """Direct native CLI routing hub with zero throttling, zero discovery crawlers, and verbatim output."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        store: Any = None,
        settings: Any = None,
    ):
        self.config = config or {}
        self.settings = settings
        self.store = store
        self.cleaner = ANSICleaner()

        eng_cfg = self.config.get("engines", {})
        driver_classes = {
            "agy": AgyDriver,
            "pi": PiDriver,
            "claude": ClaudeDriver,
            "omp": OmpDriver,
        }
        self.drivers: Dict[str, BaseCLIAgentDriver] = {}
        for eid, cls in driver_classes.items():
            ecfg = dict(eng_cfg.get(eid) or {}) if isinstance(eng_cfg, dict) else {}
            ecfg["binary"] = get_engine_binary(eid, settings=settings, yaml_config=self.config)
            self.drivers[eid] = cls.from_engine_config(ecfg)

        self.primary_engine: str = str(eng_cfg.get("primary") or "agy").strip().lower()
        self.workspace_dir: str = resolve_workspace_root(settings=settings, yaml_config=self.config)

    def get_installed_engines(self) -> List[str]:
        """Return list of configured engines whose binaries physically exist on the host."""
        return [eid for eid, drv in self.drivers.items() if drv.is_installed()]

    def list_configured_engines(self) -> List[Dict[str, Any]]:
        """Summarize binary installation paths for all four configured engines."""
        rows: List[Dict[str, Any]] = []
        for eid, drv in self.drivers.items():
            resolved = resolve_binary(drv.binary_path)
            rows.append({
                "engine_id": eid,
                "binary": drv.binary_path,
                "resolved": resolved or "",
                "installed": bool(resolved),
            })
        return rows

    async def probe_engines_summary(self, workspace_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """Live probe of all configured engines including resolved binary paths and CLI versions."""
        target_ws = workspace_dir or self.workspace_dir
        base_rows = self.list_configured_engines()

        async def _probe_row(row: Dict[str, Any]) -> Dict[str, Any]:
            eid = row["engine_id"]
            if row["installed"]:
                row["version"] = await self.drivers[eid].probe_version(target_ws)
            else:
                row["version"] = ""
            return row

        return list(await asyncio.gather(*(_probe_row(r) for r in base_rows)))

    async def _run_driver_once(
        self,
        driver: BaseCLIAgentDriver,
        prompt: str,
        workspace_dir: str,
        cli_flags: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        progress_interval: float = 0.0,
    ) -> Tuple[int, str, str]:
        if cli_flags:
            return await driver.execute_with_flags(
                prompt,
                workspace_dir,
                extra_flags=cli_flags,
                progress_callback=progress_callback,
                progress_interval=progress_interval,
            )
        return await driver.execute_one_liner(
            prompt,
            workspace_dir,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
        )

    async def execute_task(
        self,
        prompt: str,
        preferred_engine: Optional[str] = None,
        cli_flags: Optional[List[str]] = None,
        workspace_dir: Optional[str] = None,
        session_meta_out: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        progress_interval: float = 0.0,
        **_ignored: Any,
    ) -> Tuple[bool, str, str]:
        """Route prompt directly to the target native CLI engine and return cleaned verbatim output/errors."""
        target_engine = (preferred_engine or self.primary_engine).strip().lower()
        if target_engine not in self.drivers:
            return False, f"⚠️ 未配置目标引擎 '{target_engine}'。", "NONE"

        driver = self.drivers[target_engine]
        target_ws = workspace_dir or self.workspace_dir
        returncode, raw_stdout, stderr = await self._run_driver_once(
            driver,
            prompt,
            target_ws,
            cli_flags=cli_flags,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
        )

        stdout_text, detected_conv_id = extract_engine_json_result(raw_stdout)
        if session_meta_out is not None and detected_conv_id:
            session_meta_out["conversation_id"] = detected_conv_id

        clean_stdout = self.cleaner.clean_text(stdout_text or "").strip()
        clean_stderr = self.cleaner.clean_text(stderr or "").strip()

        if returncode == 0:
            return True, (clean_stdout or clean_stderr), target_engine

        parts = [clean_stdout] if clean_stdout else []
        if clean_stderr and clean_stderr != clean_stdout:
            parts.append(clean_stderr)
        detail = "\n".join(parts).strip()
        if "is not available in print mode" in detail:
            detail += "\n💡 提示：该命令在 CLI 非交互 print 模式下受限；可发送 /help 查看网关已适配的微信交互式命令。"
        msg = (
            f"⚠️ [{target_engine.upper()}] 原生 CLI 返回错误 (exit={returncode}):\n{detail}"
            if detail
            else f"⚠️ [{target_engine.upper()}] 原生 CLI 异常退出 (exit={returncode})。"
        )
        return False, msg, target_engine
