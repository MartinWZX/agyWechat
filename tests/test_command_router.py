"""Unit tests for CommandRouter and dual-state routers."""

from pathlib import Path
import pytest
from app.config import Settings
from app.singleton_agy import SingletonAGYManager, SystemState
from app.command_router import CommandRouter


@pytest.fixture
def sandbox_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "subdir").mkdir()
    (workspace / "file1.txt").write_text("hello")
    (workspace / "file2.py").write_text("print('test')")

    settings = Settings(
        WORKSPACE_ROOT=workspace,
        ALLOWED_USERS=["authorized_user"],
        AGY_BINARY="bash",
        DEBOUNCE_SECONDS=0.1,
    )
    manager = SingletonAGYManager(settings=settings)
    router = CommandRouter(manager=manager, settings=settings)
    return settings, manager, router, workspace


@pytest.mark.asyncio
async def test_user_authorization(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # Unauthorized user
    resp = await router.route("unauthorized_user", "/pwd")
    assert "访问被拒绝" in resp

    # Authorized user
    resp = await router.route("authorized_user", "/pwd")
    assert "当前工作目录" in resp


@pytest.mark.asyncio
async def test_message_length_limit(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    long_msg = "a" * 5000
    resp = await router.route("authorized_user", long_msg)
    assert "输入超限" in resp


@pytest.mark.asyncio
async def test_host_mode_pwd_and_ls(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # /pwd
    resp = await router.route("authorized_user", "/pwd")
    assert str(workspace) in resp

    # /ls
    resp_ls = await router.route("authorized_user", "/ls")
    assert "file1.txt" in resp_ls
    assert "subdir" in resp_ls


@pytest.mark.asyncio
async def test_host_mode_cd_and_sandbox(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # Valid cd into subdirectory
    resp_cd = await router.route("authorized_user", "/cd subdir")
    assert "工作目录已切换至" in resp_cd
    assert manager.GLOBAL_CWD == (workspace / "subdir").resolve()

    # Attempt to escape sandbox via ../../
    resp_escape = await router.route("authorized_user", "/cd ../../../")
    assert "超出沙箱限制" in resp_escape
    # GLOBAL_CWD must not have escaped
    assert manager.GLOBAL_CWD.is_relative_to(workspace)

    # cd without argument resets to root
    resp_reset = await router.route("authorized_user", "/cd")
    assert "已返回沙箱根目录" in resp_reset
    assert manager.GLOBAL_CWD == workspace.resolve()


@pytest.mark.asyncio
async def test_host_mode_unknown_command(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    resp = await router.route("authorized_user", "write some code")
    assert "当前处于 `HOST_MODE`" in resp
    assert "/agy" in resp


@pytest.mark.asyncio
async def test_agy_mode_routing_and_passthrough(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # Start AGY session
    resp_start = await router.route("authorized_user", "/agy")
    assert "AGY 会话已启动" in resp_start
    assert manager.state == SystemState.AGY_MODE

    # In AGY_MODE, /pwd should warn user
    resp_pwd = await router.route("authorized_user", "/pwd")
    assert "当前系统处于 `AGY_MODE`" in resp_pwd

    # Passthrough ordinary message
    resp_pass = await router.route("authorized_user", "echo 'hello from passthrough'")
    assert resp_pass is None  # Input went straight into PTY

    # /ctrlc
    resp_ctrlc = await router.route("authorized_user", "/ctrlc")
    assert "Ctrl+C" in resp_ctrlc

    # /exit returns to HOST_MODE
    resp_exit = await router.route("authorized_user", "/exit")
    assert "回到 `HOST_MODE`" in resp_exit
    assert manager.state == SystemState.HOST_MODE
