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


@pytest.mark.asyncio
async def test_agy_mode_status(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # Start AGY session
    await router.route("authorized_user", "/agy")
    assert manager.state == SystemState.AGY_MODE

    # /status in AGY_MODE
    resp_status = await router.route("authorized_user", "/status")
    assert "系统运行状态" in resp_status
    assert "AGY_MODE" in resp_status
    assert "PID" in resp_status
    assert "authorized_user" in resp_status

    await router.route("authorized_user", "/exit")
    assert manager.state == SystemState.HOST_MODE


@pytest.mark.asyncio
async def test_host_mode_status_and_help(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # /status in HOST_MODE
    resp_status = await router.route("authorized_user", "/status")
    assert "系统运行状态" in resp_status
    assert "HOST_MODE" in resp_status

    # /help in HOST_MODE
    resp_help = await router.route("authorized_user", "/help")
    assert "WeChat-AGY Bridge 帮助手册" in resp_help
    assert "Google AGY 智能体原生命令" in resp_help


@pytest.mark.asyncio
async def test_agy_mode_duplicate_agy_and_quit(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    # Start AGY session
    await router.route("authorized_user", "/agy")
    assert manager.state == SystemState.AGY_MODE

    # Send /agy while in AGY_MODE -> should warn, not spawn second process
    resp_dup = await router.route("authorized_user", "/agy")
    assert "AGY 独占会话已在运行中" in resp_dup
    assert manager.state == SystemState.AGY_MODE

    # Send /quit -> should exit to HOST_MODE
    resp_quit = await router.route("authorized_user", "/quit")
    assert "回到 `HOST_MODE`" in resp_quit
    assert manager.state == SystemState.HOST_MODE


@pytest.mark.asyncio
async def test_agy_mode_empty_and_whitespace_input(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    await router.route("authorized_user", "/agy")
    assert manager.state == SystemState.AGY_MODE

    # Sending empty or whitespace message must not crash with IndexError
    resp_empty = await router.route("authorized_user", "   ")
    assert resp_empty is None  # Input was sent as \r

    await router.route("authorized_user", "/exit")
    assert manager.state == SystemState.HOST_MODE


@pytest.mark.asyncio
async def test_host_mode_path_with_spaces(sandbox_env):
    settings, manager, router, workspace = sandbox_env

    spaced_dir = workspace / "dir with spaces"
    spaced_dir.mkdir()
    (spaced_dir / "spaced_file.txt").write_text("ok")

    # /ls with quotes
    resp_ls = await router.route("authorized_user", '/ls "dir with spaces"')
    assert "spaced_file.txt" in resp_ls

    # /cd with quotes
    resp_cd = await router.route("authorized_user", '/cd "dir with spaces"')
    assert "工作目录已切换至" in resp_cd
    assert manager.GLOBAL_CWD == spaced_dir.resolve()
