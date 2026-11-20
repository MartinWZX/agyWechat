# agyWechat — Pure Two-State WeChat Router & Native Four-Engine AI CLI Gateway

[简体中文](../README.md) | [English](README_EN.md)

**agyWechat** is a pure **Two-State Separation WeChat Gateway** (Official Account Webhook + Personal WeChat Tencent iLink Bot) combining a deterministic host/workspace command viewer with 100% zero-intervention native passthrough to four local AI CLI agents: **Google Antigravity (`agy`)**, **Pi (`pi`)**, **Claude Code (`claude`)**, and **Oh-My-Pi (`omp`)**.

Core principle: **Pure text command router outside engines; 100% native AI agent passthrough inside engines** — no gateway TUI/subcommand interception, no entry-time CLI discovery crawlers or PTY probes, no host environment stripping, zero 429/403 circuit-breaking or rate-limit cooldowns, and verbatim ANSI-cleaned stdout/stderr forwarding.

---

## 📁 Minimal Project Layout

```text
agyWechat/
├── main.py                      # Single-instance flock entrypoint, FastAPI Webhook, / & /health, iLink worker lifecycle
├── config.py                    # Unified Pydantic Settings & YAML loader (.env > config.yaml > defaults)
├── config.yaml                  # Engine binaries, skip-permission flags, default models & timeouts
├── run.sh                       # Convenience launcher
├── requirements.txt             # Core Python dependencies
├── pytest.ini                   # Pytest configuration
├── .env.example                 # Environment variable template
├── AGENTS.md / CLAUDE.md / GEMINI.md # Workspace agent behavior rules
│
├── gateway/                     # Dual WeChat channels, native Recognition extraction & terminal cleaning
│   ├── wechat_gateway.py        # Two-state voice/text boundary & direct native CLI passthrough
│   ├── ilink_client.py          # Tencent iLink Bot HTTP client (QR login, getupdates, sendmessage)
│   ├── ilink_gateway.py         # Long-polling iLink worker, thread-safe ContextTokenStore & single-pass chunk pusher
│   └── terminal_cleaner.py      # ANSICleaner (ANSI/spinner/CR sanitizer) & WeChatSplitter (fence-safe splitter)
│
├── router/                      # Pure two-state finite state machine
│   ├── pipeline.py              # DeterministicRouter facade (Non-Engine Router <-> In-Engine Passthrough)
│   ├── engine_session_router.py # EngineSessionRouter (outer commands, instant entry, strictly /exit /clear /help inside)
│   ├── engine_session_presenter.py  # Presenter helpers for outer help, /engines table, instant welcome & minimal help
│   └── workspace_navigator.py   # Safe workspace directory validation (blocks /etc, ~/.ssh, etc.)
│
├── bridge/                      # Direct zero-interception CLI drivers & hub
│   ├── multi_cli_hub.py         # Direct zero-lockout hub passing native stdout/stderr back verbatim
│   └── drivers/                 # AgyDriver, PiDriver, ClaudeDriver, OmpDriver -> BaseCLIAgentDriver (os.environ.copy())
│
├── memory/                      # SQLite WAL persistence (strictly 2 core tables)
│   ├── core_state_store.py      # working_sessions, messages (auto-drops legacy tables/FTS + VACUUM)
│   ├── engine_session_repository.py # Persisted user engine, workspace & native conversation_id
│   └── sqlite_utils.py          # Self-closing SQLite WAL connection helper
│
├── telemetry/                   # Live host metrics & gateway observability
│   └── health_monitor.py        # Real CPU loadavg, /proc/meminfo RAM, disk usage, uptime & installed engines
│
├── scripts/                     # CLI utilities (login_wechat.py, wsl_service.sh)
└── tests/                       # Automated Pytest suite
```

---

## 🧭 Two-State Separation Contract

1. **State 1 — Non-Engine Router State (`active_engine == ""`)**:
   - **Voice Strictly Forbidden**: Inbound WeChat voice messages (`msg_type == "voice"`) are rejected immediately with text instructions.
   - **Text Management Commands & Asset Drop-off**: `/help`, `/status` (live host CPU/RAM/disk/uptime + installed engines), `/engines` (install status/version, binary path & session state), `/export [engine]` / `/engines audit [engine]` (per-engine command audit JSON export), `/pwd`, `/workspace <path>`, `/ls [subpath]`, `/agy`, `/pi`, `/claude`, `/omp` (instant `is_installed()` check only), plus per-user isolated default workspaces (`USER_WORKSPACES`) and automatic `.wechat_assets/` persistence for inbound images/files.
2. **State 2 — In-Engine Native Autonomous State (`active_engine in {"agy", "pi", "claude", "omp"}`)**:
   - **Only 3 Gateway Control Commands**: `/exit` (return to Non-Engine state), `/clear` (reset gateway-tracked `conversation_id` for a fresh thread), and `/help` (show active engine, version, bound workspace, and control hints).
   - **100% Native Passthrough + Streaming Progress**: Every other text message, slash command, verbatim WeChat native `Recognition` transcript, or captioned image/file attachment passes directly to `asyncio.create_subprocess_exec(..., env=os.environ.copy(), start_new_session=True)`, optionally emitting periodic stage progress summaries (`PROGRESS_PUSH_INTERVAL_SECONDS`) and returning ANSI-cleaned stdout/stderr verbatim.

---

## 🚀 Quick Start & Tests

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
./venv/bin/pytest -v tests/
```
