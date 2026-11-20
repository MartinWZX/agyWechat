# agyWechat Multi-Engine CLI Workspace Rules (`agy` / `pi` / `claude` / `omp`)

1. **System Identity & Pure Two-State Non-Intervention**:
   - **agyWechat** is a pure **Two-State Separation WeChat Gateway**:
     - **State 1 — Non-Engine Gateway Router (`active_engine == ""`)**: Text-only command gateway for host hardware metrics, workspace navigation, installed-engine inspection, and explicit instant entry (`/agy`, `/pi`, `/claude`, `/omp`).
     - **State 2 — In-Engine Native Autonomous Session (`active_engine in {"agy", "pi", "claude", "omp"}`)**: Gateway handles **strictly 3 control commands** (`/exit`, `/clear`, `/help`); **100% of all other inputs** (natural language text, any other slash command/flags, and verbatim WeChat `Recognition` voice transcripts) pass directly to the active native CLI subprocess with full host environment passthrough, zero gateway rate-limiting, zero 429/403 circuit-breaking/cooldown/lockdown, zero error-classifying interception, zero discovery crawlers, zero context compression, zero semantic caching, and zero artificial concurrency/turn caps (`max_turns: 0`, `timeout_seconds: 0`).
   - Non-zero CLI outputs and `stderr` diagnostics are ANSI-cleaned and returned verbatim to WeChat so the user sees native CLI feedback directly.

2. **Two-State Routing & Voice Boundary**:
   - **Non-Engine State (`active_engine == ""`)**: Strictly forbid voice control (`msg_type == "voice"`); accept only text management commands (`/help`, `/status`, `/engines`, `/pwd`, `/workspace <path>`, `/ls [subpath]`, `/agy`, `/pi`, `/claude`, `/omp`). Engine entry validates binary installation only (`is_installed` via `resolve_binary`) with zero crawlers or PTY probes.
   - **In-Engine State (`active_engine in {"agy", "pi", "claude", "omp"}`)**: Permit WeChat voice messages by forwarding WeChat native `Recognition` transcripts verbatim to the active CLI engine (abort with an explicit error notice if `Recognition` is empty—never synthesize placeholder text). Gateway intercepts only `/exit`, `/clear`, and `/help`.

3. **Evidence-Based Coding & Anti-Hallucination Discipline**:
   - Verify exact symbol names, signatures, and behavior directly in source files before calling across modules.
   - Never leave `TODO`, `pass`, dummy data stubs, or silent bare `except` blocks in production code.
   - Report only observed file paths, symbols, and post-edit test/command results.

4. **Workspace, Host Environment & Resource Hygiene**:
   - `WorkspaceNavigator.validate_path` blocks directory navigation into `~/.ssh`, `~/.aws`, `~/.config/gcloud`, or system directories (`/etc`, `/proc`, `/root`, etc.).
   - Subprocess CLI invocations execute directly via `asyncio.create_subprocess_exec(*cmd, cwd=workspace_dir, env=os.environ.copy(), ...)` without shell wrappers, preserving the full native CLI host environment (API keys, credentials, git auth, and native agent tools).
   - Centralize configuration loading in `config.py` (`Settings` + `resolve_setting_or_yaml`) and manage SQLite connections with `memory.sqlite_utils.connect` (`ClosingConnection`) strictly over the 2-table store (`working_sessions`, `messages`).

5. **Zero-Leakage Git Protocol**:
   - Never run `git add .`, `git add -A`, `git commit -a`, `--no-verify`, or `git push --force` to protected branches. Stage explicit paths (`git add <file>`).
   - Never commit `.env*` (except `.env.example`), `*.db*`, `*.log`, private keys (`*.pem`, `*.key`, `*credentials*.json`), hardcoded API keys, `.context_tokens.*`, or scratch files.
