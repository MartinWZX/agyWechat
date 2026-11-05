"""End-to-End integration tests for WeChat-AGY Bridge."""

import asyncio
from pathlib import Path
import pytest
import httpx
from app.config import Settings
from app.main import create_app
from app.singleton_agy import SingletonAGYManager, SystemState
from app.wechat_gateway import WeChatGateway, WeChatPusher


def make_e2e_setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "subdir").mkdir()
    (workspace / "sample.txt").write_text("sample content")

    settings = Settings(
        WORKSPACE_ROOT=workspace,
        ALLOWED_USERS=["test_user"],
        WECHAT_TOKEN="secret_token",
        WECHAT_PUSH_URL="http://test/mock/wechat/push",
        AGY_BINARY="bash",
        AGY_DEFAULT_ARGS=[],
        DEBOUNCE_SECONDS=0.1,
        MAX_CHUNK_CHARS=1000,
        WECHAT_MAX_MSG_LEN=2000,
    )

    pushed_requests = []

    async def mock_push_handler(request: httpx.Request):
        pushed_requests.append(request)
        return httpx.Response(200, json={"errcode": 0})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_push_handler))
    pusher = WeChatPusher(settings=settings, client=mock_client)
    gateway = WeChatGateway(settings=settings, pusher=pusher)
    manager = SingletonAGYManager(settings=settings, push_callback=gateway.on_terminal_output)

    app = create_app(settings=settings, manager=manager, gateway=gateway)

    return {
        "app": app,
        "settings": settings,
        "manager": manager,
        "gateway": gateway,
        "workspace": workspace,
        "mock_client": mock_client,
        "pushed_requests": pushed_requests,
    }


@pytest.fixture
def e2e_setup(tmp_path):
    return make_e2e_setup(tmp_path)


@pytest.mark.asyncio
async def test_e2e_full_workflow(e2e_setup):
    app = e2e_setup["app"]
    manager: SingletonAGYManager = e2e_setup["manager"]
    workspace: Path = e2e_setup["workspace"]
    pushed_requests: list[httpx.Request] = e2e_setup["pushed_requests"]
    mock_client: httpx.AsyncClient = e2e_setup["mock_client"]

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Health and status checks
            res = await client.get("/health")
            assert res.status_code == 200
            health_data = res.json()
            assert health_data["status"] == "healthy"
            assert health_data["state"] == "HOST_MODE"
            assert not health_data["agy_running"]

            res = await client.get("/status")
            assert res.status_code == 200
            status_data = res.json()
            assert status_data["cwd"] == str(workspace)
            assert "channel" in status_data
            assert "ilink_active" in status_data

            # 2. WeChat GET Webhook verification
            res = await client.get("/webhook?echostr=echostr_val_1234")
            assert res.status_code == 200
            assert res.text == "echostr_val_1234"

            # 3. Test /pwd command via POST /webhook
            post_pwd = {
                "FromUserName": "test_user",
                "Content": "/pwd",
            }
            res = await client.post("/webhook", json=post_pwd)
            assert res.status_code == 200
            assert res.json() == {"errcode": 0, "errmsg": "ok"}

            # Wait for async queue delivery to pusher
            await asyncio.sleep(0.3)
            assert len(pushed_requests) >= 1
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert str(workspace) in last_body

            # 4. Test /ls command
            post_ls = {
                "FromUserName": "test_user",
                "Content": "/ls",
            }
            res = await client.post("/webhook", json=post_ls)
            assert res.status_code == 200
            await asyncio.sleep(0.3)
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert "sample.txt" in last_body
            assert "subdir" in last_body

            # 5. Test /cd sandbox enforcement
            post_escape = {
                "FromUserName": "test_user",
                "Content": "/cd ../../../",
            }
            res = await client.post("/webhook", json=post_escape)
            assert res.status_code == 200
            await asyncio.sleep(0.3)
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert "超出沙箱限制" in last_body

            # 6. Test /cd valid navigation
            post_cd = {
                "FromUserName": "test_user",
                "Content": "/cd subdir",
            }
            res = await client.post("/webhook", json=post_cd)
            assert res.status_code == 200
            await asyncio.sleep(0.3)
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert "工作目录已切换至" in last_body
            assert manager.GLOBAL_CWD == (workspace / "subdir").resolve()

            # 7. Start AGY session via /agy
            post_agy = {
                "FromUserName": "test_user",
                "Content": "/agy",
            }
            res = await client.post("/webhook", json=post_agy)
            assert res.status_code == 200
            await asyncio.sleep(0.4)

            assert manager.state == SystemState.AGY_MODE
            assert manager.is_running()
            all_bodies = [req.content.decode("utf-8") for req in pushed_requests]
            assert any("AGY 会话已启动" in b for b in all_bodies)

            # Verify status endpoint reflects AGY_MODE
            res = await client.get("/status")
            assert res.json()["state"] == "AGY_MODE"

            # 8. Send terminal input in AGY_MODE
            pushed_count_before = len(pushed_requests)
            post_input = {
                "FromUserName": "test_user",
                "Content": "echo 'AGY_E2E_VERIFICATION_PASS'",
            }
            res = await client.post("/webhook", json=post_input)
            assert res.status_code == 200

            # Wait for PTY execution and debounce buffer flush
            await asyncio.sleep(0.6)
            new_pushes = [req.content.decode("utf-8") for req in pushed_requests[pushed_count_before:]]
            assert any("AGY_E2E_VERIFICATION_PASS" in p for p in new_pushes)

            # 9. Send /ctrlc in AGY_MODE
            post_ctrlc = {
                "FromUserName": "test_user",
                "Content": "/ctrlc",
            }
            res = await client.post("/webhook", json=post_ctrlc)
            assert res.status_code == 200
            await asyncio.sleep(0.3)
            all_bodies = [req.content.decode("utf-8") for req in pushed_requests]
            assert any("Ctrl+C" in b for b in all_bodies)

            # 10. Exit AGY session via /exit
            post_exit = {
                "FromUserName": "test_user",
                "Content": "/exit",
            }
            res = await client.post("/webhook", json=post_exit)
            assert res.status_code == 200
            await asyncio.sleep(0.4)

            assert manager.state == SystemState.HOST_MODE
            assert not manager.is_running()
            all_bodies = [req.content.decode("utf-8") for req in pushed_requests]
            assert any("回到 `HOST_MODE`" in b for b in all_bodies)

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_e2e_unauthorized_user(e2e_setup):
    app = e2e_setup["app"]
    pushed_requests: list[httpx.Request] = e2e_setup["pushed_requests"]
    mock_client: httpx.AsyncClient = e2e_setup["mock_client"]

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            post_unauth = {
                "FromUserName": "hacker_openid",
                "Content": "/pwd",
            }
            res = await client.post("/webhook", json=post_unauth)
            assert res.status_code == 200

            await asyncio.sleep(0.3)
            assert len(pushed_requests) >= 1
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert "访问被拒绝" in last_body

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_e2e_xml_payload(e2e_setup):
    app = e2e_setup["app"]
    pushed_requests: list[httpx.Request] = e2e_setup["pushed_requests"]
    mock_client: httpx.AsyncClient = e2e_setup["mock_client"]

    xml_body = """<xml>
        <ToUserName><![CDATA[server_bot]]></ToUserName>
        <FromUserName><![CDATA[test_user]]></FromUserName>
        <MsgType><![CDATA[text]]></MsgType>
        <Content><![CDATA[/pwd]]></Content>
    </xml>"""

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/webhook", content=xml_body, headers={"Content-Type": "application/xml"})
            assert res.status_code == 200
            assert res.text == "success"

            await asyncio.sleep(0.3)
            assert len(pushed_requests) >= 1
            last_body = pushed_requests[-1].content.decode("utf-8")
            assert "当前工作目录" in last_body

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_e2e_anonymous_user_blocked(e2e_setup):
    app = e2e_setup["app"]
    pushed_requests: list[httpx.Request] = e2e_setup["pushed_requests"]
    mock_client: httpx.AsyncClient = e2e_setup["mock_client"]

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Send raw text body with no identifiable user_id
            res = await client.post("/webhook", content="/pwd", headers={"Content-Type": "text/plain"})
            assert res.status_code == 200

            await asyncio.sleep(0.3)
            assert len(pushed_requests) >= 1
            last_body = pushed_requests[-1].content.decode("utf-8")
            # Must be denied because "anonymous" is not in ALLOWED_USERS
            assert "访问被拒绝" in last_body
            assert "anonymous" in last_body

    await mock_client.aclose()
