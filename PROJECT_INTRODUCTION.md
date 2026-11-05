# WeChat-AGY Single-Process Bridge 项目功能与模块架构文档

> **项目名称**：WeChat-AGY Single-Process Bridge (`agyWechat`)  
> **项目定位**：将本地与服务器端 Google Antigravity CLI（`agy`）搬入微信 ClawBot 聊天界面的生产级 Python 异步桥接服务。

---

## 一、 项目背景与核心价值

在日常开发与运维场景中，开发者经常需要在移动场景（如手机微信）中实时查看项目进度、发起 AI 编程任务、处理突发 Bug 或审批代码变更。Google Antigravity CLI（`agy`）作为一款功能强大的终端智能体编程工具，具备深度的代码理解、自动编辑、工具调用与多步推理能力，但原生仅运行在 POSIX 终端环境中。

**WeChat-AGY Single-Process Bridge** 解决了以下核心痛点：
1. **移动端终端交互**：利用微信 ClawBot 作为随身交互入口，随时随地向服务器端 AGY 派发编程与运维任务。
2. **终端虚拟化适配**：将复杂的 TUI（终端用户界面）、ANSI 转义序列、动态 Spinner 进度动画及屏幕重绘，净化重构为符合移动端排版的 Markdown 消息。
3. **单例与资源可控**：严格限制同一时刻单例运行，防止移动端多轮误触引发的多进程资源竞争与代码冲突。
4. **沙箱与安全隔离**：提供安全目录沙箱、OpenID 访问白名单及标准微信签名防御，保障服务器主机环境安全。

---

## 二、 系统总体架构与双态模态

系统设计遵循 **双态状态机（Dual-State Machine）** 范式，通过互斥的 `HOST_MODE`（宿主导航态）与 `AGY_MODE`（AGY 独占态）解耦工作区定位与智能体交互：

```
                           微信用户消息输入
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │ WeChatGateway (验签/解包) │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  UnifiedRouter (鉴权分发) │
                    └────────────┬────────────┘
                                 │
                 ┌───────────────┴───────────────┐
                 │                               │
        [AGY 进程未运行]                  [AGY 进程运行中]
                 ▼                               ▼
       ┌──────────────────┐            ┌──────────────────┐
       │    HOST_MODE     │            │     AGY_MODE     │
       │   (宿主导航态)    │            │    (AGY独占态)    │
       └─────────┬────────┘            └─────────┬────────┘
                 │                               │
    ┌────────────┼────────────┐      ┌───────────┼───────────┐
    ▼            ▼            ▼      ▼           ▼           ▼
  /pwd         /ls          /cd    /ctrlc      /exit      普通输入/原生指令
获取路径     浏览目录     切换目录   (SIGINT)   安全终止    100%透传写入PTY
                 │                                           │
                 └───────────────┐   ┌───────────────────────┘
                                 ▼   ▼
                           ┌───────────────┐
                           │ POSIX PTY /   │
                           │ Singleton AGY │
                           └───────┬───────┘
                                   │ 终端裸流 (含ANSI/TUI)
                                   ▼
                           ┌───────────────┐
                           │ StreamCleaner │ ◄── ANSI清洗 + Spinner去重
                           │ & Debouncer   │ ◄── 1.5s 防抖 + 审批探测
                           └───────┬───────┘ ◄── Markdown 分片保护 (≤2000字)
                                   │
                                   ▼
                           ┌───────────────┐
                           │ WeChatPusher  │ ──► 主动推送至微信客户端
                           └───────────────┘
```

---

## 三、 核心模块功能详解

项目源码结构高度模块化，各司其职：

```
agyWechat/
├── app/
│   ├── __init__.py
│   ├── config.py              # 配置管理与强类型校验
│   ├── main.py                # FastAPI 应用程序入口与 Webhook 路由
│   ├── singleton_agy.py       # PTY 分配、子进程生命周期与单例控制
│   ├── terminal_cleaner.py    # 终端流净化、防抖、代码块分片与审批探测
│   ├── command_router.py      # 双态模态命令路由器与沙箱防护
│   └── wechat_gateway.py      # 微信协议适配、消息防丢队列与主动推送
├── tests/                     # 39 个自动化测试覆盖套件
├── ARCHITECTURE.md            # 系统设计方案与时序规格
├── README.md                  # 用户操作与快速开始手册
├── run.sh                     # 一键环境初始化与启动脚本
└── requirements.txt           # 核心依赖清单
```

### 1. 配置管理中心 (`app/config.py`)
- **技术实现**：基于 `pydantic-settings` 实现类型安全与配置校验。
- **核心能力**：
  - **动态环境变量解析**：自动加载 `.env`，支持白名单 `ALLOWED_USERS`（支持 JSON 数组格式或逗号分隔符输入，支持 `*` 通配符）。
  - **沙箱路径规范化**：自动将 `WORKSPACE_ROOT` 转换为绝对路径，目录不存在时自动递归创建。
  - **终端规格配置**：集中管理虚拟 PTY 宽（`PTY_COLS`）高（`PTY_ROWS`）、防抖窗口（`DEBOUNCE_SECONDS`）及微信消息字符阈值（`WECHAT_MAX_MSG_LEN`）。

### 2. 单例进程管理器 (`app/singleton_agy.py`)
- **技术实现**：基于 POSIX `pty.openpty()` 伪终端与 `asyncio.Lock`。
- **核心能力**：
  - **绝对单例保障**：全局维护单一 `SingletonAGYManager`，启动前加锁检查，杜绝多客户端同时唤起多个 AGY 进程。
  - **高保真 PTY 环境**：通过 `termios` 与 `fcntl.ioctl(..., TIOCSWINSZ)` 设定伪终端行列尺寸，为 AGY 提供与真实 Linux 终端一致的交互体验。
  - **新会话独立运行**：设置 `start_new_session=True`，使子进程脱离宿主进程组，便于独立信号传递与清理。
  - **全量异步读写**：主事件循环中以异步非阻塞方式轮询 PTY Master fd，确保输入实时直通、输出流式捕获。
  - **三层纵深清杀防御**：
    1. 第一层：`os.killpg(pgid, signal.SIGTERM)` 发送软终止信号；
    2. 第二层：超时未退出触发 `os.killpg(pgid, signal.SIGKILL)` 强制清杀；
    3. 第三层：兜底调用 `psutil` 遍历清除任何脱离进程组的僵尸/孤儿孙进程。

### 3. 流式终端净化与防抖系统 (`app/terminal_cleaner.py`)
- **核心痛点**：终端包含大量 ANSI 颜色码、光标位移符、动态 Spinner 动画帧、重绘擦除序列，直接发送到微信会导致严重乱码或刷屏。
- **核心能力**：
  - **`ANSICleaner`**：使用完备的正则表达式剥离 CSI、OSC、光标定位符及进度动画（如 `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`、`◐◓◑◒` 等），并合并折叠连续重复的进度刷屏行。
  - **`IncrementalDiffExtractor`**：集成 `pyte` 虚拟屏幕流解析器，追踪终端行增量变更，消除因光标回退造成的重复输出。
  - **`ApprovalDetector` 审批嗅探**：精准匹配智能体等待人类审批的提示符（如 `[y/N]`、`Allow [Y/n]`、`Do you want to proceed?`），命中后直接穿透防抖窗口，0 延迟秒级将确认信息推送到微信。
  - **`DebounceBuffer` 防抖合并**：默认设置 1.5 秒滑动时间窗口，将连续输出片段拼装为完整可读的段落，防止微信在几秒内收到数十条碎片化消息。
  - **`MarkdownChunker` 安全分片**：针对微信单条消息最大 2000 字符的限制，在超出阈值时按行智能切割，**严格保证 Markdown 代码块（```` / ```）的成对闭合与在下一条自动续接**，彻底避免代码块在微信端渲染崩溃。

### 4. 双态命令路由器 (`app/command_router.py`)
- **统一入口**：`UnifiedRouter` 首先校验发送者的 OpenID 白名单，校验通过后根据 AGY 当前运行状态动态分发：
  - **`HOST_MODE` 路由**：
    - `/pwd`：查看当前工作目录；
    - `/ls [路径]`：浏览目录内容（附带文件夹/文件图标及尺寸）；
    - `/cd <路径>`：切换当前工作区，**具备沙箱越界防护**，任何企图通过 `../` 逃逸 `WORKSPACE_ROOT` 的操作均被严格拦截；
    - `/status`：查看服务整体运行时间、PID 与会话状态；
    - `/agy` 或 `/new`：在当前选定目录下创建并启动全局唯一 AGY 进程，平滑进入 `AGY_MODE`。
  - **`AGY_MODE` 路由**：
    - `/ctrlc` 或 `/stop`：向 PTY 写入 ASCII `\x03` 发送 SIGINT，中断当前正在执行的任务；
    - `/new`：优雅清理旧进程后，在当前目录秒级重建全新会话；
    - `/exit`、`/quit` 或 `/kill`：安全终止 AGY 进程，触发三层清杀后退回 `HOST_MODE`；
    - **100% 直通透传**：所有非拦截命令（包括所有普通自然语言交互需求）以及 Google AGY 的原生斜杠指令（如 `/clear`, `/diff`, `/effort`, `/skills`, `/mcp`, `/tasks`, `/usage`, `/agents`, `/goal` 等）全量 1:1 写入 PTY Master fd，无损享受完整的 AGY 智能体能力。

### 5. 微信网关与异步通知队列 (`app/wechat_gateway.py`)
- **协议兼容**：支持微信 ClawBot、企业微信及各类基于 Webhook 回调的服务。
- **100ms 极速响应机制**：
  - 微信服务器要求 Webhook 在 5 秒内返回 HTTP 200，否则会触发重试。
  - 网关在收到用户消息后仅执行格式提取，并在毫秒级完成消息入队并返回 HTTP 200 响应。
  - 消息处理交由后台独立协程异步调度，杜绝重试风暴。
- **指数退避重试推送队列 (`PushQueue`)**：
  - 后台常驻队列统一消费推送任务。
  - 网络抖动或接口限流时，采用 `min(base_delay * 2^retries, 30)` 算法实施指数退避重试，最大重试 3 次，防止消息丢失。
- **安全签名校验**：实现标准的 SHA1 字典序签名验证，杜绝未授权伪造请求。

### 6. 应用生命周期与 HTTP 服务 (`app/main.py`)
- **FastAPI 现代化架构**：
  - 基于 `@asynccontextmanager` 的 `lifespan` 机制，在服务启动时自动初始化后台工作队列与消费循环，服务关闭时自动销毁孤儿进程、释放 PTY 描述符。
  - 提供 `/webhook` 微信标准验签与消息接入点。
  - 提供 `/health` 与 `/status` 运维探针接口，便于集成 Docker 健康检查或 Prometheus/K8s 探活。
  - 内置 `/mock/wechat/push` 与 `/mock/wechat/messages` 接口，脱离外网与真实微信凭证即可实现 100% 本地闭环自动化测试。

---

## 四、 自动化测试体系

项目具有严苛的工程质量控制体系，测试套件位于 `tests/`，包含 39 个全绿测试用例（覆盖单元测试、并发防护与端到端闭环）：

| 测试套件 | 测试文件 | 核心校验场景 |
|---------|---------|-------------|
| **终端流净化测试** | `test_terminal_cleaner.py` | ANSI 序列清洗、Spinner 动画去重、`pyte` 屏幕 Diff 提取、审批提示符即时探测、长 Markdown 代码块分片闭合保护 |
| **单例进程测试** | `test_singleton_agy.py` | PTY Master/Slave 分配、子进程输入写回、Ctrl+C 中断传递、自然退出感知、多开并发互斥与三层清杀机制 |
| **路由与沙箱测试** | `test_command_router.py` | 白名单用户鉴权、非白名单拦截、沙箱越界拦截 (`/cd ../../`)、路径解析、双态命令路由分发 |
| **微信网关测试** | `test_wechat_gateway.py` | 标准 SHA1 签名校验、JSON/XML 消息体解析、异步接收队列、主动推送接口适配与指数退避重试 |
| **端到端集成测试** | `test_e2e.py` | 模拟真实微信消息发送、HOST 态切换目录、启动 AGY 独占态、透传输入、流式防抖聚合与主动推送完整生命周期闭环 |

---

## 五、 环境配置参数速查表 (`.env`)

| 环境变量名 | 默认值 | 作用说明 |
|-----------|-------|---------|
| `WECHAT_TOKEN` | `dev_token_123456` | 微信 Webhook GET 请求签名校验 Token |
| `WECHAT_PUSH_URL` | `http://127.0.0.1:8000/mock/wechat/push` | 微信主动推送 API 接口地址（生产环境填写微信/企业微信 API） |
| `WECHAT_BOT_SECRET` | *(空)* | 微信主动推送 API 的 Secret 或 Access Token |
| `ALLOWED_USERS` | `["*"]` | 允许交互的微信用户 OpenID 白名单，支持逗号或 JSON 数组；`*` 代表全员放行 |
| `WORKSPACE_ROOT` | 当前工作目录 | 文件沙箱根路径，`/cd` 命令无法越界跳出此目录 |
| `AGY_BINARY` | `agy` | Google Antigravity CLI 的可执行文件路径 |
| `AGY_DEFAULT_ARGS` | `[]` | 启动 AGY 时默认追加的 CLI 参数（如 `--effort high` 等） |
| `PTY_COLS` | `120` | PTY 虚拟终端宽度（影响终端折行与输出排版） |
| `PTY_ROWS` | `40` | PTY 虚拟终端高度 |
| `DEBOUNCE_SECONDS`| `1.5` | 终端流式输出防抖聚合延迟（秒） |
| `MAX_CHUNK_CHARS` | `1500` | 单次消息防抖聚合目标字符阈值 |
| `WECHAT_MAX_MSG_LEN` | `2000` | 微信平台单条消息最大字符硬限制 |
| `SERVICE_HOST` | `0.0.0.0` | FastAPI HTTP 服务绑定 IP |
| `SERVICE_PORT` | `8000` | FastAPI HTTP 服务端口 |

---

## 六、 常用操作命令速查手册

### 1. 宿主导航态 (`HOST_MODE`)
在未启动 AGY 之前，可使用宿主命令定位项目：
- `/pwd`：输出当前工作目录绝对路径。
- `/ls [路径]`：浏览指定目录下的文件与子文件夹列表。
- `/cd <路径>`：安全切换当前目录（受 `WORKSPACE_ROOT` 沙箱保护）。
- `/status`：查看桥接服务运行指标与状态。
- `/agy` 或 `/new`：在当前选定目录拉起全新的 AGY 会话，进入 `AGY_MODE`。

### 2. AGY 独占态 (`AGY_MODE`)
- **任意自然语言**：直接输入编程需求、代码修改意见或回答 AGY 的提问，100% 原始透传至 PTY。
- **原生 AGY 指令**：直接在微信输入 `/diff`、`/clear`、`/effort`、`/skills`、`/mcp`、`/tasks`、`/usage` 等，全部 1:1 直通执行。
- `/ctrlc` 或 `/stop`：向终端下发中断信号（Ctrl+C），停止当前长耗时操作。
- `/new`：强杀旧实例并在当前目录重新拉起全新会话。
- `/exit`、`/quit` 或 `/kill`：退出 AGY 会话，安全返回 `HOST_MODE`。
