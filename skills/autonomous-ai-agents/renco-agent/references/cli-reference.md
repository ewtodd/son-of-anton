# Renco CLI Reference

Live sources when anything looks stale: `renco --help`, `renco <command> --help`,
https://renco-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
renco [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
renco chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
renco setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
renco model                Interactive model/provider picker
renco fallback [add|remove|list]  Fallback provider chain
renco config [show|edit|get|set|unset|path|env-path|check|migrate]
renco login / logout       OAuth sign-in / clear stored auth
renco doctor [--fix]       Check dependencies and config
renco status [--all]       Component status
```

### Tools & Skills

```
renco tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

renco skills list|browse|search QUERY|inspect ID
renco skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
renco skills config        Enable/disable skills per platform
renco skills check|update|uninstall|publish PATH
renco skills tap add REPO  Add a GitHub repo as a skill source
renco bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
renco mcp add NAME (--url or --command) | remove | list | test NAME
renco mcp catalog | install NAME     Curated catalog install
renco mcp configure NAME             Toggle tool selection
renco mcp serve                      Run Renco as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
renco gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `renco photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://renco-agent.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
renco sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
renco cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
renco webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
renco profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
renco profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
renco auth                 Interactive credential manager
renco auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
renco auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
renco desktop / gui        Native desktop app
renco dashboard            Web admin panel + embedded chat (--stop / --status)
renco proxy                OpenAI-compatible local proxy backed by an OAuth provider
renco portal               Quick setup / sign in via Nous Portal
renco kanban <verb>        Multi-agent work-queue board
renco project              Named multi-folder workspaces
renco skin list|use|set    Switch/tweak skins (see references/themes.md)
renco pets <verb>          Pet mascots (see references/petdex.md)
renco memory setup|status|off|reset   Memory provider
renco secrets bitwarden|onepassword   External secret stores
renco moa                  Mixture-of-Agents slots
renco hooks / security / backup / import / checkpoints / console
renco logs [-f] [errors]   View agent/error logs
renco send                 One-off message through a gateway platform
renco pairing / plugins / insights / journey / computer-use
renco acp                  ACP server (IDE integration)
renco completion bash|zsh|fish
renco update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `renco photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `renco config edit` · [Configuration docs](https://renco-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `renco tools list` · [Tools reference](https://renco-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `renco skills browse` · [Skills catalog](https://renco-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `renco model` · [Providers guide](https://renco-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `renco config env-path` · [Env vars reference](https://renco-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.renco/logs/gateway.log` (or `renco logs`) |
| Sessions | `renco sessions browse` (reads state.db) |
