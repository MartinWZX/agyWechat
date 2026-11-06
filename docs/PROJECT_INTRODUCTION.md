# WeChat Multi-CLI Agent RSI Architecture 项目介绍与使用指南

> **项目名称**：WeChat Multi-CLI Agent RSI Architecture (`agyWechat`)  
> **项目定位**：基于“代码优先、大模型最后（Code-First, LLM-Last）”与 RSI 递归自我演进哲学的生产级全自主多智能体编排与微信服务系统  
> **核心驱动**：Google Antigravity CLI (`agy`)、Pi Agent (`pi`)、Claude Code (`claude`)、Oh-My-Pi (`omp`)

---

## 一、 项目背景与范式变革

在现阶段以大语言模型（LLM）为驱动的自动化工具中，行业普遍面临三大核心瓶颈：
1. **概率脆弱性（Probabilistic Fragility）**：模型输出具有随机性，同一问题多次询问可能得到不同格式或逻辑的答案，难以作为稳定运行的底层基础设施；
2. **算力与风控壁垒（Latency & Rate Limiting）**：每次微小交互都要消耗数千 Token 并等待数秒甚至数十秒网络往返，高频调用更极易触发厂商 `HTTP 429 Too Many Requests` 或 `HTTP 403 Forbidden` 封号；
3. **“口嗨幻觉”与执行断层（Hallucination Gap）**：模型常常在回答中宣称“已经修复”、“已创建文件”，但物理文件系统并未发生任何实际改变，或在未声明的情况下擅自修改系统关键文件。

### 核心演进范式：从“大模型优先”向“代码优先”收敛 (The RSI Paradigm Shift)

**WeChat Multi-CLI Agent** 颠覆了传统“每次提问均调用大模型”的昂贵路径，确立了**自我淘汰（Self-Obsoletion via Code Solidification）**的设计准则：

- **冷启动与探索期（Genesis - LLM-First, Code-Last）**：当系统面对一个全新任务或未知领域时，系统内缺乏现成规则与代码。此时**大模型作为第一要素**，负责分析需求、编写新工具（Tools）、提炼初始技能（Skills），并由胶水代码串联跑通。
- **追问与共创期（Evolution - Interactive RSI Co-Evolution）**：在用户通过微信与智能体不断交互、追问、指出漏洞并要求修改的过程中，系统开启动态递归迭代：
  - **工具列表动态优化**：自动对 `tools/tool_registry.py` 中的工具进行参数修正、函数重构或合并，经沙盒 AST 检验与测试运行后热更新，并持久化历史版本；
  - **经验沉淀为技能**：排障流程与工具组合经验自动沉淀为 `.agents/skills/` 下的标准化脚本，避免重复探索；
  - **频次与规律统计**：系统实时追踪意图触发频次 $N$ 与时间间隔方差。
- **成熟收敛期（Convergence - Code-First, LLM-Last）**：
  - 高频问题（$N \ge 3$）自动固化为 L0 秒级正则表达式规则或 Python 脚本，直接拦截回复，消耗 **0 Token、0 秒等待**；
  - 周期性任务（变异系数 $CV \le 0.25$）自动推导 Cron 表达式写入纯代码调度器 `scheduler/jobs.json`，由本地 Python 进程定期执行；
  - **最终状态**：系统中 95% 以上的日常运维与常规问答完全由 100% 确定性的 Python 代码接管，概率性大模型的调用率收敛趋近于 0（$\lim_{t \to \infty} P(\text{LLM}) \to 0$）。只有当出现脱离已有代码库的全新异常时，系统才唤醒大模型介入。

---

## 二、 核心特性矩阵

### 1. 原生四大主流命令行智能体接入 (Multi-CLI Drivers)
- **100% 官方原生二进制调用**：直接调用宿主机上由官方发布的原版 `agy`、`pi`、`claude`、`omp`，保留原生安全握手（如 `loadCodeAssist`）、TLS 特征与官方遥测。
- **单机一行式与常驻会话双模引擎**：
  - 支持单行子进程模式（One-Liner）：`claude -p` / `pi -p` / `omp run` / `agy -p`，进程随用随关；
  - 支持常驻 PTY 终端会话模式：利用系统文件锁单例维持 `agy` 会话，消除频繁重连的 403 风险。
- **跨引擎无感故障转移（Multi-Engine Failover）**：当主通道（如 `agy`）因配额或网络进入冷却期时，调度器在毫秒级自动切换至 `claude`、`pi` 或 `omp` 备选引擎。

### 2. 全引擎默认 `dangerously-skip-permissions` 全自主免确认执行
- 后台服务与微信异步通讯环境中，任何终端 `[y/N]` 确认提示都会引发线程死锁。系统全引擎强制注入跳过权限确认参数：
  - `agy`：默认注入 `--dangerously-skip-permissions`（或全自动审批配置）；
  - `claude`：默认注入 `claude -p "<prompt>" --dangerously-skip-permissions --max-turns 3 --output-format json`；
  - `pi`：默认注入 `pi -p "<prompt>" --no-confirm`；
  - `omp`：默认注入 `omp run --dangerously-skip-permissions --yes -p "<prompt>"`。
- **外置沙盒硬防护补偿**：在 Python 外层强力拦截越界目录（禁止访问根目录、`~/.ssh`、`~/.aws`）与高危命令（`rm -rf /`、`mkfs`、`shutdown`），确保宿主机绝对安全。

### 3. 五维一体高安全反风控体系 (Zero 429/403 Policy)
1. **滑动窗口限频**：5 分钟滑动窗口 $\le 20$ 次，1 小时 $\le 100$ 次；
2. **高斯泊松拟人化延迟**：请求间隔自动注入 $3.0 \sim 8.0$ 秒随机抖动；
3. **429 智能指数退避**：遭遇 429 时休眠 $2^n \times 15$ 秒，多次连击熔断 15 分钟；
4. **403 账号硬隔离熔断**：遭遇 403 立即锁定外发，防止封号扩散并触发微信报警；
5. **内层子智能体受控并发**：并发数 $\le 2$，限制 `flash` / `haiku` 模型，单次轮数 $\le 3$ 步。

### 4. 四维反幻觉确定性物理闭环
- **物理工作区快照比对（WorkspaceSnapshotDiffer）**：真实对比 Git 状态与物理文件哈希，彻底杜绝“说多做少”与“说少做多”。
- **沙盒影子空跑（Shadow Dry-Run）**：所有新变异的工具与技能脚本在部署前必须通过 AST 语法树解析与沙盒试运行，确认 Exit Code 0 后方可上线。
- **强制四段式履约协议**：必须按 `<facts_anchor>`、`<causal_chain>`、`<execution_manifest>`、`<final_conclusion>` 结构化输出。

### 5. 微信生态原生适配与多端直连
- **双通道接入**：
  - **个人号免公网扫码直连**：基于腾讯官方 iLink Bot 协议出站长轮询，无需公网 IP 与域名备案；
  - **企业号 / Webhook 回调接入**：支持标准企业微信机器人、ClawBot 及微信公众平台。
- **微信原生语音直提与同音字纠错**：
  - 原生提取微信转写的 `Recognition` 文本；
  - 内置基于拼音相似度与编辑距离的术语归一化（如“阿吉” $\to$ `agy`，“克劳德” $\to$ `claude`）。
- **终端 TUI / ANSI 序列净化**：将富文本控制字符、光标位移、动态 Spinner 实时重构为美观规范的 Markdown。

---

## 三、 快速上手与操作指南

### 1. 环境准备与依赖安装

系统要求 Linux 操作系统（推荐 Ubuntu 22.04+ 或 Debian 12+），Python 3.10+。

```bash
# 1. 克隆代码仓库
git clone https://github.com/MartinWZX/agyWechat.git
cd agyWechat

# 2. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖项
pip install -r requirements.txt
```

### 2. 环境配置 (`.env` 与 `config.yaml`)

复制环境配置模板并填写个人凭据：
```bash
cp .env.example .env
```

主要配置字段说明：
```ini
# --- 微信通道配置 ---
# 个人号 iLink 直连模式
WECHAT_ILINK_BOT_TOKEN="your_ilink_bot_token"
WECHAT_ILINK_BASE_URL="https://ilink.tencent.com/api"

# Webhook 模式 (若使用 Webhook)
WECHAT_WEBHOOK_KEY="your_webhook_key"
WECHAT_CORPID="your_corpid"
WECHAT_CORPSECRET="your_corpsecret"
WECHAT_AGENTID=1000002

# --- 默认 CLI 智能体引擎 ---
DEFAULT_CLI_ENGINE="agy"  # 可选: agy, pi, claude, omp, auto

# --- 安全与沙盒配置 ---
SAFE_ROOT_DIR="/home/martin/AI/agyWechat"
DANGEROUSLY_SKIP_PERMISSIONS=true
```

### 3. 一键运行与测试验证

```bash
# 执行全量单元测试与反幻觉测试 (44 测试项, 100% 通过)
./venv/bin/pytest tests/

# 启动单进程生产服务
./venv/bin/python main.py
```

终端将打印如下启动日志，代表各模块正常就绪：
```
INFO:     Single process flock acquired: /tmp/agent_core/locks/main.lock
INFO:     Module 1: WeChat Gateway (iLink + Webhook) initialized
INFO:     Module 3: MultiCLIHub registered engines: ['agy', 'pi', 'claude', 'omp']
INFO:     Module 5: WindControlGovernor initialized (Window: 5m/20, 1h/100)
INFO:     Module 7: CodeFirstCronScheduler started (Loaded 2 jobs)
INFO:     Module 13: EvolutionEngine Stage: STAGE_C_CONVERGENCE_CODE_FIRST
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 4. 微信端交互指令体验

| 交互类型 | 微信端发送内容示例 | 系统处理流程与行为 |
|---------|-------------------|-------------------|
| **L0 状态查询 (0 Token)** | `系统状态` 或 `status` | 命中本地 `skills/scripts/system_status.py`，0 毫秒立即返回 CPU/内存/引擎健康度 |
| **L0 风控看板 (0 Token)** | `风控看板` 或 `wind` | 命中本地 `skills/scripts/wind_control_dashboard.py`，返回滑动窗口水位与熔断器状态 |
| **L0 审计检查 (0 Token)** | `反幻觉审计` 或 `audit` | 命中本地 `skills/scripts/hallucination_audit.py`，秒级检查工作区文件完整度 |
| **原生语音输入识别** | 发送语音：“阿吉查一下系统状态” | 微信自动提取 `Recognition`，归一化纠错为 `agy status`，秒级响应 |
| **高频自适应固化** | 连续 3 次询问同一特定运维问题 | 第 3 次执行后自动固化为 L0 正则规则，后续再次询问消耗 0 Token |
| **周期任务固化** | 每隔固定周期（如整点）请求备份 | 统计间隔 $CV \le 0.25$，自动沉淀至 `scheduler/jobs.json` 定时静默执行 |
| **未知复杂编程任务** | `请帮我编写一个批量压缩图片的工具` | 判定为新领域任务，调度原生 CLI 智能体自主编写并注册到 `tools/` |

---

## 四、 生产部署方案 (Linux Systemd)

为了保证微信网关在无人值守的服务器上长期平稳运行，推荐使用 Systemd 守护进程配置：

1. 检查并编辑服务模板：`deploy/systemd/agy-wechat.service.example`
2. 部署到系统服务目录：
```bash
sudo cp deploy/systemd/agy-wechat.service.example /etc/systemd/system/agy-wechat.service
sudo systemctl daemon-reload
sudo systemctl enable agy-wechat
sudo systemctl start agy-wechat
```
3. 查看服务运行状态：
```bash
sudo systemctl status agy-wechat
journalctl -u agy-wechat -f
```

---

## 五、 项目工程目录速查

```
agyWechat/
├── gateway/          # 微信通信双网关、原生语音 Recognition 提取纠错与终端净化
├── compressor/       # L0 认知语义网关、零虚构前置检验、上下文无损压缩
├── bridge/           # 四大 CLI 原生驱动底座 (agy/pi/claude/omp) 与 PTY 维持
├── guard/            # 五维反风控状态机 (滑动窗口、高斯延迟、429退避、403硬熔断)
├── crystallizer/     # 高频意图热点追踪与 L0 正则规则自动固化器 (N>=3)
├── scheduler/        # 周期性时间序列分析与 Stage-1 纯代码 Cron 调度器 (CV<=0.25)
├── tools/            # 动态工具注册中心、AST 沙盒试运行、自动版本管理与热加载
├── skills/           # 四级技能管理体系、L0 运维脚本库、跨智能体同步
├── sandbox/          # 外置安全沙盒 (阻断越界目录与高危指令，补偿免确认执行)
├── verifier/         # 四维反幻觉物理校验器 (快照比对、影子运行、四段式履约协议)
├── memory/           # 核心记忆状态库 (SQLite WAL 模式与 FTS5 BM25 全文检索)
├── rsi/              # RSI 递归自我演进生命周期引擎与成熟度动态量化
└── telemetry/        # 系统健康度遥测监控
```

---
> **项目总结**：`agyWechat` 将工业级的代码工程严谨性与大模型的探索能力深度结合。通过“大模型开路、代码收敛巩固”的 RSI 机制，让智能体越用越快、越用越稳，最终将概率性的大模型调用率降至接近于零，打造真正坚不可摧的自动化生产力工具。
