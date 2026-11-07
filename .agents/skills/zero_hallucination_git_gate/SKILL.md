---
name: zero-hallucination-git-gate
description: Deterministic pre-commit/pre-push Git zero-leakage gate and Python AST anti-hallucination contract auditor. Run before staging, committing, or refactoring Python modules.
---

# Zero-Hallucination AST & Git Zero-Leakage Gate (`v1.0.0`)

## Overview

Provides deterministic local code inspection enforcing:
1. **Git Zero-Leakage Pre-Push Gate**: Scans `git diff --cached` for prohibited sensitive file paths (`.env*`, private keys, SQLite/CSV/Parquet data dumps, vector DBs, logs, uncleaned `.ipynb` notebooks) and hardcoded credentials (`sk-`, `ghp_`, `AKIA`, PEM headers, password-bearing URIs).
2. **Python AST Anti-Hallucination Audit**: Detects `pass` and ellipsis stubs, mutable default arguments (`=[]`, `={}`), blocking `time.sleep` / `requests` calls inside `async def`, and silent exception swallowing.

## Deterministic L0 Entrypoint

Invoke via `SkillManager.execute_l0_script("skills/scripts/zero_hallucination_git_gate.py", workspace_dir=".", target_files=["config.py"])` or via `ToolRegistry.execute("git_zero_leakage_audit", workspace_dir=".")`.
