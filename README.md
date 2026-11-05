# WeChat-AGY Single-Process Bridge

> 将本地 Google Antigravity CLI（`agy`）终端搬入微信 Cloud Bot 聊天界面的生产级 Python 桥接服务。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-39%20passed-brightgreen.svg)]()

---

## 1. 核心设计哲学

- **严格单例 (Strict Singleton)**：全局任意时刻最多允许 1 个 `agy` 进程运行，全局 `asyncio.Lock` 杜绝多开。
- **双态模态 (Dual-State Machine)**：`HOST_MODE`（宿主导航态）与 `AGY_MODE`（AGY 独占态）互斥切换。
- **PTY 直通 (PTY Passthrough)**：基于 POSIX `pty.openpty()` 终端模拟，全量 TUI 交互，消息 100% 直通 stdin。
- **流式净化与聚合 (Stream Cleaner)**：`pyte` 虚拟屏幕 + ANSI 剥离 + Spinner 动画帧去噪 + 1.5s 防抖合并 + 审批提示符秒级触发 + Markdown 代码块安全分片 (≤2000字符)。
- **防御纵深 (Defense in Depth)**：三层进程清杀（`os.killpg` SIGTERM/SIGKILL → `process.kill()` → `psutil` 孤儿进程扫荡）。

---

## 2. 系统架构与双态模态

```
                        服务启动
                           │
                           ▼
                    ┌──────────────┐
                    │  HOST_MODE   │ ◄──────────────────────────┐
                    │  (宿主导航态) │                             │
                    └──────┬───────┘                             │
                           │                                     │
         ┌─────────────────┼─────────────────┐                  │
         ▼                 ▼                 ▼                   │
     /pwd响应          /ls响应           /cd切换              /exit │
     返回路径          返回列表         GLOBAL_CWD             /kill │
                                                                 │
                           │ /agy 或 /new                        │
                           ▼                                     │
                    ┌──────────────┐   agy进程                   │
                    │  AGY_MODE    │   自行退出                   │
                    │ (AGY独占态)   │──────────────────────────►──┘
                    └──────┬───────┘
                           │
         ┌─────────────────┼──────────────────┐
         ▼                 ▼                  ▼
      /new重启          /ctrlc写           所有其他输入
      杀旧启新          \x03到PTY          100%直通PTY stdin
```

---

## 3. 命令操作手册

### 3.1 宿主导航态 (`HOST_MODE`)
在进入 AGY 之前，用户可通过微信命令浏览本地目录、选定工作区：

| 命令 | 功能说明 | 示例 |
|------|---------|------|
| `/pwd` | 显示当前工作目录 | `/pwd` |
| `/ls [路径]` | 查看指定或当前目录文件列表（带图标及大小） | `/ls` 或 `/ls src` |
| `/cd <路径>` | 切换工作目录（严格受 `WORKSPACE_ROOT` 沙箱保护） | `/cd project-a` |
| `/status` | 查看系统运行状态、沙箱与会话就绪态 | `/status` |
| `/help` | 查看完整帮助手册 | `/help` |
| `/agy` 或 `/new` | 在当前工作目录启动全局唯一 AGY 进程，进入 `AGY_MODE` | `/agy` |

### 3.2 AGY 独占态 (`AGY_MODE`)
一旦进入 `AGY_MODE`，普通命令与 AGY 原生指令直通 AGY，特殊控制命令被拦截：

| 命令 | 功能说明 | 实现机制 |
|------|---------|---------|
| *任意文本输入 / 需求问答* | 100% 透传至 AGY 终端交互 | 写入 PTY master fd |
| `/clear`, `/diff`, `/effort`, `/skills`, `/mcp`, `/tasks`, `/usage` 等 | Google AGY 原生 Slash 指令，100% 透传至 AGY 执行 | 写入 PTY master fd |
| `/ctrlc` 或 `/stop` | 向终端发送 SIGINT (Ctrl+C) 中断当前执行任务 | 写入 `\x03` |
| `/new` | 强杀当前进程，并在当前目录重启全新 AGY 会话 | `restart()` |
| `/exit`、`/quit` 或 `/kill` | 终止 AGY 会话，三层清杀后安全回到 `HOST_MODE` | `terminate()` |
| `/agy` | 提示当前已处于单例独占态，杜绝多开 | 状态防呆提示 |
| `/pwd`, `/ls`, `/cd` | 提示用户当前处于独占态，需先 `/exit` | 防误触提示 |

---

## 4. 快速开始

### 4.1 环境准备
- Linux / macOS 操作系统
- Python 3.10+
- Google Antigravity CLI (`agy`) 已安装并配置在 `$PATH` 或 `~/.local/bin/agy`

### 4.2 配置环境变量
从模板创建配置文件：
```bash
cp .env.example .env
```
根据实际情况修改 `.env`：
```ini
# 微信 Webhook 校验 Token
WECHAT_TOKEN=your_wechat_webhook_token_here

# 微信主动推送 API 地址 (如微信 Cloud Bot 发送消息接口)
WECHAT_PUSH_URL=https://api.weixin.qq.com/cgi-bin/message/custom/send
WECHAT_BOT_SECRET=your_wechat_bot_secret_here

# 允许访问的微信用户 OpenID 白名单 (* 表示全部允许)
ALLOWED_USERS=["user_openid_1", "user_openid_2"]

# 文件沙箱根目录 (/cd 无法越界此目录)
WORKSPACE_ROOT=/home/martin/AI/agyWechat

# agy 可执行文件路径
AGY_BINARY=/home/martin/.local/bin/agy

# PTY 虚拟终端规格与防抖配置
PTY_COLS=120
PTY_ROWS=40
DEBOUNCE_SECONDS=1.5
MAX_CHUNK_CHARS=1500
WECHAT_MAX_MSG_LEN=2000

# 服务监听地址
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8000
```

### 4.3 一键启动
项目自带自检与一键启动脚本：
```bash
./run.sh
```
脚本将自动初始化虚拟环境、安装依赖、加载配置并启动 Uvicorn 服务。

---

## 5. 接口说明

| 端点 | 方法 | 说明 |
|------|------|------|
| `/webhook` | `GET` | 微信服务器接入校验（验证签名并返回 `echostr`） |
| `/webhook` | `POST` | 微信消息接收端点，100ms 内极速响应 200 ACK，消息异步入队处理 |
| `/health` | `GET` | 服务健康检查（返回系统状态、运行时间、PID 等） |
| `/status` | `GET` | 当前系统状态详情（运行态、当前工作目录、活跃用户等） |
| `/mock/wechat/push` | `POST` | 本地测试/开发专用 Mock 消息接收端点 |
| `/mock/wechat/messages` | `GET` | 查看本地测试收到的所有 Mock 推送消息 |

---

## 6. 自动化测试

项目具备全方位的单元测试和端到端集成测试，涵盖终端 ANSI 净化、分片保护、进程单例、命令路由与 Webhook 闭环：

```bash
# 运行全部 39 个测试用例
./venv/bin/pytest -v
```

测试模块说明：
- `tests/test_terminal_cleaner.py`：测试 ANSI 控制符清洗、Spinner 去重、提示符检测、Markdown 代码块分片。
- `tests/test_singleton_agy.py`：测试 PTY 分配、输入透传、Ctrl+C、自然退出感知、进程清杀与重启。
- `tests/test_command_router.py`：测试白名单鉴权、沙箱安全越界拦截、双态路由分发。
- `tests/test_wechat_gateway.py`：测试微信标准签名校验、多种 Payload 解析、推送队列与指数重试。
- `tests/test_e2e.py`：端到端模拟微信 Webhook 发送与消息接收的完整生命周期交互。

---

## 7. 生产部署指南 (systemd)

如需在生产服务器后台长期运行，可配置 systemd 服务单元：

`/etc/systemd/system/agy-wechat.service`:
```ini
[Unit]
Description=WeChat-AGY Single-Process Bridge
After=network.target

[Service]
Type=simple
User=martin
WorkingDirectory=/home/martin/AI/agyWechat
ExecStart=/home/martin/AI/agyWechat/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --lifespan on
Restart=always
RestartSec=3
EnvironmentFile=/home/martin/AI/agyWechat/.env

[Install]
WantedBy=multi-user.target
```

启动并使能服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agy-wechat.service
sudo systemctl status agy-wechat.service
```
