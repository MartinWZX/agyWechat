"""Tests for Zero-Hallucination AST Audit, SecretStr Config, Sandbox Redlines, and Git Zero-Leakage Gate."""
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from config import Settings
from gateway.ilink_gateway import ContextTokenStore, ILinkPusher
from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError
from skills.skill_manager import SkillManager
from tools.tool_registry import (
    ToolRegistry,
    tool_ast_contract_audit,
    tool_db_query_stats,
    tool_git_zero_leakage_audit,
)
from verifier.hallucination_guard import ShadowDryRunValidator


def test_settings_secret_str_masking():
    s = Settings(
        WECHAT_TOKEN="wx_hook_secret_val",
        WECHAT_BOT_SECRET="wx_bot_secret_val",
        WEIXIN_TOKEN="ilink_secret_val",
    )
    assert isinstance(s.WECHAT_TOKEN, SecretStr)
    assert isinstance(s.WECHAT_BOT_SECRET, SecretStr)
    assert isinstance(s.WEIXIN_TOKEN, SecretStr)

    dumped_repr = repr(s)
    assert "wx_hook_secret_val" not in dumped_repr
    assert "wx_bot_secret_val" not in dumped_repr
    assert "ilink_secret_val" not in dumped_repr
    assert s.WEIXIN_TOKEN.get_secret_value() == "ilink_secret_val"

    empty_s = Settings(WEIXIN_TOKEN="   ")
    assert empty_s.WEIXIN_TOKEN is None


@pytest.mark.asyncio
async def test_ilink_pusher_unwraps_secret_str_at_http_boundary(tmp_path: Path):
    settings = Settings(WEIXIN_TOKEN="real_bearer_tok_999")
    token_store = ContextTokenStore(tmp_path / "ctx.json")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ret": 0}
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = mock_resp

    pusher = ILinkPusher(settings=settings, token_store=token_store, client=mock_client)
    ok = await pusher.send_raw("user_1", "hello")
    assert ok is True

    sent_headers = mock_client.post.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer real_bearer_tok_999"


def test_approval_guard_enforces_all_prohibited_calls_and_aliases():
    prohibited_snippets = [
        "eval('1 + 1')",
        "exec('a = 2')",
        "import os\nos.system('id')",
        "import subprocess\nsubprocess.Popen(['ls'])",
        "import shutil\nshutil.rmtree('/tmp/demo')",
        "from shutil import rmtree as purge_dir\npurge_dir('/tmp/demo')",
        "from subprocess import Popen\nPopen(['echo', 'hi'])",
    ]
    for snippet in prohibited_snippets:
        with pytest.raises(SandboxSecurityError):
            ApprovalGuard.scan_python_ast(snippet)
        passed, errors = ApprovalGuard.inspect_script_code(snippet)
        assert passed is False
        assert len(errors) >= 1

    ok, msg = ApprovalGuard.scan_python_ast("def add(a: int, b: int) -> int:\n    return a + b\n")
    assert ok is True
    assert "passed" in msg


def test_approval_guard_git_redline_commands():
    blocked_git_cmds = [
        "git add .",
        "git add -A",
        "git add --all",
        "git add *",
        "git commit -am 'quick dump'",
        "git commit -a -m 'blind stage'",
        "git commit --no-verify -m 'skip hooks'",
        "git commit -n -m 'skip hooks short'",
        "git push --force origin main",
        "git push -f origin main",
    ]
    for cmd in blocked_git_cmds:
        with pytest.raises(SandboxSecurityError):
            ApprovalGuard.scan_shell_command(cmd)

    allowed_git_cmds = [
        "git add .gitignore .env.example config.py",
        "git status --porcelain",
        "git diff --cached --name-only",
        "git commit -m 'feat(security): add explicit staged files'",
        "git push origin feat/zero-leakage",
    ]
    for cmd in allowed_git_cmds:
        ok, _ = ApprovalGuard.scan_shell_command(cmd)
        assert ok is True


def test_db_query_stats_closes_connection_cleanly(tmp_path: Path):
    db_file = tmp_path / "stats.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT);")
    conn.execute("INSERT INTO messages (body) VALUES ('m1'), ('m2');")
    conn.commit()
    conn.close()

    res = ToolRegistry.execute("db_query_stats", db_path=str(db_file))
    assert res == {"messages_count": 2}

    bad_db = tmp_path / "empty.db"
    sqlite3.connect(str(bad_db)).close()
    with pytest.raises(sqlite3.OperationalError):
        tool_db_query_stats(str(bad_db))


def test_git_zero_leakage_audit_and_ast_audit(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    env_example = tmp_path / ".env.example"
    env_example.write_text("OPENAI_API_KEY=your_key_here\n", encoding="utf-8")
    clean_py = tmp_path / "clean_mod.py"
    clean_py.write_text("def compute(x: int) -> int:\n    return x * 2\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", ".env.example", "clean_mod.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    clean_report = tool_git_zero_leakage_audit(str(tmp_path))
    assert clean_report["passed"] is True
    assert clean_report["blocked_files"] == []
    assert clean_report["secret_hit_lines"] == []

    # Stage prohibited .env.production, uncleaned notebook, and synthetic secret token
    env_prod = tmp_path / ".env.production"
    synthetic_token = "sk-" + ("A" * 24)
    env_prod.write_text(f"API_KEY={synthetic_token}\n", encoding="utf-8")

    dirty_nb = tmp_path / "analysis.ipynb"
    dirty_nb.write_text(
        json.dumps({"cells": [{"cell_type": "code", "execution_count": 1, "outputs": [{"text": "secret"}]}]}),
        encoding="utf-8",
    )

    subprocess.run(
        ["git", "add", "-f", ".env.production", "analysis.ipynb"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    dirty_report = tool_git_zero_leakage_audit(str(tmp_path))
    assert dirty_report["passed"] is False
    assert ".env.production" in dirty_report["blocked_files"]
    assert "analysis.ipynb" in dirty_report["uncleaned_notebooks"]
    assert len(dirty_report["secret_hit_lines"]) >= 1

    # AST contract audit test
    bad_py = tmp_path / "bad_mod.py"
    bad_py.write_text(
        "import time\n"
        "import requests\n"
        "def bad_defaults(items=[]):\n"
        "    try:\n"
        "        return items\n"
        "    except Exception:\n"
        "        return None\n"
        "async def bad_coro():\n"
        "    time.sleep(1)\n"
        "    requests.get('https://example.com')\n",
        encoding="utf-8",
    )
    ast_res = tool_ast_contract_audit([str(bad_py)])
    assert ast_res["passed"] is False
    joined = "\n".join(ast_res["violations"])
    assert "mutable_default_arg" in joined
    assert "silent_exception_return_none" in joined
    assert "sync_sleep_in_async" in joined
    assert "sync_requests_in_async" in joined


@pytest.mark.asyncio
async def test_l0_skill_zero_hallucination_git_gate_and_gitignore():
    root = Path(__file__).resolve().parent.parent
    l0_script = root / "skills" / "scripts" / "zero_hallucination_git_gate.py"
    ok, _ = ShadowDryRunValidator.validate_script(l0_script.read_text(encoding="utf-8"))
    assert ok is True

    sm = SkillManager(workspace_root=root)
    result = await sm.execute_l0_script(
        l0_script,
        workspace_dir=str(root),
        target_files=[str(l0_script)],
    )
    assert result["ast_audit"]["passed"] is True

    ignored_samples = [
        ".env",
        ".env.production",
        "cert.pem",
        "dump.sqlite3",
        "customers.csv",
        "chroma_db/data.bin",
        ".cursor/session.json",
        "terraform.tfstate",
        "scratch_test.py",
        "result/out.html",
    ]
    for sample in ignored_samples:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", sample],
            cwd=root,
            check=False,
        )
        assert proc.returncode == 0, f"Expected {sample} to be ignored by .gitignore"

    not_ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=root,
        check=False,
    )
    assert not_ignored.returncode != 0
