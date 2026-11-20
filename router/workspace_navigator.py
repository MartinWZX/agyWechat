"""Non-Engine Workspace Directory Browser, Path Sandbox Guard, and Session Switcher."""
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Tuple, Union

from memory.engine_session_repository import EngineSessionRepository

PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "AGENTS.md")
FORBIDDEN_DIR_NAMES = {".ssh", ".aws", ".gnupg", ".kube", ".docker", "gcloud"}
FORBIDDEN_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/var", "/boot", "/dev", "/proc", "/sys", "/root")
SKIP_LS_NAMES = {"__pycache__", ".pytest_cache", "node_modules", ".git"}
MAX_ASSET_BYTES = 20 * 1024 * 1024


def _detect_asset_ext(raw_bytes: bytes, asset_type: str) -> str:
    """Infer file extension from magic bytes for images or fall back safely."""
    if asset_type == "image":
        if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if raw_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if raw_bytes.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".png"
    return ".txt"


def sanitize_asset_filename(
    raw_name: str,
    default_prefix: str = "asset",
    default_ext: str = ".bin",
) -> str:
    """Strip directory traversal/control bytes and produce a safe basename within `.wechat_assets`."""
    normalized = (raw_name or "").replace("\x00", "").replace("\\", "/").strip()
    base = Path(normalized).name if normalized else ""
    base = base.lstrip(".")
    safe = re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]", "_", base).strip("._")
    if not safe:
        safe = f"{default_prefix}_{int(time.time() * 1000)}"
    if Path(safe).suffix == "":
        safe = f"{safe}{default_ext}"
    return safe


class WorkspaceNavigator:
    """Manages per-user workspace inspection, asset persistence, and switching."""

    def __init__(
        self,
        session_repo: EngineSessionRepository,
        default_workspace: str = ".",
        allowed_roots: Optional[Iterable[str]] = None,
        user_workspaces: Optional[Dict[str, str]] = None,
    ):
        self.session_repo = session_repo
        self.default_workspace = str(Path(default_workspace).resolve())
        self.user_workspaces: Dict[str, str] = {}
        for uid, raw_ws in (user_workspaces or {}).items():
            u = str(uid).strip()
            p = str(raw_ws).strip()
            if u and p:
                self.user_workspaces[u] = str(Path(p).expanduser().resolve())
        roots: List[Path] = [
            Path(self.default_workspace),
            Path.home().resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        for ws_path in self.user_workspaces.values():
            roots.append(Path(ws_path))
        for extra in allowed_roots or ():
            if extra:
                roots.append(Path(str(extra)).expanduser().resolve())
        self.allowed_roots = roots

    def get_default_workspace(self, user_id: str) -> str:
        """Return per-user default workspace when configured, otherwise global default."""
        return self.user_workspaces.get(user_id, self.default_workspace)

    def get_workspace(self, user_id: str) -> str:
        """Return user's persisted workspace_dir or per-user/global default workspace."""
        return self.session_repo.get_workspace_dir(user_id, self.get_default_workspace(user_id))

    @staticmethod
    def detect_markers(directory: Path) -> List[str]:
        """Identify project type hints inside a directory."""
        return [m for m in PROJECT_MARKERS if (directory / m).exists()]

    def validate_path(self, user_id: str, raw_path: str) -> Tuple[bool, Optional[Path], str]:
        """Resolve and validate a target directory against host/credential boundaries."""
        raw = (raw_path or "").strip()
        if not raw:
            return False, None, "请提供目标路径，例如 `/workspace ~/projects/my_app`。"
        if "\x00" in raw:
            return False, None, "⛔ 路径包含非法空字节。"

        current = Path(self.get_workspace(user_id))
        if raw.startswith("~"):
            candidate = Path(os.path.expanduser(raw))
        else:
            p = Path(raw)
            candidate = p if p.is_absolute() else (current / p)

        try:
            resolved = candidate.resolve()
        except OSError as exc:
            return False, None, f"⛔ 无法解析路径 '{raw}'：{exc}"

        r_str = str(resolved)
        if r_str == "/" or any(r_str == p or r_str.startswith(p + "/") for p in FORBIDDEN_PREFIXES):
            return False, None, f"⛔ 安全限制：禁止将工作目录切换至系统目录 '{r_str}'。"

        if any(part in FORBIDDEN_DIR_NAMES for part in resolved.parts):
            return False, None, f"⛔ 安全限制：禁止访问凭据敏感目录 '{r_str}'。"

        if not any(resolved == root or root in resolved.parents for root in self.allowed_roots):
            roots_preview = ", ".join(str(r) for r in self.allowed_roots[:2])
            return False, None, f"⛔ 安全限制：目标路径 '{r_str}' 超出允许的工作根目录范围（{roots_preview} 等）。"

        if not resolved.exists():
            return False, None, f"⛔ 目录不存在：{r_str}"
        if not resolved.is_dir():
            return False, None, f"⛔ 目标不是目录：{r_str}"

        return True, resolved, ""

    def format_pwd(self, user_id: str) -> str:
        """Format current workspace directory and project context summary."""
        ws = Path(self.get_workspace(user_id))
        markers = self.detect_markers(ws)
        marker_line = ", ".join(markers) if markers else "普通目录（未检测到 Git/包管理标识）"
        user_default = self.get_default_workspace(user_id)
        if str(ws) == user_default:
            is_default = " (用户专属默认工作区)" if user_id in self.user_workspaces else " (默认项目根目录)"
        else:
            is_default = ""
        return (
            f"📂 当前工作目录：{ws}{is_default}\n"
            f"🏷️ 目录标识：{marker_line}\n"
            "👉 未进入引擎前可发送 `/ls` 查看内容、`/workspace <路径>` 切换项目，就绪后发送 `/agy` 启动引擎。"
        )

    def format_ls(self, user_id: str, raw_subpath: str = "") -> str:
        """List immediate subdirectories and files cleanly for WeChat browsing."""
        target = Path(self.get_workspace(user_id))
        if raw_subpath.strip():
            ok, resolved, err = self.validate_path(user_id, raw_subpath)
            if not ok or resolved is None:
                return err
            target = resolved

        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return f"⛔ 无法读取目录 {target}：{exc}"

        dirs: List[str] = []
        files: List[str] = []
        for entry in entries:
            if entry.name in SKIP_LS_NAMES or entry.name in FORBIDDEN_DIR_NAMES:
                continue
            if entry.is_dir():
                sub_tag = " [git]" if (entry / ".git").exists() else ""
                dirs.append(f"📁 {entry.name}/{sub_tag}")
            else:
                files.append(f"📄 {entry.name}")

        combined = (dirs + files)[:25]
        extra = max(0, len(dirs) + len(files) - 25)
        body = "\n".join(combined) if combined else "（空目录）"
        more = f"\n... 另有 {extra} 项未展开" if extra else ""
        return f"📂 目录清单 `{target}`（{len(dirs)} 子目录 / {len(files)} 文件）：\n{body}{more}\n👉 发送 `/workspace <子目录名>` 进入目标目录。"

    def switch_workspace(self, user_id: str, raw_path: str) -> str:
        """Validate and persist a new working directory for the user's Non-Engine session."""
        if not raw_path.strip():
            return self.format_pwd(user_id)
        ok, resolved, err = self.validate_path(user_id, raw_path)
        if not ok or resolved is None:
            return err
        saved = self.session_repo.set_workspace_dir(user_id, str(resolved))
        markers = self.detect_markers(resolved)
        badge = f"（检测到: {', '.join(markers)}）" if markers else ""
        return (
            f"✅ 已将当前会话工作目录切换至：\n`{saved}` {badge}\n"
            "👉 现在可发送 `/agy`（或 `/pi`、`/claude`、`/omp`）在该目录下启动引擎原生会话。"
        )

    def save_inbound_asset(
        self,
        user_id: str,
        data: Union[bytes, str],
        file_name: str = "",
        asset_type: str = "file",
        target_subdir: str = ".wechat_assets",
    ) -> Tuple[bool, Optional[Path], str]:
        """Validate and persist an inbound image or file fragment inside the user's bound workspace."""
        raw_bytes = data.encode("utf-8") if isinstance(data, str) else bytes(data or b"")
        if not raw_bytes:
            return False, None, "附件内容为空，未执行落盘。"
        if len(raw_bytes) > MAX_ASSET_BYTES:
            return False, None, f"附件大小超过 {MAX_ASSET_BYTES // (1024 * 1024)} MB 安全上限。"

        ws_str = self.get_workspace(user_id)
        ok_ws, resolved_ws, err_ws = self.validate_path(user_id, ws_str)
        if not ok_ws or resolved_ws is None:
            return False, None, err_ws

        subdir_clean = (target_subdir or "").strip().strip("/\\")
        if subdir_clean:
            if any(part in FORBIDDEN_DIR_NAMES or part in (".", "..") for part in Path(subdir_clean).parts):
                return False, None, f"⛔ 安全限制：非法的目标子目录 '{subdir_clean}'。"
            dest_dir = (resolved_ws / subdir_clean).resolve()
            if dest_dir != resolved_ws and resolved_ws not in dest_dir.parents:
                return False, None, "⛔ 安全限制：目标子目录越界。"
        else:
            dest_dir = resolved_ws

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, None, f"无法创建工作区附件目录：{exc}"

        default_ext = _detect_asset_ext(raw_bytes, asset_type)
        prefix = "wx_img" if asset_type == "image" else "wx_file"
        safe_name = sanitize_asset_filename(file_name, default_prefix=prefix, default_ext=default_ext)
        target_file = (dest_dir / safe_name).resolve()
        if dest_dir not in target_file.parents:
            return False, None, "⛔ 安全限制：附件文件名非法，禁止越界写入。"

        if target_file.exists():
            stem = target_file.stem
            suffix = target_file.suffix
            stamp = int(time.time() * 1000)
            candidate = (dest_dir / f"{stem}_{stamp}{suffix}").resolve()
            idx = 1
            while candidate.exists():
                candidate = (dest_dir / f"{stem}_{stamp}_{idx}{suffix}").resolve()
                idx += 1
            target_file = candidate

        try:
            target_file.write_bytes(raw_bytes)
        except OSError as exc:
            return False, None, f"写入工作区失败：{exc}"
        return True, target_file, ""
