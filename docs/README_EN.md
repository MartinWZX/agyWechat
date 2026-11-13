# Autonomous Multi-CLI Agent (Code-First, LLM-Last)

[简体中文](../README.md) | [English](README_EN.md)

A production-grade unified multi-agent CLI orchestration system built on the **"Code-First, LLM-Last"** paradigm. It natively integrates Google `agy`, Pi `pi`, Claude Code `claude`, and Oh-My-Pi `omp` with autonomous `dangerously-skip-permissions` execution, four-dimensional physical anti-hallucination verification, and native WeChat voice recognition extraction.

---

## 📁 Project Layout

The repository root stays strictly minimal, with core domains encapsulated into dedicated modules and auxiliary documentation, scripts, and deployment files organized into subdirectories:

```text
agyWechat/
├── main.py                      # Single-process host entrypoint (flock lock, FastAPI & hub setup)
├── config.py                    # Unified configuration bridge (Pydantic Settings + YAML loader)
├── config.yaml                  # Production core config (CLI paths, security thresholds, homophones)
├── run.sh                       # One-click host startup script
├── requirements.txt             # Python dependency manifest
├── .env / .env.example          # Environment variables and template
│
├── AGENTS.md                    # Cross-engine orchestrator rules (anti-hallucination & execution safety)
├── GEMINI.md                    # -> Loaded by Google agy at startup
├── CLAUDE.md                    # -> Loaded by Claude Code at startup
│
├── docs/                        # 📚 Architecture & design documentation
│   ├── ARCHITECTURE.md          # Detailed system architecture whitepaper
│   ├── PROJECT_INTRODUCTION.md  # Project background & evolution guide
│   └── README_EN.md             # English README
│
├── scripts/                     # 🛠️ Operations & login utilities
│   └── login_wechat.py          # Interactive WeChat QR login helper (Tencent iLink protocol)
│
├── deploy/                      # 🚀 Deployment manifests
│   └── systemd/                 # Sample systemd service unit
│       └── agy-wechat.service.example
│
├── gateway/                     # Module 1: WeChat dual gateways, voice Recognition & output cleanup
│   ├── wechat_gateway.py        # Message bus & L0 deterministic rule dispatch
│   ├── voice_normalizer.py      # Native WeChat voice recognition extraction & homophone mapping
│   ├── ilink_gateway.py         # Tencent iLink long-polling bot gateway & chunked pusher
│   └── terminal_cleaner.py      # Terminal ANSI/VT100 cleaner & WeChat message splitter
├── crystallizer/                # Module 2: Pattern mining & automated skill crystallization
│   └── pattern_crystallizer.py  # Pattern extraction, slot collision guard & eval admission
├── scheduler/                   # Module 3: Two-stage code-first Cron scheduler
│   ├── code_first_cron.py       # Stage-1 deterministic execution + Stage-2 cognitive escalation
│   └── jobs.json                # Persisted Cron job registry
├── guard/                       # Module 4: Per-engine security guard (quota & circuit-breaker safety)
│   └── wind_control_governor.py # Sliding-window rate limiting, safe throttling & tri-state circuit breaker
├── bridge/                      # Module 5: Multi-CLI hub & failover routing
│   ├── multi_cli_hub.py         # Priority concurrency gate & cross-engine failover
│   └── drivers/                 # Native CLI driver implementations
│       ├── base_driver.py       # Abstract driver contract (one-liner & persistent PTY modes)
│       ├── agy_driver.py        # agy driver
│       ├── pi_driver.py         # pi driver
│       ├── claude_driver.py     # claude driver
│       └── omp_driver.py        # omp driver
├── compressor/                  # Module 6: Cognitive admission gate & context compression
│   └── cognitive_gate.py        # Dirty-input rejection & [DATA_ANCHOR_MANIFEST] assembly
├── memory/                      # Module 7: SQLite WAL state store & three-tier memory
│   └── core_state_store.py      # Short-term sessions, FTS5 episodic recall & audit tables
├── skills/                      # Module 8: Four-tier skill manager & L0 deterministic scripts
│   ├── skill_manager.py         # Skill loader & lifecycle promoter
│   └── scripts/                 # Deterministic Python utility scripts (status, dashboard, audit)
├── tools/                       # Module 9: Deterministic tool registry & trace auditing
│   └── tool_registry.py         # Out-of-process sandboxed tools, AST checks & trace logging
├── sandbox/                     # Module 10: AST sandbox security guard & L1-L5 approval flow
│   └── approval_guard.py        # Dangerous command blocking & WeChat safety confirmation
├── telemetry/                   # Module 11: End-to-end observability & runtime dashboard
│   └── health_monitor.py        # Code offload metrics & security health dashboard
├── verifier/                    # Module 12: Four-dimensional anti-hallucination verification engine
│   └── hallucination_guard.py   # SHA-256 snapshot differ, stub detection & consistency verifier
└── rsi/                         # Module 13: Recursive Self-Improvement (RSI) orchestrator
    └── evolution_engine.py      # Drives LLM-First -> Code-First lifecycle convergence
```

---

## ⚡ Core Design Principles

1. **Minimal Root Directory**: The repository root keeps only entrypoints (`main.py`), core configs (`config.yaml`), environment definitions (`.env`), and orchestrator rules (`AGENTS.md`), organizing all documentation, scripts, and deployment assets into dedicated subdirectories.
2. **Code Offload Rate $\ge 90\%$ (Design Target)**: Routine queries and frequent WeChat text/voice requests are intercepted by L0 rules and handled deterministically by local Python scripts, reducing token usage while ensuring runtime safety and zero hallucination.
3. **Closed-Loop Physical Anti-Hallucination**: Pre/post workspace SHA-256 snapshot diffing intercepts both under-execution and undeclared modifications (automatically rolling back unauthorized file edits).
4. **Dual-Mode Native CLI Scheduling**: Dispatches official `agy`, `pi`, `claude`, and `omp` binaries in one-liner or persistent PTY modes with automatic non-interactive `--dangerously-skip-permissions` execution protected by external sandbox guardrails.
