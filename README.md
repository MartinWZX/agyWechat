# Autonomous Multi-CLI Agent (Code-First, LLM-Last)

基于 **“代码优先、大模型兜底（Code-First, LLM-Last）”** 架构范式的生产级多智能体 CLI 统一编排系统。原生对接 Google `agy`、Pi `pi`、Claude Code `claude`、Oh-My-Pi `omp`，默认开启 `dangerously-skip-permissions` 全自主免确认执行，内置四维反幻觉物理校验与微信原生语音识别直提。

---

## 📁 优化后的项目目录组织架构 (Clean Project Layout)

项目根目录保持高度整洁，核心功能采用领域模块化封装，非核心文件按功能归档至对应子目录：

```text
agyWechat/
├── main.py                      # 全局唯一单进程主入口（flock 锁、FastAPI 与总线初始化）
├── config.py                    # 统一配置桥接器（Pydantic Settings + YAML 加载）
├── config.yaml                  # 生产级核心配置（CLI 路径、风控阈值、同音字表）
├── run.sh                       # 宿主机一键启动脚本
├── requirements.txt             # Python 核心依赖清单
├── pytest.ini                   # 测试套件配置文件
├── .env / .env.example          # 环境配置与示例
│
├── AGENTS.md                    # 跨引擎统一主控规范（防幻觉、防 429 铁律）
├── GEMINI.md                    # -> 供 Google agy 启动读取
├── CLAUDE.md                    # -> 供 Claude Code 启动读取
│
├── docs/                        # 📚 项目架构与设计文档归档
│   ├── ARCHITECTURE.md          # 详细系统架构白皮书
│   └── PROJECT_INTRODUCTION.md  # 项目背景与演进介绍
│
├── scripts/                     # 🛠️ 运维部署与登录工具脚本
│   └── login_wechat.py          # 微信登录交互脚本（腾讯官方 iLink 扫码）
│
├── deploy/                      # 🚀 部署配置
│   └── systemd/                 # systemd 守护服务单元文件示例
│       └── agy-wechat.service.example
│
├── gateway/                     # 模块 1：微信消息网关、原生语音 Recognition 直提与同音纠错
│   ├── wechat_gateway.py        # 微信消息总线与 L0 规则直回
│   ├── voice_normalizer.py      # 微信语音识别解析与拼音同音映射
│   ├── ilink_gateway.py         # 微信 iLink 官方机器人协议与长轮询接入
│   └── terminal_cleaner.py      # 终端 ANSI 转义字符清洗与微信文本分片
├── crystallizer/                # 模块 2：文字/语音特征挖掘与自动固化引擎
│   └── pattern_crystallizer.py  # 模式提取与影子实测准入
├── scheduler/                   # 模块 3：二段式代码优先定时调度器
│   ├── code_first_cron.py       # Stage-1 纯代码 + Stage-2 认知唤醒
│   └── jobs.json                # 定时任务注册表
├── guard/                       # 模块 4：多引擎独立风控守卫（Anti-429 / Anti-403）
│   └── wind_control_governor.py # 滑动窗口限流、拟人延迟与三态熔断
├── bridge/                      # 模块 5：多智能体 CLI 桥接中心与 Failover 路由
│   ├── multi_cli_hub.py         # 优先级调度与跨引擎热备切换
│   └── drivers/                 # CLI 原生驱动实现
│       ├── base_driver.py       # 抽象驱动（单行命令与常驻 PTY 双模）
│       ├── agy_driver.py        # agy 驱动
│       ├── pi_driver.py         # pi 驱动
│       ├── claude_driver.py     # claude 驱动
│       └── omp_driver.py        # omp 驱动
├── compressor/                  # 模块 6：认知准入守门员与源头数据预检
│   └── cognitive_gate.py        # 脏数据/残缺语音拦截与 [DATA_ANCHOR_MANIFEST]
├── memory/                      # 模块 7：SQLite WAL 单写连接状态机与三层记忆
│   └── core_state_store.py      # 短期会话、FTS5 长期记忆与审计表
├── skills/                      # 模块 8：四级技能管理与 L0 纯代码脚本库
│   ├── skill_manager.py         # 技能层级加载器
│   └── scripts/                 # 确定性脚本库（system_status, dashboard, audit 等）
├── tools/                       # 模块 9：确定性工具注册表与 Trace 审计
│   └── tool_registry.py         # 工具执行记录与 Python/CLI/MCP 双模直调
├── sandbox/                     # 模块 10：AST 沙盒安全守卫与微信 L1-L5 审批流
│   └── approval_guard.py        # 危险命令阻断与高危操作微信确认
├── telemetry/                   # 模块 11：全链路可观测性与运行看板
│   └── health_monitor.py        # 纯代码卸载率与风控看板
├── verifier/                    # 模块 12：全链路四维反幻觉确定性审计引擎
│   └── hallucination_guard.py   # 物理快照差分、逻辑自洽与影子实测校验
├── rsi/                         # 模块 13：RSI 递归自演进总控引擎
│   └── evolution_engine.py      # 驱动 LLM-First -> Code-First 全周期生命周期演进
└── tests/                       # 🧪 自动化测试套件
    ├── test_code_first_system.py # 核心系统与全链路集成测试
    ├── test_full_system.py       # 12 核心模块综合测试
    ├── test_rsi_evolution.py     # RSI 自演进与时序 Cron 固化测试
    ├── test_ilink_gateway.py     # 微信 iLink 协议与 Poller 测试
    └── test_terminal_cleaner.py  # 终端 ANSI 与分片算法测试
```

---

## ⚡ 核心设计原则

1. **根目录极致清爽**：根目录仅保留入口文件（`main.py`）、主配置文件（`config.yaml`）、环境定义（`.env`）与规范规范（`AGENTS.md`），所有辅助文件、文档、旧版代码与部署资产均归整入专用子目录。
2. **纯代码卸载率 $\ge 90\%$**：常规查询与高频微信语音/文字消息经由 L0 规则拦截，100% 由本地 Python 脚本毫秒级处理，0 Token 消耗，彻底消除 429 风控与幻觉。
3. **物理反幻觉闭环**：通过物理状态前后快照差分（Git / Hash Diff），拦截“说多做少”与“说少做多”（自动回滚越权篡改）。
4. **单行式与常驻 PTY 双模调度**：支持 `agy`、`pi`、`claude`、`omp` 官方二进制驱动，自动注入 `--dangerously-skip-permissions` 免确认执行。

---

## 🧪 运行测试套件

```bash
./venv/bin/pytest tests/
```
