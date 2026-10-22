"""Default configuration data for Son of Anton Agent.

Pure-data leaf module: DEFAULT_CONFIG and OPTIONAL_ENV_VARS, extracted
verbatim from son_of_anton_cli/config.py. Must not import from son_of_anton_cli.config.
"""

DEFAULT_CONFIG = {
    "model": "",
    "providers": {},
    "toolsets": ["son-of-anton-cli"],
    # SQLite journal mode used by every Son of Anton database opener. WAL is the
    # normal default; set DELETE for weak-fsync/shared filesystems where WAL is
    # not crash-safe (for example macOS virtiofs, NFS, or SMB).
    "database": {
        "journal_mode": "wal",
    },
    # Soft file-descriptor limit for long-running Son of Anton server processes.
    # Clamped to the OS hard limit; 0/false/null disables the adjustment.
    "runtime": {
        "nofile_soft_limit": 4096,
    },
    # Global active chat session cap across CLI, TUI, and messaging.
    # None/0 = unbounded.
    "max_concurrent_sessions": None,
    # Soft LRU cap on in-memory sessions. When more than
    # this many are live, the gateway evicts the least-recently-active DETACHED
    # sessions (no live client) so accumulated agents don't pile up under memory
    # pressure. Reopening one re-resumes it from disk. 0/null disables.
    "max_live_sessions": 16,
    "session": {
        # Per-session JSONL journal under $SON_OF_ANTON_HOME/journals/: written
        # as messages are persisted, holding the reasoning and tool detail the
        # transcript clips — a plain greppable file for you and the model.
        # Set false to stop writing journals.
        "journal": True,
    },
    "agent": {
        # Unlimited by default. The agent turn cap caused more problems than
        # it solved (silent mid-task truncation). null = unlimited; set a
        # positive integer to cap, or use "none"/"unlimited"/"inf"/0/-1 —
        # all normalized by son_of_anton_cli.config.resolve_turn_limit.
        "max_turns": None,
        # Optional wall-clock budget in seconds per conversation run.
        # null/absent = feature fully off (zero behavior change). When set,
        # the agent gets a one-time wrap-up notice at 80% elapsed and
        # implicit provider stale timeouts are capped to the remaining
        # budget. CLI one-shot equivalent: `son-of-anton chat --run-budget N`.
        "run_budget_seconds": None,
        # Inactivity timeout for gateway agent execution (seconds).
        # The agent can run indefinitely as long as it's actively calling
        # tools or receiving API responses.  Only fires when the agent has
        # been completely idle for this duration.  0 = unlimited.
        "gateway_timeout": 1800,
        # Maximum time an alias routing key waits for the active turn holding
        # the same resolved session lease. On expiry the inbound message is
        # rejected with a resend notice rather than run without serialization.
        # Non-positive values fall back to 1800 seconds.
        "gateway_turn_lease_timeout": 1800,
        # Per-session AIAgent cache in the gateway. Each cached agent keeps a
        # warm prompt prefix AND the session's full transcript, so the cache
        # trades memory for cost: too small and every turn re-pays an uncached
        # prompt, too large and tool-heavy transcripts fill the heap.
        "agent_cache": {
            # LRU entry cap.
            "max_size": 128,
            # Evict an agent that has been idle this long (seconds).
            "idle_ttl_secs": 3600,
            # Anonymous-RSS budget (MB) above which the gateway starts shedding
            # least-recently-used transcripts, which reload from the persisted
            # session on the next turn. "auto" derives the budget from the
            # cgroup memory limit the gateway runs under (or total RAM when
            # uncapped); a number sets it explicitly; 0/off disables the pass
            # and lets memory grow to whatever the two bounds above allow.
            "memory_high_mb": "auto",
            # Upper bound on how many sessions one pressure pass sheds, so a
            # burst of teardowns cannot stall the gateway.
            "max_evictions_per_pass": 16,
            # Most-recently-used sessions the pressure pass never touches —
            # they are the ones actively paying for a warm prompt cache.
            "protect_recent": 8,
        },
        # Force-interrupt budget once gateway stop()/drain has begun
        # (seconds). Applies to SIGTERM/external stop and to the final
        # phase of in-band restart after any after-turn wait. 0 = interrupt
        # immediately (the default).
        #
        # Keep this short and under systemd TimeoutStopSec — a long value
        # here invites SIGKILL-mid-cleanup. For in-band restart
        # (/restart, SIGUSR1), prefer restart_after_turn_timeout below so
        # active turns finish *before* stop() begins (#77184).
        "restart_drain_timeout": 0,
        # Cron-only floor under the stop()/drain wait (seconds). A chat turn
        # interrupted by a restart is announced to the user and resumed on
        # their next message; an interrupted cron run is written to jobs.json
        # as a permanent failure that nobody is waiting on, so it must not
        # inherit restart_drain_timeout's 0 (#82161). Clamped at runtime to
        # the shutdown-watchdog leash minus teardown headroom, so raising it
        # past ~50s has no effect unless TimeoutStopSec is raised too.
        # 0 = opt out (cron drains on restart_drain_timeout, legacy).
        "cron_drain_timeout": 30,
        # In-band restart wait for active turns to finish before stop()
        # (seconds). /restart and SIGUSR1 refuse new work, then wait up to
        # this cap for in-flight agents/cron/api runs to complete naturally
        # so the requesting turn is not amputated by restart_drain_timeout.
        # 0 = legacy behaviour (enter stop()/drain immediately). Default
        # 30 min is a safety valve for wedged agents, not a target latency —
        # an interactive `son-of-anton gateway restart` must never block for hours
        # on a turn that wedged (#79133). Long unattended turns can raise
        # this in config.toml.
        "restart_after_turn_timeout": 1800,
        # Upper bound (seconds) a submitted prompt waits for the deferred
        # agent build (MCP discovery, model metadata) before
        # failing with a visible error (#63078). The gateway's wait is
        # patient — the prompt is delivered the moment the build completes
        # and a progress notice is emitted past 30s — so this cap only fires
        # on a genuinely hung build. Raise it for deployments with many slow
        # or unreachable MCP servers.
        "build_wait_timeout": 600,
        # Max app-level retry attempts for API errors (connection drops,
        # provider timeouts, 5xx, etc.) before the agent surfaces the
        # failure.  The OpenAI SDK already does its own low-level retries
        # (max_retries=2 default) for transient network errors; this is
        # the Son of Anton-level retry loop that wraps the whole call.  Lower
        # this to 1 if you use fallback providers and want fast failover
        # on flaky primaries; raise it if you prefer to tolerate longer
        # provider hiccups on a single provider.
        "api_max_retries": 3,
        # Empty-response retry guard (NS-503).  The empty-retry loop
        # re-sends the full conversation input at full price on every
        # attempt; these settings stop it from re-billing *deterministic*
        # empties (unsignaled provider refusals with zero output tokens)
        # while failing open on any ambiguous evidence (missing usage,
        # any generated tokens, model/provider change mid-streak).
        "empty_response_guard": {
            # Master switch for both guards below. False restores the
            # legacy fixed 3-retry behaviour unconditionally.
            "enabled": True,
            # When the estimated input cost of a single empty attempt
            # meets or exceeds this many USD, the retry budget for the
            # streak drops from 3 to 1. Unknown pricing or missing usage
            # leaves the budget untouched.
            "cost_threshold_usd": 0.25,
        },
        # Tool-use enforcement: injects system prompt guidance that tells the
        # model to actually call tools instead of describing intended actions.
        # Values: "auto" (default — applies to gpt/codex models), true/false
        # (force on/off for all models), or a list of model-name substrings
        # to match (e.g. ["gpt", "codex", "gemini", "qwen"]).
        "tool_use_enforcement": "auto",
        # Execution-discipline guidance: injects a system prompt block covering
        # tool persistence, mandatory tool use for arithmetic/system facts,
        # external-write read-back, count reconciliation, literal preservation
        # of identifiers, and verification-gated completion.  Chosen once at
        # session start keyed on model name (prompt stays byte-stable).
        # Values: "auto" (default — applies to gpt/codex/grok/deepseek/kimi/
        # qwen/glm/minimax/mimo/mistral models), true/false (force on/off for
        # all models), or a list of model-name substrings to match.
        "execution_guidance": "auto",
        # Runtime anti-stall guards. When True (default), two conservative
        # guards run: (1) an identical-call loop breaker that appends a short
        # notice to the tool result when the same tool is called 3+ consecutive
        # times with identical arguments AND identical results (never blocks;
        # pollers like `process` are exempt), and (2) a continue-intent
        # extension of the empty-response recovery that re-prompts once when
        # the model ends its turn saying it will continue but takes no action.
        # Set False to disable both.
        "stall_guards": True,
        # Universal "finish the job" guidance — short prompt block applied to
        # all models that targets two cross-family failure modes: (1) stopping
        # after a stub instead of finishing the artifact, (2) fabricating
        # plausible-looking output when a real path is blocked.  Costs ~80
        # tokens in the cached system prompt.  Set False to disable globally.
        "task_completion_guidance": True,
        # Universal parallel-tool-call guidance — short prompt block applied to
        # all models that tells the model to batch independent tool calls
        # (reads, searches, web fetches, read-only commands) into one turn
        # instead of one call per turn.  The runtime already runs independent
        # calls concurrently, so this just steers the model to produce the
        # batch — cutting round-trips and the resent-context cost that
        # compounds over a long conversation.  Costs ~70 tokens in the cached
        # system prompt.  Set False to disable globally.
        "parallel_tool_call_guidance": True,
        # Local-environment toolchain probe — surfaces Python/pip/uv/PEP-668
        # state in the system prompt when something non-default is detected
        # (e.g. python3 has no pip module, pip→python version mismatch, PEP
        # 668 enforcement without uv).  Costs zero tokens when the env is
        # clean (probe emits nothing).  Skipped for remote terminal backends
        # (ssh — it has its own probe).  Set False to
        # disable entirely.
        "environment_probe": True,
        # Coding posture — on interactive coding surfaces (CLI, TUI, desktop
        # app, ACP) in a code workspace, Son of Anton adds a coding operating brief
        # + a live git/workspace snapshot to the system prompt. See
        # agent/coding_context.py.
        #   "auto" (default) — prompt-only posture when the surface is
        #                      interactive AND cwd is a code workspace.
        #                      Toolsets are never touched; messaging platforms
        #                      unaffected.
        #   "on"             — force the prompt posture everywhere.
        #   "off"            — disable entirely.
        "coding_context": "auto",
        # Staged inactivity warning: send a warning to the user at this
        # threshold before escalating to a full timeout.  The warning fires
        # once per run and does not interrupt the agent.  0 = disable warning.
        "gateway_timeout_warning": 900,
        # Clarify questions wait for the user's answer, on every surface:
        # 0 = no timeout, and the agent does nothing until they answer (it
        # used to "use its best judgement and proceed" after a deadline and
        # had already acted by the time they replied).  A positive value
        # opts into a deadline in seconds, after which the agent is told the
        # user has not answered and ends its turn to wait for their reply.
        # The user can always end a pending question with /stop or /new.
        "clarify_timeout": 0,
        # Seconds an unanswered clarify question or command approval waits
        # before a desktop notification (notify-send) reminds the user the
        # agent is blocked on them; CLI/TUI only. 0 = no notification.
        "prompt_notify_after": 120,
        # Periodic "still working" notification interval (seconds).
        # Sends a status message every N seconds so the user knows the
        # agent hasn't died during long tasks.  0 = disable notifications.
        # Lower values mean faster feedback on slow tasks but more chat
        # noise; 180s is a compromise that catches spinning weak-model runs
        # (60+ tool iterations with tiny output) before users assume the
        # bot is dead and /restart.
        "gateway_notify_interval": 180,
        # Session stall watchdog (seconds). Scope (#76354): this is a
        # RECOVERY notifier for an in-process AIAgent that has an
        # adapter-queued follow-up (pending inbound / queued event) while its
        # activity clock is stale — NOT a general gateway/session stall
        # detector. It does not observe startup restoration, build sentinels,
        # turn leases, debounce state, or work owned by another process; the
        # scan cadence is per AIAgent instance, not globally coordinated per
        # durable session. Notify-only: warns the user to try /new. Distinct
        # from gateway_timeout (which kills the turn) and
        # gateway_notify_interval ("still working" heartbeats). 0 = disable.
        "session_stall_timeout": 300,
        # Long-lived reconnect-loop escalation (seconds). A platform that has
        # been continuously failing/reconnecting for this long gets
        # needs_attention flagged in gateway runtime status (visible in
        # `son-of-anton status` / fleet monitoring). Retries never stop — this is a
        # signal, not a circuit breaker. 0 = disable.
        "reconnect_attention_after": 7200,
        # Freshness window for the gateway auto-continue note (seconds).
        # After a gateway crash/restart/SIGTERM mid-run, the next user
        # message gets a "[System note: your previous turn was
        # interrupted — process the unfinished tool result(s) first]"
        # prepended so the model picks up where it left off.  That's the
        # right behaviour while the interruption is fresh, but stale
        # markers (transcript last touched hours or days ago) can revive
        # an unrelated old task when the user's next message starts new
        # work.  This window is the max age of the last persisted
        # transcript row for which we still inject the continue note.
        # Default 3600s comfortably covers a long turn (gateway_timeout
        # default is 1800s) plus runtime slack.  Set to 0 to disable the
        # gate and restore pre-fix behaviour (always inject).
        "gateway_auto_continue_freshness": 3600,
        # Max seconds the gateway waits for boot auto-resume turns to finish
        # before it releases the startup-restore inbound gate.  While startup
        # restore is in progress the gateway QUEUES every inbound message
        # instead of replying, so no channel gets an answer until this gate
        # opens.  Without a bound, one pathologically long resumed turn holds
        # the gate shut and every channel's inbound piles up unanswered for as
        # long as that turn runs.  On timeout the gate releases and the slow
        # resume turn keeps running in the background; duplicate-agent
        # protection is unaffected because the resume slot is claimed
        # synchronously before the gate runs.  Set to 0 to disable the bound
        # (historical "wait forever" behaviour).
        "gateway_startup_restore_drain_timeout": 30,
        # Stale-stream ceiling for local providers (Ollama, oMLX, llama-cpp) in
        # seconds. When the base stale timeout is at its default (180s) and a
        # local endpoint is detected, this finite ceiling replaces the former
        # infinite disable so a wedged local server eventually trips the
        # detector instead of hanging forever. The env var
        # ``SON_OF_ANTON_LOCAL_STREAM_STALE_TIMEOUT`` overrides for escape-hatch use.
        "local_stream_stale_timeout": 900,
        # How user-attached images are presented to the main model on each turn.
        #   "auto"   — attach natively when the active model reports
        #              supports_vision=True AND the user hasn't explicitly
        #              configured auxiliary.vision.provider.  Otherwise fall
        #              back to text (vision_analyze pre-analysis).
        #   "native" — always attach natively; non-vision models will either
        #              error at the provider or get a last-chance text fallback
        #              (see run_agent._prepare_messages_for_api).
        #   "text"   — always pre-analyze with vision_analyze and prepend the
        #              description as text; the main model never sees pixels.
        # Affects gateway platforms, the TUI, CLI /attach, and tool-result
        # images (e.g. read_file).  vision_analyze remains available as a tool
        # regardless of this setting — the routing only controls how images
        # are presented to the main model.
        "image_input_mode": "auto",
        "disabled_toolsets": [],

        # Per-model reasoning effort overrides (spelling-tolerant).
        # Dict mapping model names (any reasonable spelling) to effort levels.
        # Takes precedence over agent.reasoning_effort when the current model
        # matches a key in this dict.
        # Edit directly in config.toml (no CLI support due to dots in keys).
        "reasoning_overrides": {},

        # Assistant ``reasoning_content`` echo on replay is NOT an ``agent``
        # key. It resolves per route (agent.message_sanitization.
        # resolve_reasoning_echo), most specific first:
        #   custom_providers.<name>.models.<model>.reasoning_echo
        #   model.reasoning_echo / a fallback_providers entry's reasoning_echo
        #   default: on for local / private-network endpoints (loopback,
        #   RFC-1918, .local/.lan), off elsewhere — hosted APIs such as
        #   Mistral/Groq 400 on the field even when declared as
        #   custom_providers.
        # An explicit true also pads turns that have no reasoning (for a
        # gateway fronting Kimi-style models); the local default never pads.
        # DeepSeek / Kimi / MiMo are always echoed (and padded) by host/name.
    },

    # Physics mode (autophysicist): the single-agent Autophysicist loop.
    # It shares the main agent's model configuration; override the endpoint
    # here for a dedicated server.
    "physics": {
        "model": "",
        # Model for sub-agents that are WRITING CODE (execute_code
        # dispatches). Empty means `model` does everything.
        #
        # Roles that are not writing code keep `model` — the Manager, and a
        # sub-agent dispatched WITHOUT execute_code (derive this, find the
        # error in that, argue the other side). Those are doing physics
        # reasoning, and routing them to a coding model to save latency trades
        # away what they were dispatched for.
        #
        # Code is where the volume is — one call per script, plus up to three
        # more each time a script fails — so a faster, stronger coding model
        # is worth the most there and costs the least.
        "coder_model": "",
        # Per-agent-role model overrides. Beats coder_model and model, matched
        # by exact agent name then longest prefix.
        #
        # Keys are the agent's own name: manager, subagent, critic,
        # spec_writer. A "subagent" entry matches every "subagent_iter<N>_<M>"
        # dispatch by prefix.
        #
        #   agent_models:
        #     manager: qwen3.8-27b-coding           # thinking profile
        #     subagent: qwen3.8-27b-instruct        # no thinking, much faster
        #     critic: deepseek-v4-flash-local       # knowledge where it pays
        "agent_models": {},
        # Sent as `reasoning_effort` on every physics call. The vocabulary is
        # the endpoint's: Qwen3.8 on vLLM takes low / medium / xhigh, and
        # xhigh is its DEFAULT — which on a script-writing prompt spent 80,000
        # characters reasoning and produced no script at all inside a 24k
        # budget. Empty leaves the endpoint's default alone.
        #
        # Harmless for a model with thinking disabled: measured against
        # qwen3.8-27b-instruct at low, medium and xhigh, the reasoning channel
        # stays empty — chat_template_kwargs.enable_thinking=false wins — so
        # one global setting can cover a deployment running both profiles of
        # the same weights.
        "reasoning_effort": "",
        # Autophysicist only: review each iteration from outside before the
        # next one starts. 1 = every iteration, 0 = off.
        #
        # The Autophysicist is one agent that decides what to investigate,
        # judges its own sub-agents and decides what is true, and its own
        # prompt names that as the weak point — but "nothing is reliable until
        # verified" is a norm with nothing enforcing it. This is the cheapest
        # possible enforcement: one prompt, one answer, no tools, no verdict
        # that gates anything. The critique lands in the next iteration's
        # context; every one is kept in CRITIQUE_LOG.md.
        #
        # A single call per iteration is a rounding error against a Manager
        # that spends five rounds and several sub-agent dispatches, so this is
        # where a slower, more knowledgeable model earns its latency:
        #   agent_models: { critic: deepseek-v4-flash-local }
        "critique_every_n": 1,
        # Who writes the critique: "model" (default) or "human".
        #
        # Human mode is for attended runs. Each critique prints the iteration's
        # activity and `git diff`, opens $EDITOR for free-text feedback, then
        # asks for a structured verdict (progress | stalled | wrong_approach |
        # needs_help) and whether to block the next iteration. The text reaches
        # the Manager through exactly the same channel as a model critique, so
        # the Manager cannot tell the two apart. Every critique — model or
        # human — is also appended to CRITIQUE_DATA.jsonl with its source for
        # later analysis.
        "critic": {"mode": "model"},
        "base_url": "",
        "api_key_env": "",
        # Base directory for physics run workspaces. Each run gets its own
        # timestamped subdirectory, git-initialized in place. MUST be a
        # directory the agent owns: the run commits its whole contents.
        # Empty = ~/.son-of-anton/workspaces.
        "workspace_root": "",
        # Ceiling on the outer loop. 0 uses the mode's own default (50).
        # `problem run --max-iterations` overrides it.
        #
        # Worth setting. Physics mode has no wall-clock or cost gate, so this
        # is the only ceiling on an unattended run, and at ~10s a call with up
        # to fifteen tool calls an iteration the default is an afternoon.
        "max_iterations": 0,
        # Seconds one model-authored script may run for. 0 uses the built-in
        # default of 60, which came from a scaffold built for symbolic work.
        # Experimental data inverts the assumption: reading a few hundred
        # thousand waveforms out of a multi-GB file and training on them is
        # legitimate work that does not fit in a minute, and the agent reads a
        # timeout as "wrong approach" and retries something smaller.
        "script_timeout": 0,
        # Token SPEND cap for each Autophysicist iteration. 0 uses the
        # built-in 64000; `problem run --token-budget` overrides it.
        #
        # Charged as new input plus output tokens, the Manager's and its
        # sub-agents': a conversation's re-sent history (or a prefix the
        # endpoint reports as cached) is counted once, not every round. At the
        # budget the Manager loses dispatch_subagent and is told to save its
        # work and end the iteration; at 1.5x the iteration is cut off. It is
        # not a context-window size.
        "token_budget": 0,
        # The physics model's context window in tokens. 0 resolves it the way
        # the main agent does (custom_providers.<name>.models.<model>
        # .context_length, the endpoint's /models metadata, known families).
        # Each iteration's opening prompt is sized against it — permanent
        # memory gets a quarter, past which older entries are injected as a
        # digest (PERMANENT_MEMORY_DIGEST.md) — and an overflow error that
        # names the real window overrides it for the rest of the run.
        "context_length": 0,
        # Interpreter that model-authored computations run under. The agent's
        # own venv deliberately ships no scientific stack, so leaving this
        # empty means numpy/scipy/matplotlib are unavailable to physics runs.
        # Point it at a Python environment carrying the stack — see
        # nix/physics-runtime.nix. Empty falls back to
        # SON_OF_ANTON_PHYSICS_PYTHON, then SON_OF_ANTON_PYTHON, then the
        # agent's own interpreter.
        "python": "",
        # Confinement for those computations: "auto" (sandbox under bubblewrap
        # when it is installed, refuse to run when it is not), "bwrap"
        # (require it), or "off" (run unconfined — model-authored code then
        # has the agent's full filesystem access and environment).
        "sandbox": "auto",
        # Host directories exposed read-only inside the sandbox, for every
        # run. A problem spec's `data:` list adds to this per run.
        "data_dirs": [],
        # Leave the network reachable from computations. Off by default.
        "sandbox_net": False,
        # Extra text appended to the code-execution instructions both physics
        # agents see, for house libraries or local conventions the built-in
        # notes do not cover (autophysicist/utils/runtime_notes.py). The
        # built-in note for analysis_utilities appears on its own whenever that
        # package imports in the configured runtime.
        "runtime_notes": "",
        # Lookup tools for the Research Manager — literature and library docs.
        # `server` names an entry in the top-level `mcp_servers` so an endpoint
        # the main agent already declares is written once, not twice with its
        # own credential; `url`/`headers`/`api_key_env` are the standalone
        # alternative. `allow` is required and is what keeps this usable: an
        # aggregating gateway can expose dozens of tools, and handing all of
        # them to an agent with a fifteen-call budget crowds out the problem.
        # An entry matches a whole tool name ("arxiv-get_abstract") or a
        # server, which matches everything it prefixes ("context7"). Empty
        # `allow` means no MCP tools at all.
        "mcp": {
            "server": "",
            "url": "",
            "headers": {},
            "api_key_env": "",
            # Per-role allowlists. Different roles want different tools, and
            # handing every role everything is how an agent with a fifteen-call
            # budget ends up browsing instead of working: the Manager decides
            # strategy and wants the literature, while a sub-agent writing one
            # script wants the API signature it is about to get wrong and
            # nothing else.
            #
            # An entry matches a whole tool name ("arxiv-get_abstract") or a
            # server, matching everything it prefixes ("context7"). A role with
            # no entry gets no tools. Omit `roles` entirely for the defaults
            # below; set it to {} to turn MCP off.
            #
            #   roles:
            #     manager:  [arxiv, context7]
            #     subagent: [context7]
            "roles": None,
            "timeout": 120.0,
        },
        # Resource caps inside the sandbox. 0 disables a limit. cpu_seconds
        # counts CPU time summed over threads, so it is off by default —
        # the per-script wall-clock timeout is the limit that matches intent.
        "memory_limit_mb": 0,
        "file_size_limit_mb": 8192,
        "cpu_seconds": 0,
    },

    "terminal": {
        "backend": "local",
        "cwd": ".",  # Use current directory
        "timeout": 180,
        # Environment variables to pass through to sandboxed execution
        # (terminal and execute_code).
        "env_passthrough": [],
        # HOME handling for host tool subprocesses:
        #   auto    — host keeps the real OS-user HOME; containers use
        #             SON_OF_ANTON_HOME/home for persistent state (default)
        #   real    — force the real OS-user HOME
        #   profile — force SON_OF_ANTON_HOME/home when it exists (old strict
        #             per-profile CLI config isolation)
        #   cwd     — force the command's working directory as HOME (gateway
        #             profiles whose terminal.cwd is the user's home)
        "home_mode": "auto",
        # Extra files to source in the login shell when building the
        # per-session environment snapshot.  Use this when tools like nvm,
        # pyenv, asdf, or custom PATH entries are registered by files that
        # a bash login shell would skip — most commonly ``~/.bashrc``
        # (bash doesn't source bashrc in non-interactive login mode) or
        # zsh-specific files like ``~/.zshrc`` / ``~/.zprofile``.
        # Paths support ``~`` / ``${VAR}``. Missing files are silently
        # skipped. When empty, Son of Anton auto-sources ``~/.profile``,
        # ``~/.bash_profile``, and ``~/.bashrc`` (in that order) if the
        # snapshot shell is bash (this is the ``auto_source_bashrc``
        # behaviour — disable with that key if you want strict login-only
        # semantics).
        "shell_init_files": [],
        # When true (default), Son of Anton sources the user's shell rc files
        # (``~/.profile``, ``~/.bash_profile``, ``~/.bashrc``) in the
        # login shell used to build the environment snapshot. This
        # captures PATH additions, shell functions, and aliases — which a
        # plain ``bash -l -c`` would otherwise miss because bash skips
        # bashrc in non-interactive login mode, and because a default
        # Debian/Ubuntu ``~/.bashrc`` short-circuits on non-interactive
        # sources. ``~/.profile`` and ``~/.bash_profile`` are tried first
        # because ``n`` / ``nvm`` / ``asdf`` installers typically write
        # their PATH exports there without an interactivity guard. Turn
        # this off if your rc files misbehave when sourced
        # non-interactively (e.g. one that hard-exits on TTY checks).
        "auto_source_bashrc": True,
    },

    "git": {
        # AUTHOR identity for commits the agent makes on your behalf (/commit),
        # applied via `git commit --author=`. The committer is never touched —
        # it stays whatever the repository or your global git config sets, so
        # the log reads "authored by the account, committed by you". Empty by
        # default: no override at all, nothing attributed to a name you did not
        # choose. Set both to author under a dedicated account (a bot user, say).
        "author_name": "",
        "author_email": "",
    },

    # Hard cap (chars) for a single automatic context file such as SOUL.md,
    # AGENTS.md, CLAUDE.md, .son-of-anton.md, or .cursorrules before Son of Anton applies
    # head/tail truncation. ``null`` (the default) lets the cap scale with the
    # model's context window (floor 20K, ceiling 500K) so large-context models
    # rarely truncate a project doc. Set a positive integer to pin a fixed cap
    # and override the dynamic behavior. Separate from read_file tool limits.
    "context_file_max_chars": None,

    # Maximum characters returned by a single read_file call.  Reads that
    # exceed this are rejected with guidance to use offset+limit.
    # 100K chars ≈ 25–35K tokens across typical tokenisers.
    "file_read_max_chars": 100_000,

    # Seconds to wait at agent-build time for in-flight MCP server discovery
    # to finish before the agent snapshots its tool list.  MCP discovery runs
    # in a background thread so a slow/dead server can't freeze startup; this
    # bounds how long the first agent build blocks on it.  The wait returns
    # the INSTANT discovery completes, so users with no MCP servers (the common
    # case) or fast servers pay ~0s regardless of this value — the bound is
    # only reached when a server is genuinely still connecting.  The old 0.75s
    # default was a touch short for HTTP/OAuth servers on a cold connect; a
    # modest bump lets more of them land in the FIRST turn's snapshot.  This is
    # only a turn-1 latency/UX knob: a server that misses this window is still
    # picked up automatically by the between-turns refresh during the
    # conversation's opening turns (``mcp.refresh_turns``; see
    # agent/turn_context.py).  Keep it small so a slow/dead server adds little
    # to first-response latency.
    "mcp_discovery_timeout": 1.5,

    # Single-query (``son-of-anton -q/-z "..."``) variant of mcp_discovery_timeout.
    # In one-shot mode there is only ONE turn, so the between-turns late-binding
    # refresh never runs: a server that misses the small interactive bound is
    # invisible to the LLM for the whole session.  This larger bound gives slow
    # cold-start servers (npx, uvx, remote HTTP) a chance to land in the one
    # tool snapshot.  ``thread.join(timeout)`` returns the instant discovery
    # completes, so reachable servers only wait for their real handshake time
    # while unavailable servers remain bounded.
    "mcp_single_query_discovery_timeout": 15.0,

    # MCP runtime behavior (distinct from the per-server definitions in
    # mcp_servers: and from the auxiliary.mcp side-LLM task settings).
    "mcp": {
        # Auto-reload MCP connections when config.toml's mcp_servers section
        # changes at runtime (CLI file watcher, default on).
        # Set to false to stop the automatic reload: every automatic reload
        # rebuilds the agent tool surface and INVALIDATES the provider
        # prompt cache (the next message re-sends the full input prefix),
        # which is expensive on long-context / high-reasoning models.
        # When disabled, the watcher still detects the change and prints
        # guidance to apply it deliberately via /reload-mcp.
        "auto_reload_on_config_change": True,
        # How many opening turns of a conversation may automatically ADD tools
        # from an MCP server that finished connecting late (missed the startup
        # wait). Any change to the tool list re-prefills the whole cached
        # conversation, so after this window the tool list is frozen: a server
        # that parks keeps its tools in the schema (calls report it
        # unavailable) and newly appearing tools wait for /reload-mcp or the
        # next session. A resumed session only refreshes while it has no MCP
        # tools at all. 0 disables the automatic refresh entirely.
        "refresh_turns": 3,
    },

    # Tool-output truncation thresholds. When terminal output or a
    # single read_file page exceeds these limits, Son of Anton truncates the
    # payload sent to the model (keeping head + tail for terminal,
    # enforcing pagination for read_file). Tuning these trades context
    # footprint against how much raw output the model can see in one
    # shot. Ported from anomalyco/opencode PR #23770.
    #
    # - max_bytes:       terminal_tool output cap, in chars
    #                    (default 50_000 ≈ 12-15K tokens).
    # - max_lines:       read_file pagination cap — the maximum `limit`
    #                    a single read_file call can request before
    #                    being clamped (default 2000).
    # - max_line_length: per-line cap applied when read_file emits a
    #                    line-numbered view (default 2000 chars).
    "tool_output": {
        "max_bytes": 50_000,
        "max_lines": 2000,
        "max_line_length": 2000,
    },

    # Tool loop guardrails nudge models when they repeat failed or
    # non-progressing tool calls. Soft warnings are always-on by default;
    # hard stops are opt-in so interactive CLI/TUI sessions keep flowing.
    "tool_loop_guardrails": {
        "warnings_enabled": True,
        "hard_stop_enabled": False,
        "warn_after": {
            "exact_failure": 2,
            "same_tool_failure": 3,
            "idempotent_no_progress": 2,
        },
        "hard_stop_after": {
            "exact_failure": 5,
            "same_tool_failure": 8,
            "idempotent_no_progress": 5,
        },
        # Per-turn runaway-loop caps (inspired by Claude Code v2.1.212,
        # Week 29, July 2026). Hard ceilings on how many times a runaway-prone
        # tool may be called within a SINGLE agent loop (turn); the counters
        # reset at the start of every turn, so a legitimate multi-turn session
        # is never starved. They are always-on and fire regardless of the
        # warn/hard-stop thresholds above. A single turn issuing dozens of web
        # searches or spawning dozens of subagents is already pathological, so
        # the defaults are low. Set either to 0 to disable that cap (unlimited).
        "loop_caps": {
            "max_web_searches": 50,   # max web_search calls per turn (0 = unlimited)
            "max_subagents": 50,      # max subagents spawned per turn (0 = unlimited)
        },
    },

    "compaction": {
        "enabled": True,
        "threshold": 0.50,           # compact when context usage exceeds this ratio.
                                      # Models with context windows below 512K are
                                      # floored at 0.75 (raise-only) so compaction
                                      # doesn't fire with half the window still free;
                                      # set this above 0.75 to override the floor.
        "threshold_tokens": None,     # absolute token cap — when set, compaction
                                      # triggers at the lower of the ratio-based
                                      # threshold and this token count. Clamped to
                                      # the model's context length at apply-time.
        "target_ratio": 0.20,         # fraction of threshold to preserve as recent tail
        "tail_mode": "legacy",        # tail retention policy (#87326):
                                      #   "legacy" — 0.20×window verbatim tail (default)
                                      #   "lean"   — clamped 2.5%-of-window tail
                                      #              (10K floor / 25K cap) plus chunked
                                      #              digests, a mechanical anchor index,
                                      #              verbatim user messages, and
                                      #              session_search recovery pointers in
                                      #              the summary. ~3x fewer retained
                                      #              tokens after compaction; costs a few
                                      #              extra summarizer calls at the
                                      #              compaction boundary.
        "protect_last_n": 20,         # minimum recent messages to keep uncompacted
        "min_tail_user_messages": 1,  # REAL (actionable) user messages guaranteed to
                                      # survive in the uncompacted tail. 1 = existing
                                      # single last-user anchor (default, behavior-
                                      # preserving); raise to e.g. 3 to keep the last
                                      # 3 real user turns verbatim when bulky tool
                                      # outputs fill the tail token budget.
        "max_attempts": 3,            # compaction retry rounds before a turn gives up
                                      # with "max compaction attempts reached". Raise
                                      # (e.g. 6) for tool-schema-heavy sessions where 3
                                      # rounds cannot clear the request estimate.
                                      # Validated >= 1, hard-capped at 10.
        "proactive_prune_tokens": 40000,  # trigger (tokens) for the deterministic,
                                      # no-LLM tool-result prune, run independently of
                                      # `threshold` above. Old tool output is the bulk
                                      # of a long session and the least useful per
                                      # token; once the context passes this size, tool
                                      # results outside the recent tail (protect_last_n)
                                      # are folded to one-line summaries — no model
                                      # call, nothing to hallucinate, and the LLM
                                      # summarizer later sees a much smaller input.
                                      # Defaults mirror opencode's prune (protect 40K,
                                      # reclaim >= 20K). 0 = off. Built-in compactor
                                      # only (other engines inherit a no-op).
                                      # NOTE: each committed prune rewrites already-sent
                                      # history, breaking the provider prompt-cache
                                      # prefix — the min_reclaim gate below keeps those
                                      # breaks episodic rather than per-turn.
        "proactive_prune_min_result_chars": 2000,  # the prune's summarize pass only
                                      # touches tool results larger than this (chars);
                                      # clamped to >= 200 so a generated summary can't
                                      # itself be re-summarized.
        "proactive_prune_min_reclaim_tokens": 20000,  # a proactive prune only commits
                                      # when it reclaims at least this many tokens
                                      # (measured on the pruned output), then waits
                                      # for a full trigger-sized token runway to
                                      # regrow before rearming. Keeps prompt-cache
                                      # breaks episodic. 0 = no minimum-savings gate.
        "micro_compact": False,       # opt-in: after each completed turn, fold the
                                      # oldest un-absorbed exchange into a rolling
                                      # summary, amortizing compaction cost instead
                                      # of paying it in one batch stall. Default False
                                      # because a pass rewrites already-sent history
                                      # and so breaks the provider prompt-cache prefix
                                      # EVERY turn — the per-turn cache break that
                                      # `proactive_prune_min_reclaim_tokens` above
                                      # exists to avoid. Enable only when you have
                                      # measured that the amortized stall is worth
                                      # more to you than the cached-prefix discount.
                                      # See docs/micro-compaction.md.
        "micro_compact_every_n_turns": 1,  # cadence: run a pass every Nth completed
                                      # turn. Since each pass costs one prompt-cache
                                      # break, this is the dial for how often that
                                      # cost is paid — 1 reclaims most aggressively
                                      # at one break per turn, 5 trades reclaim rate
                                      # for a fifth of the breaks. Clamped to >= 1.
                                      # Ignored unless `micro_compact` is true.
        "micro_compact_defrag_threshold_tokens": 2000,  # once the rolling summary
                                      # exceeds this many tokens, the next pass
                                      # re-summarizes the summary itself instead of
                                      # letting it grow without bound.
        "hygiene_hard_message_limit": 5000,  # gateway session-hygiene force-compact threshold by message count
        "hygiene_timeout_seconds": 30,  # max seconds gateway waits for pre-agent hygiene compaction
                                      # WITHOUT forward progress. The summary call streams, so
                                      # this is an inactivity budget: a slow model still
                                      # producing tokens keeps extending the wait; only a
                                      # silent/hung call is cut off.
        "hygiene_total_ceiling_seconds": 600,  # absolute cap on the hygiene compaction wait even
                                      # while tokens are still moving — bounds a degenerate
                                      # trickle stream. Clamped to >= hygiene_timeout_seconds.
        "hygiene_failure_cooldown_seconds": 300,  # skip repeated failed hygiene attempts for this session
        "context_timeout_seconds": 120,  # inactivity budget for in-agent compact_context
                                      # (conversation loop, /compact, preflight, etc.).
                                      # Same progress-aware semantics as hygiene_timeout_seconds:
                                      # streamed summary tokens extend the wait; only a silent
                                      # worker is cut off. 0 = disable the owned wrapper
                                      # (callers that already pass commit_fence, e.g. gateway
                                      # hygiene, never use this path).
        "context_total_ceiling_seconds": 600,  # absolute cap on the *pre-commit*
                                      # in-agent compact_context wait (summary /
                                      # stream phase) even while tokens are still
                                      # moving. Clamped to >= context_timeout_seconds
                                      # when the idle budget is > 0. Guarantee:
                                      # the summary phase is bounded by this
                                      # ceiling; an already-started SessionDB
                                      # commit is never abandoned mid-flight —
                                      # if the commit itself runs past the
                                      # ceiling it is logged (WARNING, then
                                      # ERROR) and surfaced to the user via the
                                      # warning channel while the host keeps
                                      # waiting in bounded increments for the
                                      # commit to finish.
        "protect_first_n": 3,         # non-system head messages always preserved
                                      # verbatim, in ADDITION to the system prompt
                                      # (which is always implicitly protected). Set to
                                      # 0 for long-running rolling-compaction sessions
                                      # where you want nothing pinned except the
                                      # system prompt + rolling summary + recent tail.
        "abort_on_summary_failure": False,  # When True, auto-compaction that fails
                                      # to generate a summary (aux LLM errored / returned
                                      # non-JSON / timed out) aborts entirely instead of
                                      # dropping the middle window with a static
                                      # "summary unavailable" placeholder.  Messages are
                                      # preserved unchanged and the session "freezes" at
                                      # its current size until the user runs /compact
                                      # (which bypasses the failure cooldown) or /new.
                                      # Default False matches historical behavior; set to
                                      # True if you'd rather pause than silently lose
                                      # context turns when your aux model is flaky.
        "in_place": True,             # When True, compaction rewrites the message
                                      # list and rebuilds the system prompt WITHOUT
                                      # rotating the session id — the conversation
                                      # keeps one durable id for its whole life
                                      # (no parent_session_id chain, no `name #N`
                                      # renumbering). Eliminates the session-rotation
                                      # bug cluster (#33618 /goal loss, #14238 lost
                                      # response, #33907 orphans, #45117 search gaps,
                                      # #42228 null cwd) — see #38763. Non-destructive:
                                      # the live context is compacted (lossy for what
                                      # the model reloads), but the pre-compaction
                                      # turns are soft-archived under the same id
                                      # (active=0, compacted=1) — still searchable via
                                      # session_search and recoverable, not deleted.
                                      # Default True since 2107b86024; set False to
                                      # restore the legacy rotating-compaction path.
        "model_thresholds": {},       # Per-model threshold overrides. Keys are
                                      # substring-matched against the model name
                                      # (longest match wins); values replace the
                                      # global `threshold` for that model, e.g.
                                      #   model_thresholds:
                                      #     "glm-5.2": 0.40
                                      #     "claude-sonnet": 0.35
                                      # The small-context floor (0.75 for <512K
                                      # models) still applies on top of overrides
                                      # (raise-only: an override above the floor
                                      # wins; one below it is raised to the floor).
        "idle_compact_after_seconds": 0,  # Opt-in idle compaction (0 = disabled).
                                      # When > 0, a session that resumes after at
                                      # least this many seconds of inactivity
                                      # compacts its accumulated history up front,
                                      # before the first reply — so a long-lived
                                      # thread resumed hours later doesn't re-read
                                      # its full stale context on every turn.
                                      # Time-based; complements (does not replace)
                                      # the size-based `threshold` above. Skipped
                                      # when the context is already at/below the
                                      # post-compaction target (threshold ×
                                      # target_ratio) and it honors the same
                                      # failure-cooldown / anti-thrash / per-session
                                      # lock guards as every automatic compaction.
                                      # Example: 1800 = compact after 30 min idle.
    },


    # Auxiliary model config — provider:model for each side task.
    # Format: provider is the provider name, model is the model slug.
    # "auto" for provider = auto-detect best available provider.
    # Empty model = use provider's default auxiliary model.
    # Side tasks fall back to a lightweight model when the configured
    # provider is unavailable.
    #
    # extra_body: forwarded verbatim as request body fields on every aux call
    # for that task. Use this to set provider-specific knobs (independent of
    # main-agent settings).
    #
    #   auxiliary:
    #     compaction:
    #       provider: deepseek
    #       model: deepseek-v4
    #
    # Each aux task is independent — main-agent provider routing does NOT
    # propagate to aux calls by design.
    "auxiliary": {
        "vision": {
            "provider": "auto",    # auto | deepseek | openai-api | custom
            "model": "",           # e.g. "google/gemini-2.5-flash", "gpt-4o"
            "base_url": "",        # direct OpenAI-compatible endpoint (takes precedence over provider)
            "api_key": "",         # API key for base_url (falls back to OPENAI_API_KEY)
            "timeout": 120,        # seconds — LLM API call timeout; vision payloads need generous timeout
            "extra_body": {},      # OpenAI-compatible provider-specific request fields
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "compaction": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 120,        # seconds — compaction summarises large contexts; increase for local models
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Note: session_search no longer uses an auxiliary LLM (PR #27590 —
        # single-shape tool returns DB content directly). The old
        # ``auxiliary.session_search.*`` block was removed here. Existing
        # values in user config.toml files are harmless leftovers and ignored.
        "approval": {
            "provider": "auto",
            "model": "",           # fast/cheap model recommended (e.g. gemini-flash, haiku)
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "mcp": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "title_generation": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Goal judge — evaluates whether a /goal run's latest response
        # satisfies the goal/contract, and drafts goal contracts. Short
        # structured-JSON calls; a fast cheap model is fine.
        "goal_judge": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 60,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
    },
    
    "display": {
        "compact": False,
        "personality": "",
        "resume_display": "full",
        # Recap tuning for /resume and startup resume.
        # resume_exchanges caps how many user+assistant pairs are painted when
        # a session is resumed; 0 means "no cap" — paint the whole transcript,
        # which is the default so a resumed session reads as the conversation
        # it actually is. Set a positive number to go back to a short recap.
        "resume_exchanges": 0,
        "resume_max_user_chars": 300,      # truncate user message text
        "resume_max_assistant_chars": 200, # truncate non-last assistant text
        "resume_max_assistant_lines": 3,   # truncate non-last assistant lines
        # When True (default), assistant entries that are *only* tool calls
        # (no visible text) are skipped in the recap. This prevents the recap
        # from being dominated by `[2 tool calls: terminal, read_file]` lines
        # when an exchange was tool-heavy. Set False to restore the legacy
        # behavior of showing tool-call summaries inline.
        "resume_skip_tool_only": True,
        # Classic CLI multiline fallbacks beyond Alt+Enter.
        # Default true matches Claude Code / Codex / OpenCode: Ctrl+J inserts
        # a newline, a trailing backslash followed by Enter continues the draft,
        # and supported terminals are asked to report Shift+Enter distinctly.
        # When true, `son-of-anton --tui` auto-resumes the most recent human-
        # facing session on launch instead of forging a fresh one.
        # Mirrors `son-of-anton -c` muscle memory.  Default off so existing
        # users aren't surprised.  SON_OF_ANTON_TUI_RESUME=<id> always wins.
        "tui_auto_resume_recent": False,
        # When true (default), `son-of-anton --tui` drops a one-time hint
        # ("subagents working · /agents to watch live") the first time a turn
        # starts delegating, nudging the user toward the live spawn-tree
        # viewer. Set false to suppress the hint.
        "tui_agents_nudge": True,
        "bell_on_complete": False,
        # Stream the model's reasoning/thinking live before the response.
        # Default ON: on thinking models the reasoning phase can run tens of
        # seconds, and with this off the user stares at a spinner the whole
        # time even though tokens are streaming. Set false for quiet output.
        "show_reasoning": True,
        # When reasoning display is on, the post-response "Reasoning" recap box
        # collapses long thinking to the first 10 lines. Set true to print the
        # complete thinking text uncollapsed (live streaming is always full).
        "reasoning_full": False,
        # Gateway notifications when a terminal(background=true) process
        # finishes:
        #   "concise" — one-line status message; failures append a short
        #               output tail (default)
        #   "all"     — running-output updates + final raw-output message
        #   "result"  — final raw-output message only
        #   "error"   — final raw-output message only on non-zero exit
        #   "off"     — no watcher messages at all
        "background_process_notifications": "concise",
        "streaming": False,
        "timestamps": False,      # Show message timestamps (CLI labels, TUI rows, desktop transcript)
        "timestamp_format": "%H:%M",  # strftime format for timestamps (e.g. "%b-%d %H:%M")
        "final_response_markdown": "strip",  # render | strip | raw
        # Preserve recent classic CLI output across Ctrl+L, /redraw, and
        # terminal resize full-screen clears. Disable if a terminal emulator
        # behaves badly with replayed scrollback.
        "persistent_output": True,
        "persistent_output_max_lines": 200,
        # Clear terminal scrollback as well as the visible viewport when the
        # classic CLI performs a full redraw/resize recovery. Disabled by
        # default because some users prefer preserving terminal history;
        # enable when a terminal/tmux stack stamps stale prompt chrome into
        # scrollback during fullscreen/restore window transitions.
        "cli_rebuild_scrollback_on_redraw": False,
        # Print a one-line summary of resolved modal prompts (approval /
        # clarify) into scrollback so the question and decision survive the
        # panel repaint. Set false to keep scrollback untouched.
        "persist_prompts": True,
        "inline_diffs": True,     # Show inline diff previews for write actions (write_file, patch)
        # File-mutation verifier footer.  When true (default), the agent
        # appends a one-line advisory to its final response whenever a
        # write_file / patch call failed during the turn and was never
        # superseded by a successful write to the same path.  This catches
        # the "batch of parallel patches, half fail, model claims success"
        # class of over-claim that otherwise forces users to run
        # `git status` to verify edits landed.  Set false to suppress.
        "file_mutation_verifier": True,
        # Credits status-bar notices (usage bands, depleted /
        # restored).  When false, no credits notices are emitted — balance data
        # is still captured and /usage keeps working.  Off switch for sub +
        # top-up users who find the gauge noisy.
        "credits_notices": True,
        # Turn-completion explainer.  When true (default), the agent appends a
        # one-line explanation to its final response whenever a turn ends
        # abnormally with no usable reply — empty content after retries, a
        # partial/truncated stream, a still-pending tool result, or an
        # iteration/budget limit.  Replaces the bare "(empty)" sentinel so the
        # failure isn't silent from the UI's perspective.  Set false to suppress.
        "turn_completion_explainer": True,
        "show_cost": False,       # Show $ cost in the status bar (off by default)
        # Focus view (/focus): display-only reduced-output mode. When true the
        # CLI/TUI pins tool_progress to "off" (reusing the existing suppression
        # path), reports a per-turn hidden-line count with a recovery hint, and
        # pins a "focus" segment in the status bar. focus_saved_tool_progress
        # holds the mode /focus off restores. Never affects what is sent to the
        # model — see son_of_anton_cli/focus_view.py.
        "focus_view": False,
        "focus_saved_tool_progress": "all",
        "skin": "default",
        # TUI busy indicator style: kaomoji (default), emoji, unicode (braille
        # spinner), or ascii.  Live-swappable via `/indicator <style>`.
        "tui_status_indicator": "kaomoji",
        # Seconds between prompt_toolkit redraws in the classic CLI when idle.
        # Default 1.0 keeps the wall-clock status-bar read-outs (idle-since-
        # last-turn) ticking and keeps the bottom chrome alive during idle —
        # without it prompt_toolkit stops repainting the status bar after a
        # turn and it can go stale/disappear (#45592).
        # Set 0 to disable the background refresh if it fights terminal
        # auto-scroll in non-fullscreen mode on some emulators (#48309).
        "cli_refresh_interval": 1.0,
        "user_message_preview": {  # CLI: how many submitted user-message lines to echo back in scrollback
            "first_lines": 2,
            "last_lines": 2,
        },
        "interim_assistant_messages": True,  # Gateway: send natural mid-turn assistant status messages. Desktop: keep mid-turn narration between tool calls instead of collapsing to the final message.
        "tool_progress_command": False,  # Enable /verbose command in messaging gateway
        "tool_preview_length": 0,  # Max chars for tool call previews (0 = no limit, show full paths/commands)
        # Human-phrased tool status labels for built-in tools: "Searching the
        # web for ...", "Reading <file>", "Browsing <url>" instead of the raw
        # tool name. Applies to CLI spinner + gateway/desktop tool-progress.
        # Custom/plugin/MCP tools always fall back to the raw preview.
        "friendly_tool_labels": True,
        # CLI-only post-turn accounting line printed after each interactive turn:
        # "⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands".
        # Observed from the tool-progress feed the CLI already receives; never
        # printed in quiet/non-interactive paths or in gateway/messaging
        # surfaces (those have their own runtime footer).
        "turn_summary": True,
        # CLI-only: append cumulative turn output tokens to the live spinner
        # timer ("⚡ Reading file  ( 2.3s · ↓ 1.2k tok)"). Updates as each API
        # call in the turn reports usage.
        "spinner_token_flow": True,
        # Per-platform display overrides. Each key is a gateway platform
        # ("signal", …) mapping to a dict of display settings that override
        # the global value for that platform only. Currently read:
        # show_reasoning (written by the gateway's /reasoning show|hide).
        "platforms": {},
        "copy_shortcut": "auto",  # "auto" (platform default) | "ctrl_c" | "ctrl_shift_c" | "disabled"
    },

    "privacy": {
        "redact_pii": False,  # When True, hash user IDs and strip phone numbers from LLM context
    },

    "human_delay": {
        "mode": "off",
        "min_ms": 800,
        "max_ms": 2500,
    },
    
    "context": {
        # Return freed glibc allocator pages after long-running agent/TUI
        # cleanup boundaries. Unsupported platforms are safe no-ops.
        "memory_trim": {
            "enabled": True,
            "cooldown_seconds": 60.0,
            # Successful trim calls are INFO logged every Nth periodic call;
            # force paths always log so process-close behavior is visible.
            "log_every_n": 1,
            # Suppress INFO logs only when a readable RSS change is smaller.
            # 0 reports every successful configured trim.
            "info_log_min_delta_mb": 0.0,
        },
    },

    # Persistent memory -- bounded curated memory injected into system prompt
    "memory": {
        "memory_enabled": True,
        "user_profile_enabled": True,
        # Built-in memory scope override. Empty = auto by session platform:
        # "cli" -> cli, signal -> gateway, everything else ->
        # shared. Pin it when a non-standard front-end should see a scope
        # (e.g. a cron worker that needs the workstation notes).
        "scope": "",
        # Approval gate for memory writes (add/replace/remove), applied to BOTH
        # foreground agent turns and the background self-improvement review fork
        # (the source of unprompted "wrong assumption" saves users reported).
        #   false (default) — write freely; the gate is off (pre-gate behaviour)
        #   true            — require approval: foreground writes prompt inline
        #                     (entries are small enough to review in a chat
        #                     bubble); background-review writes are staged
        #                     instead of committed (a daemon thread cannot block
        #                     on a prompt). Review staged entries with
        #                     /memory pending, /memory approve <id>,
        #                     /memory reject <id>.
        # To disable memory entirely, use memory_enabled: false instead.
        "write_approval": False,
        "memory_char_limit": 2200,   # ~800 tokens at 2.75 chars/token
        "user_char_limit": 1375,     # ~500 tokens at 2.75 chars/token
        # Retrieval-augmented recall (optional). Indexes session journals and
        # configured note files with an OpenAI-compatible embeddings endpoint
        # and injects the closest chunks into each turn's API copy. The system
        # prompt and the stored transcript stay untouched, so the prompt cache
        # is preserved. The index refreshes itself (gateway housekeeping loop,
        # and interactive CLI start) every reindex_interval_minutes;
        # `son-of-anton rag index [--rebuild]` forces a sync.
        "rag": {
            "enabled": False,
            # e.g. http://10.0.0.6:4002/v1 (Bifrost) or any OpenAI-compatible
            # /v1/embeddings endpoint.
            "base_url": "",
            "api_key": "",
            "api_key_env": "BIFROST_SOA_VK",
            "model": "bge-m3",
            "top_k": 5,
            # Cosine floor. bge-m3 puts unrelated passages around 0.3-0.45
            # and topical matches from ~0.55 up; 0.5 keeps an off-topic turn
            # from carrying five irrelevant chunks.
            "min_score": 0.5,
            # Per-turn query embedding runs before the first model call, so
            # it gets a short timeout; after a failure, retrieval is skipped
            # for failure_cooldown_seconds (one WARNING per window).
            "search_timeout_seconds": 3.0,
            "failure_cooldown_seconds": 60,
            # Automatic incremental refresh; 0 disables it.
            "reindex_interval_minutes": 15,
            # -z one-shot runs record source="oneshot" and are disposable:
            # by default their journals are not indexed and one-shot turns
            # retrieve nothing. Flip either on to opt in.
            "index_oneshot": False,
            "oneshot_retrieval": False,
            # Throttling: a single refresh embeds at most this many new
            # chunks (0 = no cap); the rest carry over to the next refresh.
            "max_chunks_per_refresh": 200,
            # Cap the text embedded per chunk, in characters (0 = the
            # chunker's own 1200-char limit only).
            "max_chunk_chars": 0,
            # Extra note files outside the notes tree, each
            # {path: ..., scope: shared|cli|gateway}.
            "sources": [],
            # Automatically indexed notes tree: notes/<scope>/**/*.md,
            # scope = the first directory name. Empty = $SON_OF_ANTON_HOME/notes.
            "notes_dir": "",
            # Default: $SON_OF_ANTON_HOME/rag
            "index_dir": "",
        },
    },

    # Subagent delegation — override the provider:model used by delegate_task
    # so child agents can run on a different (cheaper/faster) provider and model.
    # Uses the same runtime provider resolution as CLI/gateway startup, so all
    # configured providers (deepseek, openai-api, custom) are supported.
    "delegation": {
        "model": "",       # e.g. "unholy/dsv4-flash" (empty = inherit parent model)
        "max_concurrent_children": 10,  # unified concurrency cap: max parallel children per batch
                                       # AND max concurrent background (background=true)
                                       # delegation units. New async dispatches beyond the cap
                                       # fall back to synchronous execution. Floor of 1, no ceiling.
    },

    # Ephemeral prefill messages file — JSON list of {role, content} dicts
    # injected at the start of every API call for few-shot priming.
    # Never saved to sessions, logs, or trajectories.
    "prefill_messages_file": "",

    # Goals — persistent cross-turn goals (Ralph-style loop).
    # After every turn, a lightweight judge call asks the auxiliary model
    # whether the active /goal is satisfied by the assistant's last
    # response. If not, Son of Anton feeds a continuation prompt back into the
    # same session and keeps working until the goal is done, the turn
    # budget is exhausted, or the user pauses/clears it. Judge failures
    # fail OPEN (continue) so a flaky judge never wedges progress — the
    # turn budget is the real backstop.
    "goals": {
        # Max continuation turns before Son of Anton auto-pauses the goal and
        # asks the user to /goal resume. Protects against judge false
        # negatives (goal actually done but judge says continue) and
        # unbounded model spend on fuzzy / unachievable goals.
        "max_turns": 20,
    },


    # Loops — /loop recurring in-session wakeups (Claude Code parity).
    # A loop re-runs a prompt (or slash command) on a cadence inside the
    # live session. Fixed-interval mode fires on the user's clock;
    # self-paced mode (no interval given) starts at the floor and backs
    # off exponentially while the agent's replies stop changing.
    "loops": {
        # Smallest fixed interval accepted (seconds). Tighter cadences are
        # raised to this floor — each tick is a full agent turn.
        "min_interval_seconds": 30,
        # Backstop tick budget: the loop auto-pauses after this many
        # wakeups unless the user set --times. 0 = unlimited.
        "max_ticks": 100,
        # Self-paced cadence bounds (seconds).
        "self_paced_floor_seconds": 60,
        "self_paced_ceiling_seconds": 900,
    },

    # Honcho AI-native memory -- reads ~/.honcho/config.json as single source of truth.
    # This section is only needed for son-of-anton-specific overrides; everything else
    # (apiKey, workspace, peerName, sessions, enabled) comes from the global config.
    "honcho": {},

    # IANA timezone (e.g. "Asia/Kolkata", "America/New_York").
    # Empty string means use server-local time.
    "timezone": "",

    # Approval mode for dangerous commands:
    #   manual — always prompt the user
    #   smart  — use auxiliary LLM to auto-approve low-risk commands (default)
    #   off    — skip all approval prompts (equivalent to --yolo)
    #
    # cron_mode — what to do when a cron job hits a dangerous command:
    #   deny    — block the command and let the agent find another way (default, safe)
    #   approve — auto-approve all dangerous commands in cron jobs
    #
    # single_query_mode — what to do when a single-query (-q) session hits a
    # dangerous command. -q runs export SON_OF_ANTON_INTERACTIVE=1 (for interactive
    # sudo prompts) but have NO user waiting to answer approval prompts — an
    # unanswered prompt just waits the full timeout then fails closed, so the
    # agent is forced to work around the block (often via execute_code). This
    # setting makes that intent explicit:
    #   deny    — block the command and let the agent find another way (default,
    #             safe; mirrors cron_mode deny)
    #   approve — auto-approve all dangerous commands in single-query mode
    #
    # timeout — seconds to wait for the user's approve/deny. 0 (the default)
    # waits however long it takes: a timed-out approval failed closed for the
    # command, but the agent then carried on and tried another way to the
    # same outcome. A positive value opts into a deadline; on expiry the
    # command is denied and the agent is told to stop and ask. Shared by the
    # CLI prompt and gateway/messaging waits.
    "approvals": {
        "mode": "smart",
        "timeout": 0,
        "cron_mode": "deny",
        "single_query_mode": "deny",
        # Consecutive-denial circuit breaker for smart approvals: after this
        # many guardian DENY verdicts in a row within one session, the deny
        # message returned to the model escalates to a hard-stop instruction
        # (report to the user / ask for manual run or /approve) instead of a
        # plain "Do NOT retry". Any approval resets the count. 0 disables.
        # Inspired by ChatGPT Work's auto-review circuit breaker.
        "denial_breaker_threshold": 3,
        # When true, /reload-mcp asks the user to confirm before rebuilding
        # the MCP tool set for the active session.  Reloading invalidates
        # the provider prompt cache (tool schemas are baked into the system
        # prompt), so the next message re-sends full input tokens — this can
        # be expensive on long-context or high-reasoning models.  Users click
        # "Always Approve" to silence the prompt permanently; that flips
        # this key to false.
        "mcp_reload_confirm": True,
        # When true, destructive session slash commands (/clear, /new, /reset,
        # /undo) ask the user to confirm before discarding conversation state.
        # Three-option prompt (Approve Once / Always Approve / Cancel) routed
        # through tools.slash_confirm — a text prompt on messaging platforms.
        # Users click "Always
        # Approve" to silence the prompt permanently; that flips this key to
        # false.  TUI also honors this setting for its /clear, /new, and /reset
        # modal; SON_OF_ANTON_TUI_NO_CONFIRM=1 force-skips that modal regardless of
        # the configured value.
        "destructive_slash_confirm": True,
    },

    # Permanently allowed dangerous command patterns (added via "always" approval)
    "command_allowlist": [],
    # User-defined quick commands that bypass the agent loop (type: exec only)
    "quick_commands": {},

    # Per-platform system-prompt hint overrides. Lets an admin append to or
    # replace Son of Anton' built-in platform hint for a single messaging platform
    # (Signal, ...) without affecting other platforms.
    # Each key is a platform name; the value is either:
    #   { "append": "extra text" }   — keep the default hint, append text
    #   { "replace": "full text" }   — substitute the default hint entirely
    #   "extra text"                 — shorthand for { "append": ... }
    # `replace` wins over `append` if both are given. Example:
    #   platform_hints:
    #     signal:
    #       append: >
    #         When tabular output would be useful, emit a fenced code block
    #         instead of a Markdown table.
    "platform_hints": {},

    # Custom personalities — add your own entries here
    # Supports string format: {"name": "system prompt"}
    # Or dict format: {"name": {"description": "...", "system_prompt": "...", "tone": "...", "style": "..."}}
    "personalities": {},

    # Pre-exec security scanning via tirith
    "security": {
        "redact_secrets": True,
        # Lockdown permission mode (temple-style): every terminal command and
        # every file write outside the session directory requires explicit
        # human approval, regardless of approvals.mode. Toggle with /perm.
        "lockdown": False,
        # Persisted acknowledgement for unattended model overrides whose tier
        # lets the vendor train on prompts/completions. The startup guard still
        # prints the full warning on every run and never bypasses cost guards.
        "allow_data_training_tiers_noninteractive": False,
        # Writes to agent-instruction files (AGENTS.md/CLAUDE.md/SOUL.md/
        # .cursorrules, project-local .son-of-anton config) always require human
        # approval — even under auto-approve/yolo. Extra patterns are
        # fnmatch globs matched against the basename (e.g. "*.mdc").
        "protected_instruction_files": True,
        "protected_instruction_extra_patterns": [],
        # Path prefixes exempt from the sensitive-system-path write guard.
        # Each entry is an absolute path prefix; when the resolved or
        # normalized target of a write_file/patch call starts with any of
        # these prefixes, the blanket /etc/, /boot/, etc. refuse is skipped
        # for that path. Lets a nixos-managed host whitelist /etc/nixos/
        # without weakening the guard for the rest of /etc/.
        "sensitive_path_exceptions": [],
        "tirith_enabled": True,
        # Acknowledged supply-chain security advisories. Each entry is the
        # ID of an advisory the user has read and acted on (uninstalled the
        # compromised package, rotated credentials). Acked advisories no
        # longer trigger the startup banner. Add via `son-of-anton doctor --ack
        # <id>`; remove by editing the list directly. See
        # ``son_of_anton_cli/security_advisories.py`` for the catalog.
        "acked_advisories": [],
    },

    "cron": {
        # Fail closed when an unpinned job's current global model/provider
        # differs from its creation-time snapshot. This prevents unattended
        # jobs from silently inheriting a paid default. Set to false only when
        # jobs should deliberately track changing global inference defaults.
        "model_drift_guard": True,
        # Default inference model for cron jobs (Axis A — WHAT model an
        # agent job runs on). Resolution at fire time: per-job user pin >
        # cron.model > global model.default. When set, unpinned jobs follow
        # this deliberately, so the #44585 model-drift fail-closed guard does
        # not engage for the model axis — cron spend no longer shadows chat
        # `/model` switches. Empty string = fall through to model.default.
        "model": "",
        # Inference provider paired with cron.model. Empty string = resolve
        # from global config.
        "model_provider": "",
    },

    # Tool Search (progressive disclosure for large tool surfaces).
    # When the model is connected to many MCP servers or non-core plugin
    # tools, their JSON schemas can consume a substantial fraction of the
    # context window on every turn. When enabled, those tools are replaced
    # in the model-facing tools array with three bridge tools —
    # tool_search / tool_describe / tool_call — and surfaced on demand.
    #
    # Core Son of Anton tools (terminal, read_file, write_file, patch,
    # search_files, todo, memory, browser_*, etc.) are NEVER deferred.
    # See tools/tool_search.py for full design notes and the
    # openclaw-tool-search-report PDF in this PR for the rationale.
    "tools": {
        "tool_search": {
            # Tiered disclosure: any deferrable (MCP/plugin) tool activates
            # the bridge; the listing then scales with catalog size.
            #   Tier 0 — no MCP/plugin tools: everything stays eager.
            #   Tier 1 — catalog listing fits the budget: bridge +
            #     name+description manifest (degrades to names-only).
            #   Tier 2 — per-tool listing over budget even names-only (e.g.
            #     Cloudflare's ~3,300-tool flat API surface): bare bridge +
            #     a one-line-per-server summary (name + tool count) so the
            #     model knows which domains are reachable; individual tools
            #     discoverable through tool_search only.
            # "auto"/"on" — activate when at least one deferrable tool exists.
            # "off" — disable entirely. Tools-array assembly is a pass-through.
            "enabled": "auto",
            # Listing budget as a percentage of the active model's context
            # length. Effective budget = min(this % of context,
            # listing_max_tokens). Range 0..100.
            "threshold_pct": 5,
            # When the model calls tool_search without a ``limit`` argument,
            # how many hits to return. Range 1..max_search_limit.
            "search_default_limit": 5,
            # Hard upper bound the model can request via ``limit``. Range 1..50.
            "max_search_limit": 20,
            # Catalog listing embedded in the tool_search bridge
            # description: every deferred tool's name + first sentence of its
            # description (≤60 chars), grouped by MCP server / toolset. Keeps
            # capabilities discoverable while schemas stay deferred.
            # "auto" (default) — include when the listing fits the budget
            #   (falls back to names-only, then to the bare tier-2 bridge).
            # "on"  — same rendering, but explicit intent to always list.
            # "off" — always the bare bridge (tier 2 for every catalog).
            "listing": "auto",
            # Absolute cap on the embedded listing in tokens (chars/4
            # estimate), regardless of context size. Range 200..60000.
            "listing_max_tokens": 4000,
        },
    },

    # Logging — controls file logging to ~/.son-of-anton/logs/.
    # agent.log captures INFO+ (all agent activity); errors.log captures WARNING+.
    "logging": {
        "level": "INFO",       # Minimum level for agent.log: DEBUG, INFO, WARNING
        "max_size_mb": 5,      # Max size per log file before rotation
        "backup_count": 3,     # Number of rotated backup files to keep
    },

    # Per-model metadata overrides — manually declare context_window,
    # max_output_tokens, capabilities, or model family for any
    # provider+model. Recognized fields: context_window,
    # max_output_tokens, supports_tools, supports_vision,
    # supports_reasoning, model_family.
    #
    # Semantics:
    #   1. Explicit (model_overrides.<provider>.<model_id>): wins over
    #      models.dev and hardcoded defaults for the fields
    #      it sets. NOTE: an explicit model.context_length (global) and a
    #      custom_providers per-model context_length are user settings at
    #      other layers and are consulted in the resolution chain order
    #      documented in agent/model_metadata.py.
    #   2. Fill-gap defaults (model_overrides.<provider>._default and
    #      model_overrides._default): apply ONLY to models the catalog
    #      does not know. They never displace catalog data for known
    #      models, so a _default cannot accidentally clamp every model
    #      of a provider.
    #
    # An unknown model id (not in models.dev) starts from safe defaults
    # (200K context, tools on, vision/reasoning off) and the override
    # patches the fields it sets — overriding a model the catalog
    # doesn't know yet is the supported self-unblock path (#84482,
    # #8731).
    #
    # Provider keys accept the Son of Anton provider id (as used elsewhere in
    # this file) or the models.dev provider id; model ids match
    # case-insensitively.
    #
    # Example:
    #   model_overrides:
    #     upstage:
    #       solar-pro4:
    #         context_window: 524288
    #       syn-pro:
    #         context_window: 65536
    #     custom:my-local-vllm:
    #       my-llava-model:
    #         context_window: 8192
    #         supports_vision: true
    #         supports_reasoning: false
    #         supports_tools: true
    #     _default:            # fill-gap only: models not in the catalog
    #       context_window: 128000
    "model_overrides": {},

    # models.dev registry — provider/model metadata (context windows,
    # capabilities, pricing, modalities).  The agent fetches this on startup
    # and serves from cache; a background daemon refreshes stale data.
    # Override ``url`` to point at a mirror (e.g. a self-hosted copy behind
    # a corporate proxy).  ETag conditional GET ensures refreshes are
    # cheap (304 = no download).
    "models_dev": {
        "url": "",  # empty = default https://models.dev/api.json
    },

    # Network settings — workarounds for connectivity issues.
    "network": {
        # Force IPv4 connections.  On servers with broken or unreachable IPv6,
        # Python tries AAAA records first and hangs for the full TCP timeout
        # before falling back to IPv4.  Set to true to skip IPv6 entirely.
        "force_ipv4": False,
    },

    # Gateway settings — control how messaging platforms (Discord, Slack,
    # Signal, etc.) deliver agent-produced files as native attachments.
    "gateway": {
        # One person owns this instance. /sessions and /resume then see the
        # whole account database — the terminal's view — instead of one
        # chat's lane, so a conversation moves between TUI and messenger.
        # Refused at startup unless every platform allowlist names exactly
        # one user (gateway/single_user.py), so a shared instance cannot
        # flip it by accident.
        "single_user": False,

        # Durable delivery-obligation ledger: final agent responses are
        # recorded in state.db around the platform send, and a gateway that
        # died between finalize and platform ACK redelivers the stored
        # response on the next boot (ambiguous cases carry a visible
        # "recovered reply — may be a duplicate" marker; honest
        # at-least-once). Disable to lose in-flight final responses on
        # crash/restart, as before.
        "delivery_ledger": True,

        # Seconds the gateway waits for a single messaging platform to finish
        # connecting during startup (and on reconnect). Discord in particular
        # can blow past the old fixed 30s when an account has many slash
        # commands to sync (#19776: ~28-31s sync). Raise this
        # if your gateway hits "discord connect timed out" / "Timeout waiting
        # for connection to Discord" restart loops. ``0`` or negative disables
        # the timeout entirely (wait indefinitely). Bridged at startup to the
        # internal SON_OF_ANTON_GATEWAY_PLATFORM_CONNECT_TIMEOUT env var, which still
        # works as a manual override and wins if set explicitly.
        "platform_connect_timeout": 30,

        # In-process event-loop liveness watchdog (#69089). A daemon OS thread
        # probes the gateway asyncio loop; after consecutive missed probes it
        # dumps all-thread stacks and hard-exits with the service-restart exit
        # code so the supervisor (systemd/launchd) revives the process instead
        # of leaving a wedged-but-alive zombie. Set to false to disable.
        "loop_watchdog": True,

        # Whether the gateway keeps writing the legacy sessions.json mirror of
        # its routing index. The primary copy lives in state.db (the
        # gateway_routing table). Default True for backward compatibility with
        # external tooling and downgrade safety; set to false to stop
        # producing ~/.son-of-anton/sessions/sessions.json entirely.
        "write_sessions_json": True,

        # Portable respawn-storm circuit breaker (complements the auto-resume
        # breaker in gateway/restart_loop_guard.py). Counts gateway (re)starts in a sliding
        # window and, when too many land, sleeps an exponential backoff before
        # booting so a crash-looping supervisor (launchd KeepAlive, systemd
        # Restart=always) can't hammer the process into a respawn storm.
        # ``max_starts <= 0`` disables the breaker. The env vars
        # ``SON_OF_ANTON_GATEWAY_MAX_STARTS`` / ``SON_OF_ANTON_GATEWAY_START_WINDOW_S``
        # override these defaults for escape-hatch use.
        "respawn_storm": {
            "max_starts": 5,
            "window_seconds": 120,
        },

        # Inject a human-readable timestamp prefix (e.g.
        # "[Tue 2026-04-28 13:40:53 CEST]") onto user messages IN THE MODEL'S
        # CONTEXT so the agent has temporal awareness of when each message was
        # sent. Off by default — when off, the model sees clean message text.
        # Persisted transcripts always stay clean (the timestamp is stored as
        # message metadata regardless of this toggle), so turning it on later
        # surfaces send-times for past messages too.
        "message_timestamps": {
            "enabled": False,
        },

        # Maximum bytes for an inbound image / audio / video payload the
        # gateway will buffer into memory and cache to disk. Inbound media is
        # read fully into RAM before being written, so an unbounded upload
        # (Discord Nitro allows 500 MB) or a remote media URL pointing at a
        # huge file can spike memory and OOM-kill the gateway on constrained
        # deployments. Enforced in the shared cache helpers
        # (gateway/platforms/base.py), so the cap holds across every platform
        # adapter. ``0`` disables the cap. Default 128 MiB.
        "max_inbound_media_bytes": 134217728,

        # When false (default), any file path the agent emits is delivered
        # as a native attachment as long as it isn't under the credential /
        # system-path denylist (/etc, /proc, ~/.ssh, ~/.aws, ~/.son-of-anton/.env,
        # auth.json, etc.). This matches the symmetry of inbound delivery
        # — we accept any document type the user uploads, and the agent
        # can hand back any file that isn't a credential.
        #
        # When true, fall back to the older allowlist+recency-window
        # behavior: files must live under the Son of Anton cache, under
        # ``media_delivery_allow_dirs``, or be freshly produced inside the
        # ``trust_recent_files_seconds`` window. Recommended for
        # public-facing gateways where prompt injection from one user
        # shouldn't be able to exfiltrate the host's secrets to that same
        # user. Bridged to SON_OF_ANTON_MEDIA_DELIVERY_STRICT.
        "strict": False,
        # Extra directories from which model-emitted bare file paths may be
        # uploaded as native gateway attachments. Files inside the Son of Anton
        # cache (~/.son-of-anton/cache/{documents,images,audio,video,screenshots})
        # are always trusted; this list adds operator-controlled roots
        # (project dirs, scratch dirs, mounted shares). Accepts a list of
        # absolute paths or a single os.pathsep-separated string. Bridged
        # to SON_OF_ANTON_MEDIA_ALLOW_DIRS at gateway startup. Tilde paths are
        # expanded. Honored in both default and strict mode.
        "media_delivery_allow_dirs": [],
        # When true, files whose mtime is within ``trust_recent_files_seconds``
        # of "now" are trusted for native delivery even outside the cache /
        # operator allowlist — useful for ``pandoc -o /tmp/report.pdf`` or
        # PDFs the agent writes into a working directory. System paths
        # (/etc, /proc, ~/.ssh, ~/.aws, etc.) remain blocked regardless.
        # Disable to fall back to pure-allowlist mode. Bridged to
        # SON_OF_ANTON_MEDIA_TRUST_RECENT_FILES. Only consulted when ``strict``
        # is true; in default mode the denylist alone gates delivery.
        "trust_recent_files": True,
        # Recency window in seconds. 600 (10 min) comfortably covers a
        # multi-tool agent turn. Bridged to SON_OF_ANTON_MEDIA_TRUST_RECENT_SECONDS.
        # Only consulted when ``strict`` is true.
        "trust_recent_files_seconds": 600,

    },

    # Real-time token streaming to messaging platforms (Discord, Slack,
    # Slack, etc.). Read at the top level by the gateway; absent this block the
    # gateway falls back to these same defaults, so adding it here only makes
    # the feature discoverable in config.toml — it does not change behavior.
    #
    # Disabled by default: streaming costs extra edit/draft API calls per
    # response. Set ``enabled: true`` and restart the gateway to turn it on.
    "streaming": {
        # Master switch. When false, each response is delivered as a single
        # final message (no progressive updates).
        "enabled": False,
        # Transport selection:
        #   "auto"  — prefer native draft streaming where the platform
        #             supports it and fall back to edit-based elsewhere.
        #             Safe global default: platforms without draft support
        #             transparently use the edit path.
        #   "draft" — explicitly request native drafts; falls back to edit
        #             when the platform/chat doesn't support them.
        #   "edit"  — progressive editMessageText only (legacy behavior).
        #   "off"   — disable streaming entirely (same as enabled: false).
        "transport": "auto",
        # Minimum seconds between progressive edits.
        "edit_interval": 0.8,
        # Flush the buffer to the platform once this many characters have
        # accumulated, so short replies feel near-instant.
        "buffer_threshold": 24,
        # Cursor glyph appended to the in-progress message while streaming.
        "cursor": " \u2589",
        # When >0, the final edit for a long-running streamed response is
        # delivered as a fresh message if the preview has been visible at
        # least this many seconds, so the platform timestamp reflects
        # completion time. Platforms opt in; others ignore it.
        "fresh_final_after_seconds": 0.0,
    },

    # Session storage — controls automatic cleanup of ~/.son-of-anton/state.db.
    # state.db accumulates every session, message, tool call, and FTS5 index
    # entry forever.  Without auto-pruning, a heavy user (gateway + cron)
    # reports 384MB+ databases with 68K+ messages, which slows down FTS5
    # inserts, /resume listing, and insights queries.
    "sessions": {
        # When true, prune ended sessions inactive for retention_days once
        # per (roughly) min_interval_hours at CLI/gateway/cron startup.
        # Activity is the latest message timestamp, falling back to creation
        # time for empty sessions. Active sessions are always preserved.
        # Default false: session history is valuable for search recall, and
        # silently deleting it could surprise users.  Opt in explicitly.
        "auto_prune": False,
        # How many inactive days of ended-session history to keep. Matches
        # the default of ``son-of-anton sessions prune``.
        "retention_days": 90,
        # When true, auto-archive (soft-hide, never delete) sessions that
        # haven't been touched in ``auto_archive_days`` days, once per
        # (roughly) min_interval_hours.  "Touched" is last activity, not
        # creation, so an old-but-recently-used session is spared.  Pinned
        # sessions are always exempt.  Off by default — opt in explicitly.
        "auto_archive": False,
        # Idle threshold (days of no activity) before auto-archive hides a
        # session.  Only applies when auto_archive is true.
        "auto_archive_days": 3,
        # VACUUM after a prune that actually deleted rows.  SQLite does not
        # reclaim disk space on DELETE — freed pages are just reused on
        # subsequent INSERTs — so without VACUUM the file stays bloated
        # even after pruning.  VACUUM blocks writes for a few seconds per
        # 100MB, so it only runs at startup, and only when prune deleted
        # ≥1 session.
        "vacuum_after_prune": True,
        # Minimum days between successful VACUUM rewrites. Pruning can still
        # run on its normal cadence while SQLite reuses the freed pages.
        "min_vacuum_interval_days": 30,
        # Minimum hours between auto-maintenance runs (avoids repeating
        # the sweep on every CLI invocation).  Tracked via state_meta in
        # state.db itself, so it's shared across all processes.
        "min_interval_hours": 24,
        # Legacy per-session JSON snapshot writer.  When true, the agent
        # rewrites ``~/.son-of-anton/sessions/session_{sid}.json`` on every turn
        # boundary with the full message list.  state.db is canonical and
        # has every field the snapshot stored (plus per-message timestamps
        # and token counts), so this is off by default — the snapshots had
        # no consumer outside their own overwrite guard and accumulated
        # GBs of disk on heavy users.  Opt in only if you have an external
        # tool that consumes the JSON files directly.
        "write_json_snapshots": False,
        # Search-index (FTS) storage optimization — the compact v23 layout
        # that drops duplicate content copies and stops trigram-indexing tool
        # output (typically reclaims ~60%+ of state.db on heavy users). It is
        # OPT-IN: existing databases keep their working legacy index until the
        # user runs `son-of-anton sessions optimize-storage`, because the rebuild is
        # disk-heavy and long on large DBs (see that command's disk preflight).
        #
        #   "advise" (default): `son-of-anton update` prints a one-line notice with
        #     the reclaimable size and the command, when a legacy index is
        #     detected. Nothing is changed automatically.
        #   "require": the notice is shown as a REQUIRED upgrade (firmer copy),
        #     and future tooling may gate on it. Flip this default in a future
        #     release when we're ready to make the v23 layout mandatory — the
        #     command, progress bar, and resumability are already in place, so
        #     enforcement is a copy/gating change, not new migration code.
        #   "off": suppress the notice entirely.
        "fts_optimize_notice": "advise",
        # CJK-bigram search index (messages_fts_cjk, cjk_unicode61 loadable
        # tokenizer). When the extension is built (native/fts5_cjk/build.sh →
        # ~/.son-of-anton/lib/libfts5_cjk.so), 1-2 char CJK terms (일본, 项目, ...)
        # get index-speed exact matching instead of LIKE full-table scans.
        # True (default): use the index when the extension is present; the
        # setting is inert when it isn't. False: never load the extension or
        # serve the cjk index. Bridged to SON_OF_ANTON_CJK_FTS (internal carrier).
        "cjk_fts": True,
        # Slow session-search log threshold in milliseconds: searches at or
        # above it log one INFO line with the routing path taken (fts_cjk /
        # fts5 / trigram / like_scan) so latency regressions stay
        # attributable per query shape. 0 logs every search. Bridged to
        # SON_OF_ANTON_SEARCH_SLOW_MS (internal carrier).
        "search_slow_ms": 1000,
        # Transcript safety limits. A runaway session (hundreds of thousands
        # of rows) can exhaust memory when its transcript is materialized in
        # one shot, so interactive resume and in-memory export are guarded by
        # bounded row counts. Set a limit to 0 to disable that guard.
        # Max active messages (across the full compaction lineage) a session
        # may hold and still be resumed interactively (CLI/TUI/desktop).
        "max_resume_messages": 20000,
        # Max active messages a single session may hold for an in-memory
        # (non-streaming) export such as `son-of-anton sessions export`. Checked
        # per session, so full-DB backups of many small sessions still work.
        "max_export_messages": 20000,
    },

    # Contextual first-touch onboarding hints (see agent/onboarding.py).
    # Each hint is shown once per install and then latched here so it
    # never fires again.  Users can wipe the section to re-see all hints.
    "onboarding": {
        "seen": {},
        # Structured profile-build path offered on the very first gateway
        # message ever. "ask" (default) -> offer to build a user profile
        # (opt-in, consent-gated; the agent asks before any lookup and never
        # reads connected accounts silently). "off" -> plain intro only.
        # The offer fires at most once (latched under onboarding.seen).
        "profile_build": "ask",
    },

    # ``son-of-anton doctor`` behaviour.
    "doctor": {
        # Per-probe timeout (seconds) for the opt-in `son-of-anton doctor --live`
        # real-call backend probes (Firecrawl/FAL/browser/MCP/TTS/STT).
        "live_probe_timeout": 10,
    },

    # ``son-of-anton update`` behaviour.
    "updates": {
        # Pre-update safety backup — ONE consolidated mechanism, three modes:
        #
        #   quick (default) — snapshot critical small state files (pairing
        #     JSONs, cron jobs, config.toml, .env, auth.json, per-profile
        #     DBs) into <SON_OF_ANTON_HOME>/state-snapshots/ before the update.
        #     Files over 1 GiB (e.g. a bloated state.db) are skipped with a
        #     warning so the snapshot stays fast. Restore via ``/snapshot``.
        #     This is the #15733 (lost pairing data) / #34600 (emptied cron
        #     jobs) safety net.
        #   full — the quick snapshot PLUS a full ``son-of-anton backup``-style zip
        #     of SON_OF_ANTON_HOME into <SON_OF_ANTON_HOME>/backups/, restorable with
        #     ``son-of-anton import``. Can add minutes on large homes. This is the
        #     #48200 (wrong-path wipe) safety net. ``--backup`` forces this
        #     for a single run.
        #   off — no pre-update backup of any kind. ``--no-backup`` forces
        #     this for a single run.
        #
        # Legacy boolean values are honored: true -> full, false -> off.
        "pre_update_backup": "quick",
        # How many full pre-update backup zips to retain (mode ``full``).
        # Older ones are pruned automatically after each successful backup.
        # Values below 1 are floored to 1 — the backup just created is
        # always preserved. The quick snapshot always keeps exactly 1.
        "backup_keep": 5,
        # What `son-of-anton update` does with uncommitted local changes to the
        # source tree when it runs NON-interactively — i.e. triggered from
        # the desktop/chat app or the gateway, where there's no TTY to answer
        # a restore prompt. Interactive (terminal) updates are unaffected:
        # they always stash the changes and ask whether to restore, exactly
        # as they always have.
        #   "stash"   — auto-stash the changes, pull, then auto-restore them
        #               on top of the updated code (the safe default; nothing
        #               is ever lost — conflicts are preserved in a git stash).
        #   "discard" — auto-stash the changes and throw the stash away after
        #               the pull. Use this only if you never intend to keep
        #               local edits to the source tree on this machine.
        #               Stash-and-drop (not `reset --hard` + `clean -fd`) so
        #               ignored paths — node_modules, venv, build outputs —
        #               are never touched.
        "non_interactive_local_changes": "stash",
        # When `son-of-anton update` finds the source checkout parked on a feature
        # branch (left behind by tooling or a manual checkout), switch back
        # to the update target automatically — but only when the branch is
        # clean and every commit on it is already merged into the target.
        # When it is not safe, the code update is SKIPPED with a loud
        # warning instead of pretending success (2026-08-17 incident:
        # "✓ Code updated!" printed while the checkout stayed days behind
        # main on a stale branch). Set false to never auto-switch.
        "auto_switch_parked_branch": True,
    },

    # Language Server Protocol — semantic diagnostics from real
    # language servers (pyright, gopls, rust-analyzer, etc.) wired
    # into the post-write lint check used by ``write_file`` and
    # ``patch``.
    #
    # LSP is gated on git-workspace detection: when the agent's
    # cwd (or the file being edited) is inside a git worktree, LSP
    # runs against that workspace.  When neither is in a git repo,
    # LSP stays dormant and the in-process syntax check is the only
    # tier — handy for Discord/Slack chats where the cwd is the
    # user's home directory.
    "lsp": {
        # Master toggle.  Setting this to false disables the entire
        # subsystem — no servers spawn, no background event loop, no
        # cost.
        "enabled": True,

        # Diagnostic-wait mode for the post-write check.
        # ``"document"`` waits up to ``wait_timeout`` seconds for the
        # current file's diagnostics; ``"full"`` additionally requests
        # workspace-wide diagnostics (slower).
        "wait_mode": "document",
        "wait_timeout": 5.0,

        # Servers come from PATH only; nothing is installed at runtime. The
        # Nix package puts its ``extraPackages`` on its PATH (e.g. the
        # language servers your editor config enables); a server whose
        # binary is absent is skipped.  ``servers.<id>.command`` points at
        # a binary explicitly.

        # Idle language servers are shut down automatically after this
        # many seconds with no file activity, then respawned on demand.
        # Prevents long-running gateway/CLI processes from accumulating
        # stale pyright/gopls/tsserver children (hundreds of MB each,
        # plus pipe FDs) as the agent moves across worktrees.  Set to 0
        # to disable idle reaping and keep servers for process lifetime.
        "idle_timeout": 600.0,

        # Per-server overrides.  Each key is a server_id from the
        # registry (``pyright``, ``typescript``, ``gopls``,
        # ``rust-analyzer``, etc.) and accepts:
        #   disabled: true
        #     — skip this server even when its extensions match
        #   command: ["full/path/to/server", "--stdio"]
        #     — pin a custom binary path; bypasses auto-install
        #   env: {"KEY": "value"}
        #     — extra env vars passed to the spawned process
        #   initialization_options: {...}
        #     — merged into the LSP ``initializationOptions``
        # Empty by default; the registry defaults work for typical
        # setups.
        "servers": {},
    },


    # Paste collapse thresholds (TUI + CLI).
    #
    # paste_collapse_threshold (default 5)
    #   Bracketed-paste handler. Pastes with this many newlines or more
    #   collapse to a file reference. Set 0 to disable.
    #
    # paste_collapse_threshold_fallback (default 5)
    #   Fallback heuristic for terminals without bracketed paste support.
    #   Same line count test but heuristically gated by chars-added /
    #   newlines-added to avoid false positives from normal typing.
    #   Set 0 to disable.
    #
    # paste_collapse_char_threshold (default 2000)
    #   Long single-line paste guard. Pastes whose total char length
    #   reaches this value collapse to a file reference even if line
    #   count is below the line threshold. Catches the "8000 chars of
    #   minified JSON / log output on one line" case. Set 0 to disable.
    "paste_collapse_threshold": 5,
    "paste_collapse_threshold_fallback": 5,
    "paste_collapse_char_threshold": 2000,

}

# Optional environment variables that enhance functionality
# Optional environment variables that enhance functionality
# (fork surface: deepseek/openai inference, web search, discord+slack messaging)
OPTIONAL_ENV_VARS = {
    "DEEPSEEK_API_KEY": {
        "description": 'DeepSeek API key for direct DeepSeek access',
        "prompt": 'DeepSeek API Key',
        "url": 'https://platform.deepseek.com/api_keys',
        "password": True,
        "category": 'provider',
    },
    "DEEPSEEK_BASE_URL": {
        "description": 'Custom DeepSeek API base URL (advanced)',
        "prompt": 'DeepSeek Base URL',
        "url": '',
        "password": False,
        "category": 'provider',
    },
    "SEARXNG_URL": {
        "description": 'URL of your SearXNG instance for free self-hosted web search',
        "prompt": 'SearXNG URL (e.g. http://localhost:8080)',
        "url": 'https://searxng.github.io/searxng/',
        "tools": ['web_search'],
        "password": False,
        "category": 'tool',
    },
    "GITHUB_TOKEN": {
        "description": 'GitHub token (authenticates GitHub release downloads; higher API rate limits)',
        "prompt": 'GitHub Token',
        "url": 'https://github.com/settings/tokens',
        "password": True,
        "category": 'tool',
    },
    "SUDO_PASSWORD": {
        "description": 'Sudo password for terminal commands requiring root access; set to an explicit empty string to try empty without prompting',
        "prompt": 'Sudo password',
        "url": None,
        "password": True,
        "category": 'setting',
    },
    "SON_OF_ANTON_PREFILL_MESSAGES_FILE": {
        "description": 'Path to JSON file with ephemeral prefill messages for few-shot priming',
        "prompt": 'Prefill messages file path',
        "url": None,
        "password": False,
        "category": 'setting',
    },
    "SON_OF_ANTON_EPHEMERAL_SYSTEM_PROMPT": {
        "description": 'Ephemeral system prompt injected at API-call time (never persisted to sessions)',
        "prompt": 'Ephemeral system prompt',
        "url": None,
        "password": False,
        "category": 'setting',
    },
}
