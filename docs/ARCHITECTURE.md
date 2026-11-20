# agyWechat 架构设计与系统规格说明

> **系统定位**：纯粹“两态分离（Two-State Separation）”微信路由器 + 宿主电脑状态/工作区查看器 + 四大 AI 引擎（`agy` / `pi` / `claude` / `omp`）100% 原生直通网关  
> **核心原则**：非引擎态纯文本命令管理；引擎态网关仅保留 `/exit`、`/clear`、`/help`，其余一切输入携带完整宿主机环境原封不动直通原生 CLI（无爬虫探针、无环境变量脱敏、无 429/403 熔断与限流）

---

## 一、 核心架构分层与数据流向

系统采用严格的单向分层架构，自上而下分为 **接入网关层 → 纯粹两态路由层 → 四引擎直通驱动层 → 基础存储与宿主机遥测层**：

```text
微信端 (官方 Webhook XML / 个人号腾讯 iLink 长轮询)
  │
  ▼
[1. 接入网关层] gateway/wechat_gateway.py, ilink_gateway.py, terminal_cleaner.py, main.py
  ├─ 鉴权与 MsgId 幂等去重
  ├─ 模态边界预检（抽取微信原生 XML <Recognition> 或 iLink voice_item 文本；兼容 <?xml 声明头；严禁伪造兜底）
  └─ ANSICleaner 输出清洗（含连续进度行去重）+ WeChatSplitter 代码块保护分段推送（覆盖 iLink 与 Webhook 推送）
  │
  ▼
[2. 两态分离状态机] router/pipeline.py (DeterministicRouter)
  ├─ 第一态【非引擎态 / 网关路由态】(active_engine == "")
  │    ├─ msg_type == "voice" -> 直接拦截并提示切换文字命令或先进入引擎
  │    └─ 纯文本命令路由 (EngineSessionRouter & WorkspaceNavigator):
  │         /help, /status, /engines, /pwd, /workspace <path>, /ls [subpath], /agy, /pi, /claude, /omp
  │
  └─ 第二态【引擎态 / 原生智能体自治态】(active_engine in {"agy", "pi", "claude", "omp"})
       ├─ 放行微信原生语音识别结果 Recognition 原样透传（为空则直接拒绝并返回提示）
       ├─ 网关仅拦截 3 个桥接控制命令：/exit, /clear, /help
       └─ 其余全部输入（含任何其他 /斜杠命令、参数、自然语言、语音识别文本）携带工作区与 conversation_id 100% 直通原生 CLI
  │
  ▼
[3. 引擎驱动与直通层] bridge/multi_cli_hub.py + bridge/drivers/*
  ├─ BaseCLIAgentDriver Direct Execution：使用 os.environ.copy() 完整宿主机环境，无 Shell 注入、无密钥剥离
  ├─ 四大原生驱动：AgyDriver / PiDriver / ClaudeDriver / OmpDriver（声明式 JSON / conversation 参数支持）
  └─ MultiCLIHub：无爬虫探针、无熔断、无限流直通调用；非零退出码与 stderr 仅清洗 ANSI 后原样返还微信端
  │
  ▼
[4. 存储与宿主机遥测]
  ├─ memory/core_state_store.py      : SQLite WAL 持久化（严格仅保留 working_sessions, messages 2 张表并自动 VACUUM）
  └─ telemetry/health_monitor.py     : 宿主机实时 CPU 1/5/15m 负载、物理内存总量/可用/使用率、磁盘剩余空间、运行时长与已安装引擎
```

---

## 二、 关键模块设计细节

### 1. 配置优先级与统一装载 (`config.py`, `config.yaml`)
- `resolve_setting_or_yaml()` 确保严格遵循 **环境变量 / `.env` > `config.yaml` > `Settings` 默认值**。
- `resolve_workspace_root()` 与 `get_engine_binary(engine_id)` 保证工作区根目录与四大引擎可执行路径统一解析，支持通过 `AGY_BINARY`、`PI_BINARY`、`CLAUDE_BINARY`、`OMP_BINARY` 独立覆盖。

### 2. 秒级入引擎与两态绝对边界 (`router/`, `gateway/wechat_gateway.py`)
- **非引擎态严禁语音**：`WeChatGateway` 拦截所有 `active_engine == ""` 时的语音请求并固定返回提示，绝不唤醒任何 AI CLI。
- **毫秒级入引擎**：发送 `/agy`、`/pi`、`/claude` 或 `/omp` 时，`EngineSessionRouter.enter_engine()` 仅通过 `drv.is_installed()` 检查二进制并快速取版本号，即刻切换状态并输出精简欢迎面板（含引擎名、版本号、工作目录、`conversation_id` 状态），不启动任何 `--help` / ACP / PTY 爬虫。
- **引擎态 3 命令外 100% 直通**：仅 `/exit`、`/clear`、`/help` 由网关响应；其余任何斜杠命令（如 `/plan`、`/diff`、`/models`、`/compact` 等）与自然语言/微信原生语音 `Recognition` 均作为完整输入参数原样传给底层 CLI。

### 3. 全环境直通执行 (`bridge/multi_cli_hub.py`, `bridge/drivers/*`)
- 所有驱动默认 `timeout = 0`、`max_turns = 0`、`default_model = "inherit"`，直接以列表参数调用 `asyncio.create_subprocess_exec(*cmd, stdin=DEVNULL, cwd=workspace_dir, env=os.environ.copy(), start_new_session=True)`，并在超时或任务取消时通过 `os.killpg` 回收整棵子进程树，完整保留宿主机 API Key、Git 凭据与原生多智能体机制。
- 对支持原生会话的驱动（如 `agy --conversation` / `claude --resume`），从 JSON 输出抽取并延续 `conversation_id`，`/clear` 可随时重置。

### 4. 严格 2 表 SQLite WAL 存储 (`memory/core_state_store.py`)
`_init_db()` 维护 `working_sessions`（`session_id`、`user_id`、`created_at`、`updated_at`、`active_engine`、`engine_meta_json`、`workspace_dir`）与 `messages` 两张表，初始化时自动扫描并清理所有非上述两表的历史遗留表、FTS 虚拟表、触发器及废弃列（并执行 `VACUUM`）。
