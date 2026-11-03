# WeChat-AGY Single-Process Bridge 架构设计文档

> **版本**：v1.0.0 | **日期**：2026-11-03 | **作者**：首席系统架构师

---

## 1. 项目概述

**WeChat-AGY Bridge** 是一套将本地 Google Antigravity CLI（`agy`）终端会话搬入微信 Cloud Bot 聊天界面的生产级 Python 后端服务。用户可通过微信消息完整控制唯一的 `agy` 进程，实现远程 AI 编程协作。

### 1.1 核心设计哲学

| 原则 | 说明 |
|------|------|
| **严格单例** | 全局任意时刻最多 1 个 `agy` 进程，绝无例外 |
| **双态模态** | `HOST_MODE`（宿主导航）↔ `AGY_MODE`（AGY独占）互斥切换 |
| **PTY 直通** | AGY_MODE 下所有消息 100% 透传至 `agy` PTY stdin |
| **净化推送** | ANSI/TUI 控制符剥离后聚合推送，保持微信可读性 |
| **防御纵深** | 多层进程清杀保障（锁 → killpg → psutil 兜底） |

---

## 2. 系统架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    微信 Cloud Bot 用户端                      │
│            （发送消息 / 接收推送 via WeChat API）              │
└─────────────────────┬───────────────────┬───────────────────┘
                      │ Webhook POST       │ 主动推送 HTTP
                      ▼                   ▲
┌─────────────────────────────────────────────────────────────┐
│                   FastAPI + Uvicorn 服务层                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                  wechat_gateway.py                    │   │
│  │  • Webhook 接收 & 100ms ACK                           │   │
│  │  • 消息鉴权（ALLOWED_USERS 白名单）                    │   │
│  │  • 异步推送队列（PushQueue）                           │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │ 解析后的用户消息                    │
│                         ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                 command_router.py                     │   │
│  │  HOST_MODE: /pwd /ls /cd /help /agy(=/new)           │   │
│  │  AGY_MODE:  /new /exit /ctrlc + 100% passthrough     │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                    │
│           ┌─────────────┴──────────────┐                    │
│           ▼                            ▼                     │
│  ┌─────────────────┐        ┌────────────────────────┐      │
│  │  HOST_MODE 处理  │        │   singleton_agy.py      │      │
│  │  • os.getcwd    │        │   SingletonAGYManager   │      │
│  │  • os.listdir   │        │   • 全局锁 asyncio.Lock  │      │
│  │  • os.chdir     │        │   • PTY master/slave fd │      │
│  │  • 路径沙箱校验  │        │   • 进程组 killpg       │      │
│  └─────────────────┘        │   • psutil 兜底清杀      │      │
│                             │   • 异步 PTY 读协程      │      │
│                             └───────────┬────────────┘      │
│                                         │                    │
│                             ┌───────────▼────────────┐      │
│                             │   terminal_cleaner.py   │      │
│                             │   • pyte 虚拟屏幕       │      │
│                             │   • ANSI 正则净化       │      │
│                             │   • Spinner 帧过滤      │      │
│                             │   • 1.5s 防抖聚合       │      │
│                             │   • 审批提示符检测       │      │
│                             │   • 2000字符安全分片     │      │
│                             └───────────┬────────────┘      │
│                                         │ 净化后文本          │
│                                         ▼                    │
│                             ┌───────────────────────┐       │
│                             │   异步推送队列 Push Q  │       │
│                             └───────────┬───────────┘       │
└─────────────────────────────────────────│───────────────────┘
                                          │ HTTP 主动推送
                                          ▼
                               微信 Cloud Bot 消息发送 API
```

---

## 3. 双态状态机详解

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
      /new重启          /ctrlc写           所有其他消息
      杀旧启新          \x03到PTY          100%直通PTY stdin
```

### 3.1 HOST_MODE 可用命令

| 命令 | 功能 | 实现 |
|------|------|------|
| `/pwd` | 显示当前工作目录 | `GLOBAL_CWD` 变量 |
| `/ls [path]` | 列出目录内容（含图标） | `os.scandir()` |
| `/cd <path>` | 切换工作目录 | 沙箱校验 + 更新 `GLOBAL_CWD` |
| `/help` | 显示帮助信息 | 静态文本 |
| `/agy` | 启动 agy 进程，进入 AGY_MODE | `SingletonAGYManager.spawn()` |
| `/new` | 同 `/agy`（兼容别名） | 同上 |

### 3.2 AGY_MODE 拦截命令

| 命令 | 功能 | 实现 |
|------|------|------|
| `/new` | 杀旧进程，重启 agy | `SingletonAGYManager.restart()` |
| `/exit` 或 `/kill` | 终止 agy，回到 HOST_MODE | `SingletonAGYManager.terminate()` |
| `/ctrlc` 或 `/stop` | 发送 SIGINT（Ctrl+C） | 写入 `\x03` 到 PTY master fd |
| 其余所有消息 | 100% 透传到 agy PTY stdin | `os.write(master_fd, ...)` |

---

## 4. 模块详细设计

### 4.1 `app/config.py` — 配置管理模块

**职责**：通过 `pydantic-settings` 加载 `.env` 环境变量，提供全局类型安全配置。

**子模块/字段**：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `WECHAT_TOKEN` | `str` | 微信 Webhook 校验 Token |
| `WECHAT_PUSH_URL` | `str` | 微信主动推送消息 API 地址 |
| `WECHAT_BOT_SECRET` | `str` | 推送 API 鉴权密钥 |
| `ALLOWED_USERS` | `List[str]` | 允许使用的微信用户 OpenID 白名单 |
| `WORKSPACE_ROOT` | `Path` | 沙箱根目录，`/cd` 不可越界 |
| `AGY_BINARY` | `str` | `agy` 可执行文件路径（默认 `agy`） |
| `AGY_DEFAULT_ARGS` | `List[str]` | `agy` 启动默认参数列表 |
| `PTY_COLS` | `int` | PTY 终端列数（默认 120） |
| `PTY_ROWS` | `int` | PTY 终端行数（默认 40） |
| `DEBOUNCE_SECONDS` | `float` | 防抖聚合时间窗口（默认 1.5s） |
| `MAX_CHUNK_CHARS` | `int` | 单次推送最大字符数（默认 1500） |
| `WECHAT_MAX_MSG_LEN` | `int` | 微信消息最大长度（默认 2000） |
| `SERVICE_HOST` | `str` | 服务监听地址（默认 0.0.0.0） |
| `SERVICE_PORT` | `int` | 服务监听端口（默认 8000） |

---

### 4.2 `app/terminal_cleaner.py` — 终端流净化器

**职责**：将 `agy` PTY 输出的原始字节流转化为微信可读的纯净文本。

**子模块**：

#### 4.2.1 `ANSICleaner` — ANSI/VT100 控制符剥离器
- 使用 `pyte.Stream` + `pyte.Screen` 构建 120×40 虚拟屏幕
- 正则兜底：剥离所有 `ESC[...m`、`ESC[...H/J/K/A/B/C/D` 等控制序列
- Spinner 字符过滤：`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` 及其他旋转动画帧
- 输出：逐行净化后的纯文本

#### 4.2.2 `IncrementalExtractor` — 增量文本差分提取器
- 维护上次屏幕快照，仅提取新增行/变更行
- 去重连续重复行（防 Spinner 刷屏）
- 保留有效代码块和日志行

#### 4.2.3 `DebounceBuffer` — 防抖聚合缓冲器
- 1.5秒防抖窗口：连续输出合并为单条消息
- 1500字符硬上限：超出时立即 Flush
- `asyncio.Task` 驱动的定时 Flush 机制

#### 4.2.4 `ApprovalDetector` — 审批提示符检测器
- 检测模式：`[y/n]`、`(y/n)`、`Approve`、`Reject`、`Allow`、`Deny`、`Yes/No`、`Continue?` 等
- 检测到时立即触发 Flush，跳过防抖等待
- 返回检测结果供上层决策

#### 4.2.5 `WeChatSplitter` — 微信消息安全分片器
- 2000字符硬上限（微信消息长度限制）
- 智能分片：优先在换行符处切割，避免代码块中途截断
- Markdown 代码块完整性保护：检测 \`\`\` 配对，确保不在代码块内部截断

---

### 4.3 `app/singleton_agy.py` — 单例 AGY 进程管理器

**职责**：管理全局唯一 `agy` 进程的完整生命周期，实现严格单例和双态状态机。

**子模块**：

#### 4.3.1 `SystemState` — 状态枚举
```python
class SystemState(Enum):
    HOST_MODE = "HOST_MODE"
    AGY_MODE  = "AGY_MODE"
```

#### 4.3.2 `SingletonAGYManager` — 核心管理器
**关键属性**：
- `_lock: asyncio.Lock` — 全局互斥锁，防止并发操作
- `_process: asyncio.subprocess.Process | None` — 当前 agy 进程句柄
- `_master_fd: int | None` — PTY master 文件描述符
- `_slave_fd: int | None` — PTY slave 文件描述符
- `_state: SystemState` — 当前系统状态
- `GLOBAL_CWD: Path` — 当前工作目录

**核心方法**：

| 方法 | 功能 |
|------|------|
| `spawn(cwd)` | 在指定目录启动新 agy 进程（含 PTY 配置） |
| `terminate()` | 终止 agy 进程树，回到 HOST_MODE |
| `restart()` | 先 terminate 再 spawn（/new 实现） |
| `send_input(text)` | 向 PTY master fd 写入文本+回车 |
| `send_ctrl_c()` | 向 PTY master fd 写入 \x03 |
| `_kill_process_tree()` | killpg SIGTERM → 300ms → SIGKILL |
| `_psutil_cleanup()` | 扫描并强杀所有残留 agy 孤儿进程 |
| `_pty_reader_loop()` | 异步协程：持续读取 PTY 输出并推送 |
| `_monitor_process_exit()` | 异步协程：监控进程退出事件 |

**进程清杀三层防御**：
```
Layer 1: os.killpg(pgid, SIGTERM) → asyncio.sleep(0.3) → os.killpg(pgid, SIGKILL)
Layer 2: process.kill() + process.wait()  (直接句柄)
Layer 3: psutil 扫描 name=="agy" 残留孤儿 → kill()
```

**PTY 初始化流程**：
```
pty.openpty() → master_fd, slave_fd
fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
os.environ["TERM"] = "xterm-256color"
asyncio.create_subprocess_exec(agy_binary, ..., stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    preexec_fn=os.setsid, cwd=cwd, env={...})
os.close(slave_fd)  # 父进程关闭 slave 端
asyncio.create_task(_pty_reader_loop())
asyncio.create_task(_monitor_process_exit())
```

---

### 4.4 `app/command_router.py` — 双态命令路由器

**职责**：根据当前系统状态将用户消息路由到正确的处理逻辑。

**子模块**：

#### 4.4.1 `HostModeRouter` — 宿主导航态路由器
- 解析 `/pwd`、`/ls`、`/cd`、`/help`、`/agy`、`/new` 命令
- `/cd` 路径安全校验：必须在 `WORKSPACE_ROOT` 沙箱内
- 非法命令友好提示：引导用户使用 `/agy` 启动 AGY

#### 4.4.2 `AgyModeRouter` — AGY 独占态路由器
- 生命周期钩子拦截：`/new`、`/exit`、`/kill`、`/ctrlc`、`/stop`
- 宿主命令屏蔽：`/pwd`、`/ls`、`/cd` 返回提示
- 100% 直通：所有其他消息 → `singleton_agy.send_input(text)`

#### 4.4.3 `CommandRouter` — 顶层路由分发器
- 入口：`async def route(user_id: str, message: str) -> str | None`
- 鉴权检查：`user_id in ALLOWED_USERS`
- 状态检查：`manager.state` → 分发到对应子路由器

---

### 4.5 `app/wechat_gateway.py` — 微信消息网关

**职责**：处理微信 Cloud Bot 的 Webhook 接收与主动消息推送。

**子模块**：

#### 4.5.1 `WebhookHandler` — Webhook 接收处理器
- 路由：`POST /webhook`
- **100ms 极速 ACK**：立即返回 HTTP 200，避免微信重试
- 消息解析：提取 `user_id`、`message content`、`msg_type`
- Token 校验：HMAC-SHA256 签名验证
- 异步投递：将消息丢入内部处理队列

#### 4.5.2 `PushQueue` — 异步推送队列
- `asyncio.Queue` 实现无阻塞消息队列
- 后台 Worker 协程持续消费队列
- 失败重试：指数退避，最多 3 次重试
- 推送超时保护：单次推送 10s 超时

#### 4.5.3 `WeChatPusher` — 主动消息推送器
- 使用 `httpx.AsyncClient` 调用微信 API
- 支持文本消息和 Markdown 格式消息
- 消息长度超限自动调用 `WeChatSplitter` 分片推送

---

### 4.6 `app/main.py` — FastAPI 应用入口

**职责**：整合所有模块，管理应用生命周期。

**子模块**：

#### 4.6.1 `lifespan` — 应用生命周期管理
- **启动**：初始化 `SingletonAGYManager`、启动推送队列 Worker
- **关闭**：调用 `manager.terminate()` 清理 agy 进程树，关闭 PTY fd

#### 4.6.2 路由注册
- `POST /webhook`：微信 Webhook 接收
- `GET /health`：健康检查端点
- `GET /status`：返回当前系统状态（HOST/AGY MODE、CWD、进程PID）

---

## 5. 数据流详解

### 5.1 用户发消息到 agy 响应完整链路

```
微信用户发送消息
       │
       ▼
POST /webhook (wechat_gateway.py)
  ├─ Token 校验
  ├─ 立即返回 200 ACK
  └─ 投入内部处理队列
       │
       ▼
CommandRouter.route(user_id, message)
  ├─ [HOST_MODE] HostModeRouter 处理导航命令
  │      └─ 直接返回响应文本 → PushQueue → 微信
  └─ [AGY_MODE] AgyModeRouter
         ├─ 生命周期钩子 (/new /exit /ctrlc) → 直接处理
         └─ 其他消息 → SingletonAGYManager.send_input(text)
                            │
                            ▼
                    os.write(master_fd, text + "\r")
                            │
                            ▼
                    agy 进程 PTY stdin 接收输入
                    agy 执行并产生输出
                            │
                            ▼
                    _pty_reader_loop() 持续读取
                    os.read(master_fd, 4096)
                            │
                            ▼
                    TerminalCleaner 净化
                  ┌─ pyte 虚拟屏幕处理
                  ├─ ANSI 剥离
                  ├─ Spinner 过滤
                  └─ ApprovalDetector 检测
                            │
                    [审批提示] 立即Flush
                    [普通输出] 1.5s防抖 或 1500字符上限
                            │
                            ▼
                    WeChatSplitter 分片 (≤2000字符)
                            │
                            ▼
                    PushQueue → WeChatPusher → 微信API
                            │
                            ▼
                    微信用户收到 agy 响应消息
```

### 5.2 进程清杀时序图

```
/new 或 /exit 触发
       │
       ▼
async with _lock:  ← 获取全局互斥锁（阻止并发）
       │
       ├─ Layer 1: os.killpg(pgid, SIGTERM)
       │           asyncio.sleep(0.3)
       │           os.killpg(pgid, SIGKILL)
       │
       ├─ Layer 2: process.kill()
       │           await process.wait()
       │
       ├─ Layer 3: psutil 扫描 name=="agy"
       │           → p.kill() for each
       │
       ├─ 关闭 master_fd (os.close)
       │
       └─ 清空句柄 (_process=None, _master_fd=None)
              │
              ▼ (若是 /new)
       spawn() 在当前 GLOBAL_CWD 创建新进程
```

---

## 6. 安全设计

### 6.1 用户鉴权
- **白名单机制**：`ALLOWED_USERS` 列表，仅允许指定 OpenID 的微信用户访问
- **Token 校验**：Webhook 请求必须通过 HMAC-SHA256 签名验证
- **未授权拒绝**：未在白名单的请求返回 HTTP 403

### 6.2 路径沙箱
- **WORKSPACE_ROOT 边界**：`/cd` 命令的目标路径必须是 `WORKSPACE_ROOT` 的子目录
- **绝对路径解析**：`Path.resolve()` 防止 `../` 路径穿越攻击
- **符号链接保护**：解析符号链接后再校验边界

### 6.3 进程安全
- **单例强制**：全局 `asyncio.Lock` 防止并发启动多个 agy 实例
- **进程组隔离**：`os.setsid()` 确保 agy 及其子进程在独立进程组
- **彻底清杀**：三层清杀机制确保无孤儿进程残留

### 6.4 输入安全
- **消息长度限制**：单条输入消息长度不超过 4096 字符
- **二进制内容过滤**：非 UTF-8 内容拒绝处理

---

## 7. 可扩展性设计

### 7.1 多用户扩展路径
当前版本为单实例设计。如需支持多用户并发，可扩展为：
- `Dict[user_id, SingletonAGYManager]` — 每用户独立 agy 实例
- 独立工作目录隔离
- 资源限制（最大并发 agy 进程数）

### 7.2 消息平台扩展
- `wechat_gateway.py` 实现 `GatewayInterface` 抽象接口
- 可新增 Telegram、Slack、钉钉等网关实现

### 7.3 命令插件扩展
- `command_router.py` 支持注册自定义命令插件
- 插件格式：`async def handler(args: str) -> str`

---

## 8. 项目文件结构

```
agyWechat/
├── ARCHITECTURE.md          # 本架构设计文档
├── README.md                # 部署与操作指南
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
├── .env                     # 实际环境变量（勿提交 Git）
├── .gitignore
├── run.sh                   # 一键启动脚本
└── app/
    ├── __init__.py
    ├── main.py              # FastAPI 应用入口 & 生命周期
    ├── config.py            # pydantic-settings 配置管理
    ├── singleton_agy.py     # 单例 AGY 进程管理器（核心）
    ├── command_router.py    # 双态命令路由器
    ├── terminal_cleaner.py  # PTY 输出净化器 & 推送缓冲
    └── wechat_gateway.py    # 微信消息网关（收/发）
```

---

## 9. 核心技术选型理由

| 技术 | 选型理由 |
|------|---------|
| `pty.openpty()` | POSIX 标准伪终端，完整模拟 TUI 终端交互，支持 agy 的 TUI 界面 |
| `pyte` | 纯 Python VT100/VT220 终端模拟器，精确处理光标控制和屏幕更新 |
| `asyncio` | 全异步架构，PTY 读取、HTTP 推送、命令处理无阻塞并发 |
| `psutil` | 跨平台进程管理，精确扫描孤儿进程，确保单例铁律 |
| `FastAPI` | 高性能异步 Web 框架，自动 OpenAPI 文档，Webhook 极速响应 |
| `pydantic-settings` | 类型安全配置管理，`.env` 文件自动加载 |
| `httpx` | 原生 asyncio 兼容的 HTTP 客户端，用于主动推送消息 |

---

## 10. 关键设计决策记录

### ADR-001: 为何使用 PTY 而非 subprocess PIPE
**决策**：使用 `pty.openpty()` 而非 `subprocess.PIPE`
**理由**：`agy` 是一个全功能 TUI 应用，检测到非 TTY 环境时会禁用交互特性甚至拒绝启动。PTY 提供完整的终端模拟，使 `agy` 认为自己在真实终端中运行。

### ADR-002: 为何使用 os.setsid() 创建新进程组
**决策**：通过 `preexec_fn=os.setsid` 让 agy 成为新进程组组长
**理由**：`agy` 会衍生多个子进程（测试运行器、MCP 服务器等）。使用进程组可以通过单个 `os.killpg()` 调用一次性清杀所有相关进程，避免孤儿进程残留。

### ADR-003: 为何采用三层进程清杀机制
**决策**：killpg → process.kill() → psutil 扫描
**理由**：防御纵深。killpg 可能因权限问题失败；直接 process.kill() 是保底；psutil 扫描处理极端情况下的孤儿进程。单例铁律不允许任何漏网之鱼。

### ADR-004: /agy 作为 HOST_MODE 进入 AGY_MODE 的启动命令
**决策**：在宿主机 CLI 中使用 `/agy` 命令启动 agy 进程（而非 `/new`，因为 `/new` 在 agy 内部是新建会话的命令）
**理由**：区分语义——`/agy` = "启动 agy 程序"（HOST_MODE 专属），`/new` = "重启当前 agy 会话"（AGY_MODE 内部操作）。在 HOST_MODE 下 `/new` 作为 `/agy` 的兼容别名存在。

---

*文档持续更新中，如有架构变更请同步更新本文档。*
