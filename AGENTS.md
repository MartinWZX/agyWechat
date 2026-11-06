# Global Multi-Agent CLI Orchestrator Rules (agy / pi / claude / omp)

1. **Autonomous Unattended Execution (`dangerously-skip-permissions` Mode)**:
   - You are operating in an unattended headless production pipeline with `--dangerously-skip-permissions` (or `--no-confirm` / `--yes`) enabled by default.
   - NEVER ask the user for interactive confirmation (`[y/N]`) or wait for stdin input. Execute safe L1-L3 local tools, file reads, and sandboxed scripts autonomously in a single pass.
   - Strictly obey workspace boundaries: NEVER modify files outside the designated project workspace, NEVER read or exfiltrate `~/.ssh`, `~/.aws`, or raw OAuth token files, and NEVER execute destructive system-wide commands (`rm -rf /`, `mkfs`, `shutdown`).

2. **Strict Anti-Hallucination & Action-Speech Consistency Contract (CRITICAL)**:
   - **Zero Premise Fabrication (No Garbage-In or Garbled-ASR Reasoning)**: Base all reasoning strictly on the provided `[DATA_ANCHOR_MANIFEST]` and real tool outputs. If upstream data or WeChat voice recognition (`Recognition`) text is ambiguous, corrupted, or insufficient to prove causality, you MUST output `STATUS: INSUFFICIENT_EVIDENCE` and specify what data or clarification is needed. NEVER guess or invent causal explanations on top of faulty data or homophone-corrupted voice transcripts.
   - **No "Over-Claiming / Under-Doing" (严禁说多做少)**: NEVER claim you have created/modified a file, executed a command, or verified a fix unless you have actually invoked the tool, verified exit code `0`, and confirmed the physical change exists. Forbidden to leave `TODO`, `pass`, or mock placeholders in any file.
   - **No "Under-Claiming / Over-Doing" (严禁说少做多)**: ONLY modify files explicitly required by the current task. Every single file modification or command execution MUST be declared in `<execution_manifest>`. Any undeclared modification will be automatically rolled back by the host snapshot differ.
   - **Mandatory Three-Stage Logical Self-Consistency Output**:
     For every analytical, reasoning, or execution task, structure your response with:
     1) `<facts_anchor>`: List exact `[ANCHOR_ID]` and raw values from the input/tools.
     2) `<causal_chain>`: Step-by-step deduction where every claim explicitly cites `[ANCHOR_ID]`. Later steps MUST NOT contradict earlier facts or numerical polarities.
     3) `<execution_manifest>`: JSON list of `{"files_created": [...], "files_modified": [...], "commands_executed": [...]}` matching physical reality 100%.
     4) `<final_conclusion>`: User-facing summary strictly aligned with `<causal_chain>`.

3. **Minimal Turn & Anti-Loop Constraint (Anti-429)**:
   - Complete every cognitive task in the minimum possible turns (strictly <= 3 tool call turns).
   - NEVER enter retry loops if a command or tool fails twice; immediately stop and return a structured error summary.

4. **Throttled Subagent Concurrency (`invoke_subagent` / `Task` / Multi-Agent)**:
   - Only spawn subagents when parallel investigation or isolated workspace execution is strictly necessary.
   - **MAX PARALLEL SUBAGENTS = 2** per invocation to prevent account rate-limit bursts (HTTP 429).
   - Always assign lightweight models (`flash` / `haiku` / fast tier) to subagents unless deep architectural reasoning is mandatory.
   - Use read-only mode (`research`) for log/code/doc inspection, and isolated branches (`Workspace="branch"` or git worktree) for any file modifications.
   - Every subagent MUST return ONLY a concise conclusion summary (under 300 words) and NEVER dump raw logs into the parent context.

5. **Auto-Crystallization Output Contract**:
   - Whenever you resolve a recurring query (via text or voice), fixed prompt pattern, or routine data task, append a deterministic, complete, zero-placeholder Python script block wrapped in `<crystallize_script name="..." trigger_regex="..." voice_homophones="..." cron="...">...</crystallize_script>` so the host system can shadow-test and execute it purely in code next time without invoking any LLM CLI.
