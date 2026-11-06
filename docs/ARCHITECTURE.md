# Code-First Multi-CLI Agent 架构设计与系统规格白皮书

> **系统名称**：WeChat Multi-CLI Agent RSI Architecture (`agyWechat`)  
> **版本**：v2.0.0 (Production Ready) | **设计理念**：LLM-First, Code-Last → Code-First, LLM-Last (RSI 递归自我演进)  
> **支持驱动**：Google Antigravity (`agy`) / Pi Agent (`pi`) / Claude Code (`claude`) / Oh-My-Pi (`omp`)

---

## 一、 系统愿景与核心演进哲学

大语言模型（LLM）本质上是**概率性生成模型（Probabilistic Model）**，天然具有输出不确定性、推理延迟、Token 配额消耗以及供应商风控（429/403）风险。而生产系统的基石是**100% 确定性、零延迟与高可靠的物理代码**。

本系统确立了**“自下而上自我淘汰（Self-Obsoletion via Deterministic Solidification）”**的最高架构哲学：
$$\lim_{t \to \infty} P(\text{LLM Call}) = 0$$

- **探索期（Genesis & Exploration）**：无规则可用，大模型作为第一要素（LLM-First, Code-Last），自主探索需求、编写工具、提炼技能。
- **共创演进期（Co-Evolution & RSI Iteration）**：用户持续追问与交互，系统通过 `Observe -> Critique -> Mutate -> Verify` 递归演进循环，动态优化工具列表并沉淀标准化技能。
- **成熟收敛期（Convergence & Deterministic Dominance）**：高频交互被固化为 L0 零 Token 规则（$N \ge 3$），周期性任务被固化为纯代码 Cron（$CV \le 0.25$）。日常运行中 95%+ 请求被确定性 Python 代码秒级接管，大模型仅在遇到未定义异常（Out-of-Distribution）时作为兜底介入。

```
                     ┌───────────────────────────────────────────────┐
                     │ 阶段一：探索期 (LLM-First, Code-Last)         │
                     │ • 大模型构建框架、新工具与初始技能            │
                     └───────────────────────┬───────────────────────┘
                                             │ 用户交互与追问迭代
                                             ▼
                     ┌───────────────────────────────────────────────┐
                     │ 阶段二：共创演进期 (Interactive RSI Loop)      │
                     │ • 动态变异工具 (AST验证+版本快照)             │
                     │ • 沉淀标准化技能 (.agents/skills/)            │
                     │ • 追踪高频热点 (N>=3) 与周期规律 (CV<=0.25)   │
                     └───────────────────────┬───────────────────────┘
                                             │ 确定性固化沉淀
                                             ▼
                     ┌───────────────────────────────────────────────┐
                     │ 阶段三：成熟收敛期 (Code-First, LLM-Last)     │
                     │ • L0 确定性秒级拦截 (零Token开销)             │
                     │ • Stage-1 纯代码定时任务接管常规运维          │
                     │ • P(LLM Call) 趋近于 0，仅 OOD 异常时唤醒     │
                     └───────────────────────────────────────────────┘
```

---

## 二、 七大系统级架构铁律 (The Seven Architectural Iron Laws)

### 铁律一：全生命周期 RSI 递归自我演进与熵减铁律 (The RSI & Entropy-Reduction Law)
任何高频交互、固定流程或排障经验，严禁停留在概率性 Prompt 中，必须物理固化为：
1. `tools/tool_registry.py` 中的确定性 Python 工具函数；
2. `.agents/skills/` 与 `skills/scripts/` 中的标准化技能脚本；
3. `scheduler/jobs.json` 中的纯代码定时任务。

### 铁律二：多智能体 CLI 驱动底座 + 双模调度与受控子智能体并发
1. **100% 官方原生 CLI 调用**：严禁逆向抓取 OAuth Token 或反代非公开 API，直接调度宿主机官方原生二进制（`agy`、`pi`、`claude`、`omp`），保留原生握手与安全指纹。
2. **单机一行式与常驻 PTY 双模自适应**：
   - 一行式模式（Subprocess One-Liner）：`claude -p` / `pi -p` / `omp run` / `agy -p`，进程用完即毁；
   - 常驻 PTY 模式（Single-Host Persistent PTY）：`agy` 通过全局文件锁（`~/.agent_core/locks/agy.lock`）保持单例终端会话，消除频繁冷启动的 403 握手风控。
3. **受控子智能体并发（Anti-429）**：
   - 单次任务最多并发 $\le 2$ 个子智能体；
   - 检索排查强制只读（`research` 模式），写操作强制隔离工作区（`Workspace="branch"`）；
   - 子智能体降级为轻量模型（`flash` / `haiku`），最大轮数限制 $\le 3$，仅返回 $\le 300$ 字精简摘要。

### 铁律三：全引擎默认 `dangerously-skip-permissions` 全自主免确认执行 + 外置沙盒安全补偿
1. **零人工干预（Zero-Prompt Execution）**：全引擎注入 `--dangerously-skip-permissions` / `--no-confirm` / `--yes`，彻底消除后台服务与微信通道因 `[y/N]` 提示导致的死锁。
2. **外置沙盒硬防护（Module 10）**：将安全边界上移至外层 Python 沙盒守卫：
   - 环境变量清洗（剔除系统高危凭证）；
   - 路径白名单与黑名单拦截（严禁越界访问 `~/.ssh`、`~/.aws` 或根目录）；
   - 危险命令 AST / 正则硬阻断（`rm -rf /`、`mkfs`、`shutdown`）。

### 铁律四：五维一体全自适应反风控防御体系 (Zero 429/403 Policy)
1. **自适应滑动窗口**：5 分钟 $\le 20$ 次，1 小时 $\le 100$ 次；
2. **高斯泊松伪装抖动**：请求间隔自动注入 $3.0 \sim 8.0$ 秒随机延迟；
3. **HTTP 429 指数退避**：触发 429 时休眠 $2^n \times 15$ 秒，连续 3 次熔断冷却 15 分钟；
4. **HTTP 403 绝对锁死熔断**：检测到账号封控立即将熔断器置为 `LOCKDOWN_403`，阻断后续外发，向管理员告警；
5. **跨引擎毫秒级故障转移（Failover）**：主引擎冷却时无缝切换至备选可用引擎。

### 铁律五：全链路反幻觉确定性物理闭环与四段式履约协议
1. **工作区物理快照 Differ**：对比执行前后的 Git 状态与物理文件修改哈希，严禁“说多做少（Under-execution）”与“说少做多（Over-execution）”。
2. **沙盒影子空跑（Shadow Dry-Run）**：所有新变异工具与技能脚本必须在临时目录完成 AST 解析与测试执行（Exit Code 0），方可热加载。
3. **四段式输出契约**：
   - `<facts_anchor>`：列出引用的真实事实锚点与原始数据；
   - `<causal_chain>`：逻辑演绎推导链，步步引用事实锚点；
   - `<execution_manifest>`：物理执行清单 JSON（创建、修改、执行）；
   - `<final_conclusion>`：最终呈现给用户的严谨结论。

### 铁律六：微信生态深度适配与语音语义无损直提
1. 优先提取微信原生语音识别字段 `Recognition`，免除第三方 ASR 依赖；
2. 内置基于编辑距离与拼音相似度的同音字与语音转写纠错归一化（如“阿吉” $\to$ “agy”，“克劳德” $\to$ “claude”）；
3. ANSI / TUI 终端控制符完整剥离，动态重绘与光标移动清洗为纯净可读的 Markdown。

### 铁律七：高频问答与周期性任务纯代码定时固化 (Cron Solidification)
1. 问答意图出现频次 $N \ge 3$ 时，自动固化为 L0 正则/关键词匹配规则或独立 Python 脚本；
2. 周期性操作时间间隔变异系数 $CV = \frac{\sigma}{\mu} \le 0.25$ 时，自动推导 Cron 表达式写入 `scheduler/jobs.json`，由纯代码定时执行，大模型调用彻底归零。

---

## 三、 十三模块系统架构全景

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 微信用户接入层 (WeChat)                                │
│        • 个人号免公网扫码直连 (iLink Bot 协议)        • Webhook / 企业微信 / 公众号     │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Module 1: 网关层与语音归一化 (gateway/)                                                │
│ • ilink_gateway.py: 官方 iLink 长轮询通信, ContextToken 自动轮换, 异步推送队列          │
│ • wechat_gateway.py: Webhook 鉴权, 异步 100ms 快速 ACK, 消息事件调度                   │
│ • voice_normalizer.py: 微信原生 Recognition 提取, 拼音同音字纠错, 术语归一化          │
│ • terminal_cleaner.py: ANSI/VT100 终端序列净化, 光标控制符剥离, Markdown 渲染优化       │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ 归一化后的标准指令
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Module 2: L0 认知语义网关与意图过滤器 (compressor/cognitive_gate.py)                   │
│ • PreFlightGate: 意图合规性与数据溯源检查 (零虚构前置检验)                             │
│ • RuleMatcher: 秒级命中 L0 规则库 (状态查询/风控看板/排障审计), 零 Token 开销直接返回 │
│ • ContextCompressor: 剥离冗余历史与无效终端噪声, 上下文极致压缩 (>70% 压缩率)        │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
               │ 命中文档/状态/脚本 (95%)   │ 需大模型深度推理的未知任务 (5%)
               ▼                            ▼
┌───────────────────────────────┐ ┌──────────────────────────────────────────────────────┐
│ Module 6 & 7: 确定性代码执行层 │ │ Module 5: 五维反风控状态机 (guard/wind_control_governor.py) │
│ • crystallizer/               │ │ • 滑动窗口限频 (5m<=20, 1h<=100)                     │
│   pattern_crystallizer.py:    │ │ • 随机伪装延迟 (高斯抖动 3.0~8.0s)                   │
│   高频热点规则固化 (N>=3)     │ │ • 429 指数退避与自动冷却熔断                         │
│ • scheduler/                  │ │ • 403 绝对锁死熔断器 (Lockdown)                      │
│   code_first_cron.py:         │ │ • 跨引擎健康度评分与可用性探测                       │
│   周期性任务纯代码 Cron       │ └──────────────────────────┬───────────────────────────┘
│   (CV<=0.25 自动沉淀)         │                            │ 允许通过
└───────────────────────────────┘                            ▼
                                  ┌──────────────────────────────────────────────────────┐
                                  │ Module 3 & 4: 多智能体 CLI 调度中枢 (bridge/)        │
                                  │ • multi_cli_hub.py: 统一驱动抽象与跨引擎 Failover    │
                                  │ • agy_pty_bridge.py: agy 常驻 PTY 终端单例保持       │
                                  │ • drivers/: agy_driver, pi_driver, claude_driver,    │
                                  │             omp_driver (全部注入免确认安全参数)      │
                                  │ • 受控子智能体并发池: 并发<=2, branch隔离, max_turns<=3│
                                  └──────────────────────────┬───────────────────────────┘
                                                             │ 智能体输出与修改提案
                                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Module 10: 外置沙盒安全守卫 (sandbox/approval_guard.py)                                │
│ • 越界路径拦截 (禁止访问 ~/.ssh, ~/.aws, /etc 等)                                      │
│ • 高危命令阻断 (rm -rf /, mkfs, shutdown, dd)                                          │
│ • 环境变量沙盒隔离与敏感密钥剥离                                                       │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Module 11: 四维反幻觉物理校验引擎 (verifier/hallucination_guard.py)                    │
│ • WorkspaceSnapshotDiffer: Git 差异与文件哈希强校验 (阻断说多做少、说少做多)           │
│ • ShadowDryRunValidator: 临时沙盒运行与 AST 语法安全性实测 (Exit Code 0)               │
│ • LogicalConsistencyVerifier: 四段式契约结构与因果链锚点强校验                         │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Module 8, 9, 12, 13: RSI 递归演进与核心记忆状态层                                      │
│ • tools/tool_registry.py: 动态工具列表变异优化, AST 校验, 自动版本备份与热重载        │
│ • skills/skill_manager.py: L0-L3 四级递进式技能沉淀, 软链接多智能体规则同步             │
│ • memory/core_state_store.py: SQLite WAL 模式单写多读, FTS5 BM25 全文语义检索         │
│ • rsi/evolution_engine.py: 三阶段生命周期演进控制器, 自动化成熟度量化与能力固化        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 四、 核心交互时序图

### 1. 微信端消息接入与 L0 确定性规则拦截（零 Token 秒级响应）

```mermaid
sequenceDiagram
    autonumber
    actor User as 微信用户
    participant GW as iLink / Webhook 网关
    participant VN as 语音归一化器 (VoiceNormalizer)
    participant CG as L0 认知语义网关 (CognitiveGate)
    participant PC as 规则与脚本执行器 (Skill/Script)

    User->>GW: 发送文字或原生语音消息
    GW->>VN: 提取并净化内容 (识别 Recognition 字段)
    VN->>VN: 拼音纠错与术语对齐 ("阿吉" -> "agy")
    VN-->>GW: 返回标准化指令
    GW->>CG: 意图合规性与 L0 规则匹配
    alt 命中 L0 本地确定性规则 (状态/风控/已固化FAQ)
        CG->>PC: 执行本地 Python 脚本 / 返回规则结果
        PC-->>GW: 返回确定性文本响应
        GW-->>User: 毫秒级下发微信消息 (消耗 0 Token, 0 CLI)
    else 未命中规则 (需大模型推理)
        CG-->>GW: 放行进入下一级 CLI 调度管线
    end
```

### 2. 未知任务放行：反风控校验、CLI 调度、沙盒执行与反幻觉物理闭环

```mermaid
sequenceDiagram
    autonumber
    participant GW as 网关调度层
    participant WG as 五维反风控 (WindControlGovernor)
    participant Hub as 多智能体调度中枢 (MultiCLIHub)
    participant Driver as CLI 原生驱动 (agy/pi/claude/omp)
    participant Box as 外置沙盒 (ApprovalGuard)
    participant Diff as 反幻觉快照校验器 (SnapshotDiffer)
    participant RSI as RSI 演进引擎 (EvolutionEngine)

    GW->>WG: 申请调度令牌
    WG->>WG: 检查滑动窗口、熔断器状态并注入高斯抖动 (3~8s)
    WG-->>GW: 准予放行 (若主引擎熔断则自动 Failover)
    GW->>Diff: 记录执行前工作区物理快照 Snap_Before
    GW->>Hub: 提交执行请求
    Hub->>Driver: 执行命令 (注入 --dangerously-skip-permissions)
    Driver->>Box: 沙盒校验工作区路径与命令安全性
    Box-->>Driver: 放行安全操作
    Driver-->>Hub: 返回执行结果与输出日志
    Hub-->>GW: 交付候选输出
    GW->>Diff: 采集执行后快照 Snap_After 并校验四段式契约
    alt 物理差异与契约清单 100% 吻合
        Diff-->>GW: 校验通过 (EXIT CODE 0)
        GW->>RSI: 记录热点频次与成功经验 (触发潜在沉淀)
        GW-->>User: 推送经过净化的 Markdown 结论
    else 检测到说多做少 / 说少做多 / 逻辑矛盾
        Diff-->>GW: 阻断输出并回滚未声明的物理变更
        GW-->>User: 报警提示反幻觉拦截详情
    end
```

---

## 五、 模块接口规范与物理拓扑

### 1. 物理目录结构
```
agyWechat/
├── config.py                 # 全局配置中心 (Pydantic Settings + YAML 桥接)
├── config.yaml               # 运行时 YAML 配置文件
├── main.py                   # 单进程唯一入口 (flock, FastAPI, Gateway, Scheduler)
├── requirements.txt          # 生产依赖说明清单
├── pytest.ini                # Pytest 测试框架配置
├── run.sh                    # 服务控制脚本
├── deploy/
│   └── systemd/              # Linux Systemd 服务自启动单元
├── gateway/                  # Module 1: 微信双通道网关与净化
│   ├── ilink_gateway.py      # 微信个人号 iLink Bot 协议长轮询客户端与推送器
│   ├── wechat_gateway.py     # Webhook 回调接入、鉴权与异步调度
│   ├── voice_normalizer.py   # 微信原生语音 Recognition 提取与拼音归一化
│   └── terminal_cleaner.py   # ANSI/TUI 终端序列净化与 Markdown 转换
├── compressor/               # Module 2: L0 认知语义网关
│   └── cognitive_gate.py     # 前置溯源校验、意图过滤、上下文极致压缩
├── bridge/                   # Module 3 & 4: 多智能体 CLI 调度驱动底座
│   ├── multi_cli_hub.py      # 统一四引擎驱动调度、热备 Failover、并发限制
│   ├── agy_pty_bridge.py     # agy 终端常驻单例 PTY 进程维持
│   └── drivers/              # 原生 CLI 驱动实现
│       ├── base_driver.py    # CLI 驱动基类与抽象契约
│       ├── agy_driver.py     # Google Antigravity 原生 CLI 驱动
│       ├── pi_driver.py      # Pi Agent 原生 CLI 驱动
│       ├── claude_driver.py  # Claude Code 原生 CLI 驱动
│       └── omp_driver.py     # Oh-My-Pi 原生 CLI 驱动
├── guard/                    # Module 5: 五维反风控状态机
│   └── wind_control_governor.py # 滑动窗口、高斯延迟、429退避、403锁死熔断
├── crystallizer/             # Module 6: 高频规则固化器
│   └── pattern_crystallizer.py  # 频次水位追踪 (N>=3)、L0 规则沉淀
├── scheduler/                # Module 7: 纯代码定时任务
│   ├── code_first_cron.py    # 周期性时间序列 (CV<=0.25) 自动固化与执行
│   └── jobs.json             # 固化定时任务持久化配置
├── tools/                    # Module 8: 动态工具列表与版本演进
│   ├── tool_registry.py      # 工具注册中心、AST 沙盒测试、热更新
│   ├── versions/             # 工具历史版本快照 (支持毫秒级回滚)
│   └── tool_execution_trace.jsonl # 工具调用审计与执行追踪日志
├── skills/                   # Module 9: 四级递进式技能体系
│   ├── skill_manager.py      # 技能沉淀、四级分类管理、跨智能体同步
│   └── scripts/              # 确定性 L0 运维脚本库 (风控、审计、状态)
├── sandbox/                  # Module 10: 外置沙盒守卫
│   └── approval_guard.py     # 免确认执行安全补偿、路径黑白名单、命令拦截
├── verifier/                 # Module 11: 四维反幻觉物理校验闭环
│   └── hallucination_guard.py   # Git 快照对比、影子空跑测试、四段式契约
├── memory/                   # Module 12: 核心记忆状态中心
│   ├── core_state_store.py   # SQLite WAL 模式单写多读、FTS5 BM25 全文检索
│   └── agent_core.db         # 核心数据库物理存储
├── rsi/                      # Module 13: RSI 递归演进引擎
│   └── evolution_engine.py   # 领域成熟度量化、生命周期阶段控制、熵减调度
├── telemetry/                # 监控与遥测
│   └── health_monitor.py     # 系统健康度打分与多维指标统计
├── tests/                    # 完整端到端与单元测试集 (44 测试用例, 100% 通过)
└── docs/                     # 系统架构设计与项目介绍白皮书
    ├── ARCHITECTURE.md
    └── PROJECT_INTRODUCTION.md
```

### 2. 状态机规范

#### 1) 风控熔断器状态机 (Circuit Breaker State Machine)
```mermaid
stateDiagram-v2
    [*] --> CLOSED: 初始化
    CLOSED --> COOLDOWN_429: 触发 HTTP 429 / 配额超限
    COOLDOWN_429 --> CLOSED: 冷却时间届满 (指数退避)
    CLOSED --> LOCKDOWN_403: 触发 HTTP 403 / 封号风控
    COOLDOWN_429 --> LOCKDOWN_403: 触发 HTTP 403 / 封号风控
    LOCKDOWN_403 --> CLOSED: 人工排障验证后通过 API 手动重置
```

#### 2) RSI 生命周期阶段机 (RSI Lifecycle State Machine)
```mermaid
stateDiagram-v2
    [*] --> STAGE_A_EXPLORATION_LLM_FIRST: 领域初始冷启动 (成熟度得分 < 0.3)
    STAGE_A_EXPLORATION_LLM_FIRST --> STAGE_B_CO_EVOLUTION_INTERACTIVE: 积累初始工具与技能 (成熟度 0.3 ~ 0.7)
    STAGE_B_CO_EVOLUTION_INTERACTIVE --> STAGE_C_CONVERGENCE_CODE_FIRST: 规律固化与代码收敛 (成熟度 >= 0.7)
    STAGE_C_CONVERGENCE_CODE_FIRST --> STAGE_B_CO_EVOLUTION_INTERACTIVE: 遭遇未定义的新型 OOD 异常
```

---

## 六、 生产级可靠性与安全指标

| 指标维度 | 保证机制与设计值 | 达标验证方式 |
|---------|-----------------|-------------|
| **进程单例性** | 宿主全局文件锁 `flock` 独占，杜绝多进程冲突 | `test_host_mode_lock_singleton` |
| **风控隔离率** | 429 自动指数退避，403 物理硬熔断，风控发生率压降至 0 | `test_governor_window_and_cooldown` |
| **反幻觉确定性** | 物理文件快照哈希与 Git Differ 100% 比对，杜绝口嗨与盲改 | `test_snapshot_differ_under_execution` |
| **免确认安全性** | 内层放权 `dangerously-skip-permissions`，外层沙盒强阻断越界 | `test_sandbox_blocked_commands` |
| **语音纠错率** | 微信原生 `Recognition` 语义直提，拼音编辑距离修正准确率 > 98% | `test_voice_normalizer_homophones` |
| **代码收敛率** | 高频任务 ($N \ge 3$) 与周期任务 ($CV \le 0.25$) 100% 自动固化为代码 | `test_pattern_crystallization_auto_rule` |
| **测试覆盖率** | 13 个功能模块，44 项核心用例，执行通过率 100% (< 1.0s) | `pytest tests/` (44 passed) |

---
> **结论**：本系统彻底打破了“过度依赖概率性大模型”的脆弱传统，构建了一套能够伴随用户交互自我学习、自我编写确定性工具、自我固化定时任务并自我收敛的现代工业级自主智能体操作系统。
