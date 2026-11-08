# Phase 12: the view socket (read-only viewers)

Goal: a Unix group whose members can watch an instance's sessions (list
them, read their history, follow them live) and do nothing else, without
privileges and without any way to disturb them.

Why (owner, 2026-11-08): on e-workstation the ricky, markets and house
instances run as service users (soa-ricky, soa-markets, soa-house). Their
workers, the interactive sessions fiona starts with relay, can only be
inspected by sudo-ing to the service user, and then only with `-r` /
`/resume`, which attaches as driver and so takes the worker from fiona: its
approvals go to the terminal and her sends queue.

Read first: `internal/server/server.go` (`Listen`, `Serve`), `conn.go`,
`methods.go` (`dispatch`), `live.go` (attach, detach, `next`), `relay.go`
(`workerLink`, `relaySend`), `internal/client`, `internal/tui/app.go`
(`Start`, `attachFirst`), `commands.go` (`attachSession`), `cmd/soa-d`
(`serve`, `tui`), `cmd/son-of-anton`, `nix/nixosModules.nix`.

## The socket

`soa-d serve [--home DIR] [--view-socket PATH --view-group GROUP]`. The two
view flags come together or not at all (usage error, exit 2). Without them
nothing changes. With them the daemon also listens on PATH:

- PATH's directory must already exist, belong to the daemon's user (or
  root), and be writable by its owner only (no group or other write bit):
  whoever can write there could replace the socket. The daemon never creates
  it, since it would get the wrong group. It is outside the instance home, because a
  viewer is not in the instance's group and cannot traverse the home.
- The socket is created, given GROUP (a name or a number), and set to 0660;
  the daemon then checks both and refuses to start if either did not take.
  Giving a file a group needs the daemon to be in that group, unless the
  directory is setgid with that group, in which case the socket is born
  with it. The NixOS module uses the setgid directory (below).
- A live daemon behind PATH is "already running"; a stale file is replaced,
  as for the owner socket.
- **Filesystem permissions are the authorization.** The daemon does not look
  up who connected: every connection accepted on the view socket is a view
  connection. The owner socket (`<home>/run`, 0700; `soa-d.sock`, 0600) is
  exactly as before.
- At most 32 view connections at a time; more are closed at once. An accept
  error on the view socket is retried with backoff, never fatal: nothing a
  viewer does may stop the daemon.

`server.ListenView(path string, gid int) (net.Listener, error)` makes the
listener; `Serve(ctx, ln, views ...net.Listener)` serves the owner listener
and any view listeners.

## Enforcement (server side, default deny)

A view connection may call only:

| method | on a view connection |
|---|---|
| `hello` | as before, plus `read_only: true` |
| `session.list` | as before |
| `session.history` | as before |
| `session.children` | as before |
| `session.attach` | `as: "viewer"` only; `as: "driver"` is refused |
| `session.detach` | as before (it only ever detaches the connection's own attachments) |
| `model.list` | as before |
| `physics.list`, `physics.watch` | as before (both read only: a list, and a subscription to the run's log lines) |

Every other method, including an unknown one and any method added later, is
refused with code **-32014** and the message `this is a read-only view
connection: <method> is not allowed`. The check runs before the method is
looked up, so a new method is refused until it is added to the allowlist.
`session.attach` with `as: "driver"` is refused with -32014 and `this is a
read-only view connection: attach as viewer`.

The wall holds for viewers as for the owner socket: only common sessions
are listed, read or attached. **Fixed here**: `session.list` added loaded,
running sessions of the asked kinds on top of what the store listed, without
the wall, so `kinds: ["chat"]` showed a chat with a turn running (title and
preview) on the owner socket. The loaded sessions it adds are now only the
readable ones.

## A viewer changes nothing

A view connection attached to a session is in its viewers, never its driver,
and nothing that decides what a session does looks at viewers: a worker
with a viewer and no driver is still nobody's at a terminal, so

- its turns report to the chat (single-user instances included);
- its approvals and questions go to the chat; the viewer sees
  `approval.request` / `question.request` and cannot answer them;
- a relay send runs it, or queues it by worker hours or a running turn,
  exactly as without the viewer (never "when the terminal that picked it up
  gets to it"), takes nothing from the viewer, and sends it no `role` event;
- `[relay] worker_hours` still gates its queue.

A viewer keeps the session loaded while attached, as any attached client
does today; that is all.

## The client

`client.DialSocket(path, name)` dials one socket and never starts a daemon
(`Dial(home, name)` is `DialSocket(SocketPath(home), name)`).
`client.ViewSocketPath(instance)` is the module's path,
`/run/son-of-anton-<instance>/view.sock`; an instance name is letters,
digits, `.`, `_` and `-`, not starting with `.` (anything else is refused, so
a name cannot walk out of /run).

```
son-of-anton view INSTANCE | --socket PATH [list] [--all] [--limit N]
son-of-anton view INSTANCE | --socket PATH ID
son-of-anton view INSTANCE | --socket PATH show ID
son-of-anton -r ID|latest --view
```

- `view` never starts a daemon and never reads the instance home (no
  `config.Load`, no `config.Home`): only the socket. A socket it cannot reach
  is `son-of-anton: cannot reach the view socket PATH: ERR`, exit 1.
- `list` (the default) prints `ID  kind  status  updated  title  cwd`, workers
  (`interactive`) by default, `--all` every kind `sessions list --all` asks
  for; `no sessions` when there are none.
- `ID` replaces the process with `soa-d tui --socket PATH --view --resume ID`.
- `show ID` prints the stored transcript, oldest first, paging
  `session.history` back to the start: a header line per message,
  `[user TS]`, `[assistant TS]` or `[tool NAME]`, its content, an assistant
  message's tool calls as `-> NAME ARGUMENTS`, a blank line between messages.
  Reasoning is left out. Nothing interactive, so it can be piped.
- `-r ID --view` is the owner's way to look at a worker without taking it: the
  normal TUI path with `--view` passed on. `--view` without `-r`, or with
  `-m`, `--reasoning`, `--yolo`, `-c`, a query or `-z`, is a usage error.

`soa-d tui` gains `--socket PATH` (dial PATH only: never a daemon start, no
home; with `--home` a usage error) and `--view` (read only; needs
`--resume`; not with `-c`, `-m`, `--reasoning` or `--yolo`).

## The read-only TUI (`tui.Options.View`, `tui.Options.Socket`)

- Every attach is `as: "viewer"`: the first, `/resume`, and every reconnect
  (which dials `Socket` when it is set). No `session.create`, no
  `session.set`. The prompt history file is neither read nor written.
- The prompt is replaced by one line inside its frame: `read-only view:
  you can watch this session, not send to it or answer it`. Typing does
  nothing; `!cmd`, `ctrl+g`, `shift+tab`, `esc` interrupt and `ctrl+enter`
  are gone.
- `ctrl+c` and `ctrl+q` quit at once (a viewer has no turn to decide about).
  `ctrl+p` lists `/resume`, `/status`, `/help` and `/quit` and runs the one
  picked; every other command is refused locally with `read-only view: /NAME
  is not available`. The intro line and the status bar name `ctrl+p` and
  `ctrl+q` instead of `/help` and `:q`.
- Transcript, sidebar, the subagent view (`ctrl+x ↓`, already a viewer
  attach) and live updates as for a driver; `↑`/`↓`, `pgup`/`pgdown` and the
  wheel scroll.
- An approval or question arrives as a read-only box: its text and choices,
  none selected, and `read-only: the session's driver or its chat answers
  this · esc hides it`. Keys never answer it; `esc` hides it here only; it
  closes when the request is resolved.

## NixOS

`services.son-of-anton.instances.<name>.viewerGroup` (string or null,
default null: no view socket, today's behaviour). Set:

- `users.groups.<viewerGroup>` is defined (`mkDefault {}`), so it exists;
- tmpfiles makes `/run/son-of-anton-<name>` mode **2750**, owner the
  instance user, group `viewerGroup` (the setgid bit makes the socket inherit
  the group);
- `ExecStart` gains `--view-socket /run/son-of-anton-<name>/view.sock
  --view-group <viewerGroup>`;
- the directory is in `ReadWritePaths` (under `ProtectSystem=strict` /run is
  read-only);
- an assertion refuses `viewerGroup == group`: the instance's group can
  already read and write its home, so its members are not viewers.

**Why a setgid directory**, not `SupplementaryGroups` or socket activation:
with `SupplementaryGroups` the service user, and every command its agent
runs, would be in the viewer group, so the ricky agent could read markets'
and house's sessions through their view sockets whenever instances share a
viewer group. Socket activation would start a stopped daemon on a viewer's
connection, and keep the socket listening while the unit is stopped. The
setgid directory gives the socket its group with no membership and no new
unit; tmpfiles sets the directory up as root at boot and on every switch.

## Tests (written first)

- `internal/server/viewsock_server_test.go`: the allowlist works on a view
  connection; every method of `dispatch`'s switch, read from `methods.go`'s
  source, is classified (allowed or refused) and each refused one, and an
  unknown one, gets -32014 (an unclassified new method fails the test);
  `as: "driver"` refused; a viewer on a relayed worker leaves relay
  behaviour unchanged (the send runs, no `role` event, approvals go to the
  chat, the report reaches the chat, worker hours still gate); chats are
  neither listed nor readable (owner socket too); the owner socket's modes
  are unchanged and the view socket is 0660 with its group; `ListenView`
  refusals; the connection cap.
- `cmd/son-of-anton/view_test.go`: listing, `--all`, `show`, the TUI exec,
  `-r ID --view`, usage errors; `view` never starts a daemon and never
  reads the home (a home whose config.toml does not parse).
- `cmd/soa-d`: `tui --socket` never starts a daemon; the flag rules of
  `tui` and `serve`.
- `internal/tui/readonly_test.go`: the read-only TUI over the view socket and
  the owner's `--view` (a driver keeps its role).
- `nix/checks.nix`: the module's evaluation (`nixos-viewer-group`) and a
  NixOS VM test (`nixos-view-socket`): a user in the viewer group lists and
  shows sessions and cannot send; a user outside it cannot connect.
