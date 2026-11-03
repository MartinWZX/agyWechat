"""Unit and integration tests for SingletonAGYManager."""

import asyncio
from pathlib import Path
import pytest
from app.config import Settings
from app.singleton_agy import SingletonAGYManager, SystemState


@pytest.fixture
def test_settings(tmp_path):
    return Settings(
        WORKSPACE_ROOT=tmp_path,
        AGY_BINARY="bash",
        AGY_DEFAULT_ARGS=[],
        DEBOUNCE_SECONDS=0.1,
        MAX_CHUNK_CHARS=1000,
        WECHAT_MAX_MSG_LEN=2000,
    )


@pytest.mark.asyncio
async def test_manager_initial_state(test_settings):
    manager = SingletonAGYManager(settings=test_settings)
    assert manager.state == SystemState.HOST_MODE
    assert not manager.is_running()
    assert manager.pid is None


@pytest.mark.asyncio
async def test_spawn_and_terminate(test_settings):
    output_chunks = []

    async def on_push(text: str):
        output_chunks.append(text)

    manager = SingletonAGYManager(settings=test_settings, push_callback=on_push)
    await manager.spawn()

    assert manager.state == SystemState.AGY_MODE
    assert manager.is_running()
    assert manager.pid is not None

    # Test duplicate spawn protection
    with pytest.raises(RuntimeError):
        await manager.spawn()

    # Terminate
    await manager.terminate()
    assert manager.state == SystemState.HOST_MODE
    assert not manager.is_running()


@pytest.mark.asyncio
async def test_send_input_and_pty_reading(test_settings):
    output_chunks = []

    async def on_push(text: str):
        output_chunks.append(text)

    manager = SingletonAGYManager(settings=test_settings, push_callback=on_push)
    await manager.spawn()

    # Send an echo command to the bash shell
    await manager.send_input("echo 'HELLO_AGY_PTY'")
    # Wait for output to be processed by debounce buffer
    await asyncio.sleep(0.3)

    combined_output = "".join(output_chunks)
    assert "HELLO_AGY_PTY" in combined_output

    await manager.terminate()


@pytest.mark.asyncio
async def test_send_ctrl_c(test_settings):
    manager = SingletonAGYManager(settings=test_settings)
    await manager.spawn()

    # Send a long running command
    await manager.send_input("sleep 10")
    await asyncio.sleep(0.1)

    # Send Ctrl+C
    await manager.send_ctrl_c()
    await asyncio.sleep(0.2)

    assert manager.is_running()
    await manager.terminate()


@pytest.mark.asyncio
async def test_restart(test_settings):
    manager = SingletonAGYManager(settings=test_settings)
    await manager.spawn()
    old_pid = manager.pid

    await manager.restart()
    new_pid = manager.pid

    assert manager.state == SystemState.AGY_MODE
    assert manager.is_running()
    assert new_pid != old_pid

    await manager.terminate()


@pytest.mark.asyncio
async def test_natural_exit(tmp_path):
    # Process that exits immediately
    settings = Settings(
        WORKSPACE_ROOT=tmp_path,
        AGY_BINARY="echo",
        AGY_DEFAULT_ARGS=["bye"],
        DEBOUNCE_SECONDS=0.1,
    )
    pushed = []

    async def on_push(text: str):
        pushed.append(text)

    manager = SingletonAGYManager(settings=settings, push_callback=on_push)
    await manager.spawn()

    # Wait for process exit detection
    await asyncio.sleep(0.4)

    assert manager.state == SystemState.HOST_MODE
    assert not manager.is_running()
    assert any("HOST_MODE" in msg for msg in pushed)
