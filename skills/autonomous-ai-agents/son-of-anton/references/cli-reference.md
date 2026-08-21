# Son of Anton CLI Reference

Live sources when anything looks stale: `son-of-anton --help`, `son-of-anton <command> --help`,
https://son-of-anton.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
son-of-anton [flags] [command]        (no subcommand = interactive chat)

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
son-of-anton chat [flags]
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
son-of-anton setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
son-of-anton model                Interactive model/provider picker
son-of-anton fallback [add|remove|list]  Fallback provider chain
son-of-anton config [show|edit|get|set|unset|path|env-path|check|migrate]
son-of-anton login / logout       OAuth sign-in / clear stored auth
son-of-anton doctor [--fix]       Check dependencies and config
son-of-anton status [--all]       Component status
```

### Tools & Skills

```
son-of-anton tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

son-of-anton skills list|browse|search QUERY|inspect ID
son-of-anton skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
son-of-anton skills config        Enable/disable skills per platform
son-of-anton skills check|update|uninstall|publish PATH
son-of-anton skills tap add REPO  Add a GitHub repo as a skill source
son-of-anton bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
son-of-anton mcp add NAME (--url or --command) | remove | list | test NAME
son-of-anton mcp catalog | install NAME     Curated catalog install
son-of-anton mcp configure NAME             Toggle tool selection
son-of-anton mcp serve                      Run Son of Anton as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
son-of-anton gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `son-of-anton photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://son-of-anton.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
son-of-anton sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
son-of-anton cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
son-of-anton webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
son-of-anton profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
son-of-anton profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
son-of-anton auth                 Interactive credential manager
son-of-anton auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
son-of-anton auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
son-of-anton desktop / gui        Native desktop app
son-of-anton dashboard            Web admin panel + embedded chat (--stop / --status)
son-of-anton proxy                OpenAI-compatible local proxy backed by an OAuth provider
son-of-anton portal               Quick setup / sign in via Nous Portal
son-of-anton kanban <verb>        Multi-agent work-queue board
son-of-anton project              Named multi-folder workspaces
son-of-anton skin list|use|set    Switch/tweak skins (see references/themes.md)
son-of-anton pets <verb>          Pet mascots (see references/petdex.md)
son-of-anton memory setup|status|off|reset   Memory provider
son-of-anton secrets bitwarden|onepassword   External secret stores
son-of-anton moa                  Mixture-of-Agents slots
son-of-anton hooks / security / backup / import / checkpoints / console
son-of-anton logs [-f] [errors]   View agent/error logs
son-of-anton send                 One-off message through a gateway platform
son-of-anton pairing / plugins / insights / journey / computer-use
son-of-anton acp                  ACP server (IDE integration)
son-of-anton completion bash|zsh|fish
son-of-anton update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `son-of-anton photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `son-of-anton config edit` · [Configuration docs](https://son-of-anton.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `son-of-anton tools list` · [Tools reference](https://son-of-anton.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `son-of-anton skills browse` · [Skills catalog](https://son-of-anton.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `son-of-anton model` · [Providers guide](https://son-of-anton.nousresearch.com/docs/integrations/providers) |
| Env variables | `son-of-anton config env-path` · [Env vars reference](https://son-of-anton.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.son-of-anton/logs/gateway.log` (or `son-of-anton logs`) |
| Sessions | `son-of-anton sessions browse` (reads state.db) |
